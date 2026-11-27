"""Shared parsing helpers and pipeline guardrails.

Every pipeline step validates its own output before the next (paid) step
runs, so a bad scrape never reaches Whisper, a bad transcript never reaches
gpt-4o, and a bad summary never gets built or sent:

    scrape     -> check_episode()           (before Whisper spend)
    transcribe -> check_transcript()        (before gpt-4o spend)
    summarize  -> summary_problems()        (before writing episode_summaries/)
    build      -> summary_problems() + header image reachable
    send       -> summary_problems()        (last line of defence)

Each check returns a list of human-readable problems; empty list = OK.
"""
import os
import re

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARIES_DIR = os.path.join(ROOT_DIR, "episode_summaries")

# Chapter timestamp at the start of a show-notes line: h:mm / mm:ss /
# h:mm:ss. Brackets and the whitespace around the time are optional, since
# the site's markup has produced both "(0:55) Topic" and "( 0:55 ) Topic".
# Anchored to the line start so times mentioned mid-sentence don't match.
TIMESTAMP_RE = re.compile(r"^[(\[]?\s*(\d{1,2}(?::\d{2}){1,2})\s*[)\]]?\s*[-–—:]?\s*(.+)$")


def timestamp_to_seconds(ts):
    seconds = 0
    for part in ts.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds



TMP_DIR = os.path.join(ROOT_DIR, ".tmp")

# --- Episode identity -------------------------------------------------------
# Episodes are identified by TITLE (normalized), not URL or date: the site
# moved to HubSpot in Nov 2026 and every URL changed (www.mfmpod.com/<slug>/
# -> mfmpod.com/podcast/<slug>), and the listing page no longer shows dates.
# The URL slug is kept only as a fallback match in case a title is edited.

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def norm_title(title):
    """Case/quote/whitespace-insensitive title key."""
    return re.sub(r"\s+", " ", (title or "").translate(_QUOTES)).strip().lower()


def slug_of(url):
    """Last path segment: .../podcast/<slug> and the old .../<slug>/ both work."""
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def same_episode(a, b):
    """True if two episode dicts ({title, url}) refer to the same episode."""
    if not a or not b:
        return False
    if a.get("title") and norm_title(a.get("title")) == norm_title(b.get("title")):
        return True
    return bool(a.get("url")) and slug_of(a.get("url")) == slug_of(b.get("url"))


def episode_key(episode):
    """Filesystem-safe per-episode key (the URL slug)."""
    return re.sub(r"[^a-z0-9-]+", "-", slug_of(episode["url"]).lower()).strip("-")


def summary_path_for(episode):
    """episode_summaries/<slug>.json (permanent; one file per episode).
    Files before Nov 2026 are date-named (nov_02_2026.json) -- history only."""
    return os.path.join(SUMMARIES_DIR, episode_key(episode) + ".json")


def transcript_path_for(episode):
    return os.path.join(TMP_DIR, "transcripts", episode_key(episode) + ".json")


def email_path_for(episode):
    return os.path.join(TMP_DIR, "emails", episode_key(episode) + ".html")


def rejected_path_for(episode):
    return os.path.join(TMP_DIR, "rejected", episode_key(episode) + ".json")


def chapter_timestamps(show_notes):
    """List of chapter timestamp strings found in an episode's show notes."""
    return [m.group(1) for p in show_notes if (m := TIMESTAMP_RE.match(p["text"]))]


def check_episode(episode):
    """Scrape output is worth transcribing: has audio and chapter timestamps."""
    problems = []
    if not episode.get("audio_url"):
        problems.append("no audio_url (og:audio meta tag missing)")
    if not episode.get("show_notes"):
        problems.append("show notes are empty")
    elif not chapter_timestamps(episode["show_notes"]):
        problems.append(
            "no chapter timestamps found in show notes -- chapter summaries would be "
            "empty; check the site's timestamp format against guards.TIMESTAMP_RE"
        )
    return problems


def check_transcript(transcript, chunk_segment_counts, show_notes):
    """Transcript is complete enough to summarize every chapter."""
    problems = []
    if not transcript:
        problems.append("transcript is empty")
        return problems
    empty_chunks = [i + 1 for i, n in enumerate(chunk_segment_counts) if n == 0]
    if empty_chunks:
        problems.append(f"Whisper returned no segments for chunk(s) {empty_chunks}")
    stamps = chapter_timestamps(show_notes)
    if stamps:
        last_chapter = max(timestamp_to_seconds(t) for t in stamps)
        covered = transcript[-1]["end"]
        if covered < last_chapter:
            problems.append(
                f"transcript ends at {covered:.0f}s but the last chapter starts at "
                f"{last_chapter}s -- audio was truncated or a chunk failed"
            )
    return problems


def summary_problems(summary_data, show_notes=None):
    """Both chapter_summaries and key_takeaways are present and non-empty
    (and, if show_notes is given, every chapter has a summary)."""
    problems = []
    chapters = (summary_data or {}).get("chapter_summaries") or {}
    takeaways = (summary_data or {}).get("key_takeaways") or []
    if not chapters:
        problems.append("chapter_summaries is empty")
    elif any(not str(v).strip() for v in chapters.values()):
        problems.append("some chapter summaries are blank")
    if not takeaways:
        problems.append("key_takeaways is empty")
    elif any(not str(t.get("text", "")).strip() for t in takeaways):
        problems.append("some key takeaways are blank")
    if show_notes and chapters:
        missing = [t for t in chapter_timestamps(show_notes) if t not in chapters]
        if missing:
            problems.append(f"no summary for chapter(s) {missing}")
    return problems
