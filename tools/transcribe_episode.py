"""Download an episode's audio and transcribe it with OpenAI's Whisper API.

Usage:
    python3 tools/transcribe_episode.py

--- CHUNKING STRATEGY ---
Whisper's API rejects uploads larger than 25 MB, but a typical MFM episode
(~60-90 min at 128-160 kbps) is 60-100 MB. To work around this:
1. Download the full MP3 to a temp directory.
2. Use ffmpeg to re-encode it into sequential 25-minute chunks at 64 kbps
   mono 16 kHz -- speech-quality settings that keep each chunk around 12 MB,
   well under the 25 MB limit regardless of the source bitrate.
3. Send each chunk to Whisper in order, requesting segment-level timestamps
   (start/end in seconds relative to the chunk's own timeline).
4. Stitch the chunks together by adding a time offset of
   (chunk_index × CHUNK_SECONDS) to every segment's start/end so that all
   timestamps reference the full episode timeline.
5. Write the flat ordered list of segments to .tmp/transcripts/<slug>.json.

Output: .tmp/transcripts/<slug>.json (one per scraped episode) -- list of {"start": float, "end": float,
        "text": str} covering the entire episode in chronological order.

Cost: Whisper API is $0.006/minute (~$0.35-0.50 for a typical 60-90 min
MFM episode).
"""
import json
import os
import subprocess
import sys
import tempfile

from dotenv import load_dotenv
from openai import OpenAI

from guards import check_transcript, summary_path_for, summary_problems, transcript_path_for

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = os.path.join(ROOT_DIR, ".tmp")
SCRAPED_PATH = os.path.join(TMP_DIR, "scraped_episode.json")

# Re-encoded chunk settings: mono/64kbps/16kHz is plenty for speech and keeps
# a 25-minute chunk around ~12MB, safely under Whisper's 25MB upload limit
# regardless of the source file's original bitrate.
CHUNK_SECONDS = 1500
CHUNK_BITRATE = "64k"


def download_audio(url, dest_path):
    """Stream-download an MP3 from a URL to a local file without loading the
    entire file into memory at once.

    Input:  url (str)       -- direct MP3 URL (may include redirect hops).
            dest_path (str) -- local file path to write the audio to.
    Output: none -- writes bytes directly to dest_path.
    Raises: requests.HTTPError on non-2xx response.
    """
    import requests

    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)


def split_audio(input_path, work_dir):
    """Re-encode a full MP3 into fixed-duration chunks suitable for Whisper.

    Uses ffmpeg's segment muxer to split the file into consecutive chunks of
    CHUNK_SECONDS seconds each, re-encoding to 64 kbps mono 16 kHz (adequate
    for speech transcription and guaranteed to stay under Whisper's 25 MB
    per-file limit at any source bitrate).

    Input:  input_path (str) -- path to the full downloaded episode MP3.
            work_dir (str)   -- directory where chunk files will be written.
    Output: list of str -- absolute paths to the generated chunk files,
            sorted in chronological order (chunk_000.mp3, chunk_001.mp3, ...).
    Raises: subprocess.CalledProcessError if ffmpeg exits non-zero.
    """
    pattern = os.path.join(work_dir, "chunk_%03d.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-ar", "16000", "-ac", "1", "-b:a", CHUNK_BITRATE,
            "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
            "-reset_timestamps", "1",
            pattern,
        ],
        check=True,
        capture_output=True,
    )
    chunks = sorted(
        f for f in os.listdir(work_dir) if f.startswith("chunk_") and f.endswith(".mp3")
    )
    return [os.path.join(work_dir, c) for c in chunks]


def transcribe_chunk(client, chunk_path):
    """Send one audio chunk to OpenAI Whisper and return its timed segments.

    Requests verbose_json with segment-level timestamps so each returned
    segment includes start/end times relative to the chunk's own timeline
    (i.e. starting from 0). The caller is responsible for adding the
    appropriate time offset to align these to the full episode timeline.

    Input:  client (OpenAI)  -- authenticated OpenAI client instance.
            chunk_path (str) -- local path to the MP3 chunk file.
    Output: list of segment objects, each with .start, .end, .text attributes.
            Returns an empty list if Whisper returns no segments.
    """
    with open(chunk_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    return result.segments or []


def transcribe_episode(client, episode):
    """Download, chunk and transcribe one episode; returns its segments."""
    with tempfile.TemporaryDirectory(dir=TMP_DIR) as work_dir:
        audio_path = os.path.join(work_dir, "episode.mp3")
        print("  Downloading episode audio...")
        download_audio(episode["audio_url"], audio_path)

        print("  Splitting into chunks...")
        chunk_paths = split_audio(audio_path, work_dir)

        all_segments = []
        chunk_segment_counts = []
        for i, chunk_path in enumerate(chunk_paths):
            offset = i * CHUNK_SECONDS
            print(f"  Transcribing chunk {i + 1}/{len(chunk_paths)}...")
            segments = transcribe_chunk(client, chunk_path)
            chunk_segment_counts.append(len(segments))
            for seg in segments:
                all_segments.append(
                    {"start": seg.start + offset, "end": seg.end + offset, "text": seg.text.strip()}
                )

    # Guardrail: don't hand a gappy/truncated transcript to the paid
    # summarize step -- it would produce summaries for missing chapters.
    problems = check_transcript(all_segments, chunk_segment_counts, episode.get("show_notes", []))
    if problems:
        print(f"Transcript for {episode['title']!r} failed validation:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        sys.exit(1)
    return all_segments


def already_summarized(episode):
    """A valid saved summary means transcribe+summarize already ran for this
    episode (e.g. a repeat build before the send) -- skip it, don't pay twice."""
    path = summary_path_for(episode)
    if not os.path.exists(path):
        return False
    with open(path) as f:
        return not summary_problems(json.load(f), episode.get("show_notes"))


def main():
    """Transcribe every episode in scraped_episode.json (oldest first) to
    .tmp/transcripts/<slug>.json. Stops at the first failure (exit 1)."""
    if not os.path.exists(SCRAPED_PATH):
        print(f"{SCRAPED_PATH} not found. Run scrape_show_notes.py first.", file=sys.stderr)
        sys.exit(1)
    with open(SCRAPED_PATH) as f:
        episodes = json.load(f)
    if not episodes:
        print("No new episodes to transcribe.")
        sys.exit(1)

    load_dotenv(os.path.join(ROOT_DIR, ".env"))
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is not set in .env.", file=sys.stderr)
        sys.exit(1)
    # max_retries=5 / timeout=300: large chunk uploads hit transient SSL/network
    # errors; the SDK retries those with backoff, and 5 min stops silent hangs.
    client = OpenAI(api_key=api_key, max_retries=5, timeout=300)

    for n, episode in enumerate(episodes, 1):
        print(f"[{n}/{len(episodes)}] {episode['title']}")
        out_path = transcript_path_for(episode)
        if already_summarized(episode):
            print("  Already has a valid summary -- skipping transcription.")
            continue
        if os.path.exists(out_path):
            print(f"  Transcript already exists -> {out_path}")
            continue
        if not episode.get("audio_url"):
            print(f"No audio_url found for episode {episode['url']}", file=sys.stderr)
            sys.exit(1)
        segments = transcribe_episode(client, episode)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(segments, f, indent=2)
        print(f"  Transcribed {len(segments)} segment(s) -> {out_path}")


if __name__ == "__main__":
    main()
