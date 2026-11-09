"""Generate per-timestamp summaries and a Key Takeaways list from the
real episode transcript.

Usage:
    python3 tools/summarize_episode.py

Reads .tmp/scraped_episode.json (for the published show-notes timestamps)
and .tmp/transcript.json (written by transcribe_episode.py), slices the
transcript at each published timestamp boundary, and uses gpt-4o to
summarize each slice plus the whole episode. Writes one file per episode to
the permanent (non-.tmp) episode_summaries/<jun_24_2026>.json, named after
the episode's published date, so each episode's output is preserved across
runs rather than overwritten or lost when .tmp is cleared:
    {
      "chapter_summaries": {"13:14": "...", "24:41": "...", ...},
      "key_takeaways": [{"category": "...", "text": "..."}, ...]
    }

--- TWO-PASS DRAFT + JUDGE STRATEGY ---
Both chapter summaries and key takeaways are generated in two gpt-4o calls:

  Pass 1 (Draft): All chapter transcripts are sent in a single batched
  prompt so the model can see every chapter at once and write summaries
  with naturally varied opening sentences (separate calls can't guarantee
  this). The model returns a JSON object mapping each timestamp to a summary.

  Pass 2 (Judge): The same transcript + the draft summaries are re-sent
  with a stricter "editor" system prompt that explicitly checks for missed
  named examples, coined terms/frameworks (e.g. "Hondafication"), business
  lessons, uncertainty flags, duplicate Key Takeaway topics, and the
  hard count of exactly 7 takeaways. The judge rewrites any bullet that
  fails these checks. Falls back to the draft if the judge response can't
  be parsed.

Summaries are grounded in the actual transcript text -- never generated
from the timestamp topic title alone. Each piece goes through two passes:
a draft, then a judge pass where the model re-reads the transcript against
its own draft and rewrites it to fill in anything missed (named
frameworks/concepts the hosts coin, concrete examples, business lessons,
uncertainty flags) -- thoroughness matters more than brevity or speed here.
"""
import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = os.path.join(ROOT_DIR, ".tmp")
SCRAPED_PATH = os.path.join(TMP_DIR, "scraped_episode.json")
TRANSCRIPT_PATH = os.path.join(TMP_DIR, "transcript.json")
SUMMARIES_DIR = os.path.join(ROOT_DIR, "episode_summaries")
REJECTED_PATH = os.path.join(TMP_DIR, "rejected_summary.json")

from guards import TIMESTAMP_RE, summary_path_for, summary_problems

MODEL = "gpt-4o"


CHAPTER_DRAFT_SYSTEM_PROMPT = """You write detailed, informative chapter summaries of My First Million \
podcast segments for a subscriber email. You will be given several chapters at once and must return a \
summary for each. Follow these rules strictly:

1. Base every summary strictly on the transcript text provided for that chapter. Never invent or assume \
information that isn't in the transcript.
2. Be thorough, not brief. Capture: concrete real-life examples (specific people, companies, deals, \
dollar amounts, dates, tactics); any framework, nickname, or term the hosts coin or reference during the \
discussion (e.g. if they invent a name for a business pattern, name and briefly explain it); and the \
underlying business lesson or principle being illustrated (who did what, and why it worked or didn't).
3. If a host expresses uncertainty or hedges about a fact, number, or example (phrases like "I think", \
"not sure", "could be wrong", "don't quote me on that"), explicitly flag that uncertainty in the summary.
4. Write as many sentences as needed to cover the above thoroughly -- typically 3-6 sentences. Do not \
compress at the expense of leaving out a concrete detail.
5. Each chapter's summary must open with a noticeably different sentence structure/wording than the \
others in this batch.
6. Never use the phrases "recent segment" or "My First Million podcast" anywhere in any summary.
7. Return ONLY a JSON object mapping each chapter's exact timestamp string to its summary, like:
{"0:00": "...", "13:14": "..."}"""

CHAPTER_JUDGE_SYSTEM_PROMPT = """You are a meticulous editor for a My First Million podcast subscriber \
email. You will be given the original transcript for each chapter and a draft summary of that chapter. \
Your job: re-read each transcript carefully against its draft and produce a final, improved version.

Check each draft for:
1. Did it miss any concrete example (person, company, deal, number, date, tactic) present in the \
transcript?
2. Did it miss any framework, nickname, or term the hosts coined or referenced?
3. Did it miss the underlying business lesson (who did what, and why it worked or didn't)?
4. Did it fail to flag a host's stated uncertainty about a fact or number?
5. Does each summary still open differently from the others?

If anything is missing, rewrite that chapter's summary to include it -- it is fine and expected for the \
final version to be longer than the draft if that's what thoroughness requires. Take the time needed to \
get this right; do not just lightly edit the draft. Never use "recent segment" or "My First Million \
podcast". Return ONLY the final JSON object in the same format as the draft: {"0:00": "...", ...}"""

KEY_TAKEAWAYS_DRAFT_SYSTEM_PROMPT = """You write a "Key Takeaways" section for a My First Million \
podcast subscriber email, based on the full episode transcript. Follow these rules strictly:

1. Base every takeaway strictly on the transcript provided. Never invent information not present in it.
2. Maximize practical value: prioritize concrete business lessons (who did what, specific tactics or \
decisions that made something work or fail), real numbers/deals/outcomes mentioned, named \
frameworks/concepts/nicknames the hosts coin or reference, and any specific resources, tools, books, or \
services the hosts recommend.
3. Avoid vague generalities ("they discussed business strategy") in favor of specifics ("X grew Y to $Zm \
by doing W").
4. If a host expresses uncertainty about a fact or number, flag that in the takeaway rather than stating \
it as settled fact.
5. Each bullet covers exactly ONE topic/idea, with a short category label naming that topic (2-4 words, \
e.g. "Pricing Strategy", "Investing Philosophy", "Recommended Resource") followed by the takeaway itself \
(can be multiple sentences if needed to capture the specifics).
6. If the same topic, company, person, or concept (e.g. a specific company like "PSA") is discussed in \
more than one place across the episode, consolidate ALL of those mentions into a single bullet under one \
category. Never create two or more separate bullets about the same underlying topic.
7. Return EXACTLY 7 bullets -- no more, no fewer. Select and merge content into the 7 most valuable, \
clearly distinct topics. If you have more than 7 candidate topics, combine the closest-related ones \
rather than dropping detail; if fewer than 7, pick the most substantive remaining points from the \
transcript rather than padding with filler.
8. Never use the phrases "recent segment" or "My First Million podcast".
9. Return ONLY a JSON object like: {"takeaways": [{"category": "Pricing Strategy", "text": "..."}, ...]} \
with exactly 7 items in the "takeaways" array."""

KEY_TAKEAWAYS_JUDGE_SYSTEM_PROMPT = """You are a meticulous editor reviewing a draft "Key Takeaways" \
list for a My First Million podcast subscriber email against the full episode transcript.

Check the draft for:
1. Did it miss any named framework, nickname, or term the hosts coined or referenced?
2. Did it miss a concrete business lesson (who did what, and why it worked or didn't), with real \
numbers/deals where mentioned?
3. Did it miss a specific resource, tool, book, or service the hosts recommended?
4. Did it state anything as settled fact that a host was actually uncertain about?
5. Does every bullet have exactly one topic with an accurate, specific category label (2-4 words)?
6. CRITICAL -- duplicate-topic check: are there two or more separate bullets about the same underlying \
topic, company, person, or concept (e.g. three different bullets all about the same company)? If so, \
merge them into a single consolidated bullet that keeps all the specifics from each.
7. CRITICAL -- count check: does the list contain EXACTLY 7 bullets? If there are more than 7, merge the \
closest-related ones (do not just delete content -- fold it into a related bullet). If there are fewer \
than 7, add bullets covering other substantive points from the transcript that were left out.

Rewrite the list to fix anything wrong above. Take the time needed to get this right -- the duplicate-topic \
check and the exactly-7-bullets check are the most common mistakes, look carefully for both. Never use \
"recent segment" or "My First Million podcast". Return ONLY the final JSON object in the same format as \
the draft, with exactly 7 items in "takeaways": {"takeaways": [{"category": "...", "text": "..."}, ...]}"""


def timestamp_to_seconds(ts):
    """Convert a timestamp string from the show notes into total seconds.

    Input:  ts (str) -- timestamp in "mm:ss" or "h:mm:ss" format, e.g. "13:14".
    Output: int -- total number of seconds, e.g. 794 for "13:14".
            Used to map show-notes chapter boundaries onto the Whisper transcript
            timeline so the right audio segment is summarised for each chapter.
    """
    parts = [int(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def extract_chapters(show_notes):
    """Pull every timestamped chapter heading out of the scraped show-notes
    paragraphs.

    Input:  show_notes (list of dicts) -- the "show_notes" field from
            scraped_episode.json; each dict has "text" and "links" keys.
    Output: list of dicts, each containing:
              - timestamp    (str) the exact timestamp string, e.g. "13:14"
              - topic        (str) the chapter's short topic label
              - start_seconds (int) timestamp converted to seconds
            Paragraphs that don't match the (mm:ss) or (h:mm:ss) pattern
            are silently skipped.
    """
    chapters = []
    for para in show_notes:
        m = TIMESTAMP_RE.match(para["text"])
        if m:
            chapters.append(
                {
                    "timestamp": m.group(1),
                    "topic": m.group(2),
                    "start_seconds": timestamp_to_seconds(m.group(1)),
                }
            )
    return chapters


def slice_transcript(transcript, start_seconds, end_seconds):
    """Extract and concatenate transcript text that falls within a time window.

    Input:  transcript (list of dicts)  -- flat list from transcript.json,
                                          each dict has "start", "end", "text".
            start_seconds (float/int)   -- window start (inclusive).
            end_seconds   (float/int)   -- window end (exclusive); pass
                                          float("inf") for the last chapter.
    Output: str -- all segment texts whose "start" falls in [start, end),
            joined with spaces. Returns an empty string if no segments match.
    """
    return " ".join(
        seg["text"] for seg in transcript if start_seconds <= seg["start"] < end_seconds
    ).strip()


def chat_json(client, system_prompt, user_prompt):
    """Call gpt-4o in JSON mode and return the parsed response dict.

    Input:  client        (OpenAI) -- authenticated OpenAI client instance.
            system_prompt (str)    -- persistent behaviour/style instructions
                                     loaded into the system role.
            user_prompt   (str)    -- the actual content/data to process,
                                     loaded into the user role.
    Output: dict -- parsed JSON from the model response.
            Returns an empty dict {} and prints a warning if the response
            cannot be parsed as valid JSON.
    """
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0.4,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except (json.JSONDecodeError, TypeError):
        print("Warning: could not parse JSON response.", file=sys.stderr)
        return {}


def generate_chapter_summaries(client, chapters, transcript):
    """Generate a transcript-grounded summary for every chapter in one
    batched two-pass (draft + judge) workflow.

    All chapters are sent in a SINGLE prompt so the model can vary each
    summary's opening sentence naturally (separate per-chapter calls can't
    guarantee uniqueness since the model has no memory of prior responses).

    Input:  client    (OpenAI)     -- authenticated OpenAI client.
            chapters  (list)       -- output of extract_chapters(); each item
                                     has "timestamp", "topic", "start_seconds".
            transcript (list)      -- flat transcript segments from transcript.json.
    Output: dict mapping timestamp string -> summary string, e.g.
            {"0:00": "...", "13:14": "..."}. Falls back to the draft dict if
            the judge response can't be parsed.
    """
    chapter_blocks = build_chapter_blocks(chapters, transcript)
    transcripts_block = "\n\n---\n\n".join(chapter_blocks.values())

    print("  Drafting chapter summaries...")
    draft = chat_json(client, CHAPTER_DRAFT_SYSTEM_PROMPT, transcripts_block)

    print("  Judging and refining chapter summaries...")
    judge_prompt = (
        f"{transcripts_block}\n\n---\n\nDRAFT SUMMARIES TO REVIEW AND IMPROVE:\n"
        f"{json.dumps(draft, indent=2)}"
    )
    final = chat_json(client, CHAPTER_JUDGE_SYSTEM_PROMPT, judge_prompt)
    return fill_missing_chapters(client, final or draft, chapter_blocks)


def build_chapter_blocks(chapters, transcript):
    """Ordered dict: timestamp -> prompt block (label + that chapter's transcript slice)."""
    blocks = {}
    for i, chapter in enumerate(chapters):
        start = chapter["start_seconds"]
        end = chapters[i + 1]["start_seconds"] if i + 1 < len(chapters) else float("inf")
        chapter_text = slice_transcript(transcript, start, end)
        blocks[chapter["timestamp"]] = (
            f"Timestamp: {chapter['timestamp']}\nTopic label: {chapter['topic']}\n"
            f"Transcript:\n{chapter_text or '(no transcript available for this chapter)'}"
        )
    return blocks


def fill_missing_chapters(client, summaries, chapter_blocks, attempts=2):
    """The batched call sometimes silently drops a chapter (seen on short
    chapters right next to another, e.g. 25:45 -> 26:10, and on ones inside
    a long neighbouring segment). Re-request ONLY the missing chapters --
    a few cents, versus re-running the whole summarize step.

    Output: summaries dict with gaps filled where the model complied; the
    guard in main() still rejects the result if anything is left missing.
    """
    summaries = {k: v for k, v in summaries.items() if str(v).strip()}
    for attempt in range(attempts):
        missing = [ts for ts in chapter_blocks if ts not in summaries]
        if not missing:
            break
        print(f"  Filling {len(missing)} missing chapter(s) {missing} (attempt {attempt + 1})...")
        prompt = (
            "Summarize ONLY these chapters. Return a JSON object keyed by exactly these "
            f"timestamps: {missing}\n\n" + "\n\n---\n\n".join(chapter_blocks[ts] for ts in missing)
        )
        filled = chat_json(client, CHAPTER_DRAFT_SYSTEM_PROMPT, prompt)
        summaries.update({ts: v for ts, v in filled.items() if ts in missing and str(v).strip()})
    # Keep show-notes order for the email.
    return {ts: summaries[ts] for ts in chapter_blocks if ts in summaries}


def generate_key_takeaways(client, full_transcript_text):
    """Generate exactly 7 categorised Key Takeaways from the full episode
    transcript using a two-pass draft + judge workflow.

    Input:  client               (OpenAI) -- authenticated OpenAI client.
            full_transcript_text (str)    -- entire episode transcript as a
                                           single concatenated string.
    Output: list of dicts, each {"category": str, "text": str}, length 7.
            If the judge doesn't produce exactly 7, a warning is printed to
            stderr but the list is returned as-is (no silent truncation or
            padding, which would either lose content or fabricate it).
    """
    print("  Drafting key takeaways...")
    draft = chat_json(
        client, KEY_TAKEAWAYS_DRAFT_SYSTEM_PROMPT, f"Transcript:\n{full_transcript_text}"
    )

    print("  Judging and refining key takeaways...")
    judge_prompt = (
        f"Transcript:\n{full_transcript_text}\n\n---\n\nDRAFT TAKEAWAYS TO REVIEW AND IMPROVE:\n"
        f"{json.dumps(draft, indent=2)}"
    )
    final = chat_json(client, KEY_TAKEAWAYS_JUDGE_SYSTEM_PROMPT, judge_prompt)
    takeaways = final.get("takeaways") or draft.get("takeaways", [])
    if len(takeaways) != 7:
        print(
            f"Warning: expected exactly 7 key takeaways, got {len(takeaways)}. "
            "Review .tmp/episode_summaries output -- the model didn't follow the count rule.",
            file=sys.stderr,
        )
    return takeaways


def main():
    """Orchestrate the full summarisation pipeline:
    1. Load the newest episode from scraped_episode.json and the full
       transcript from transcript.json.
    2. Extract chapter boundaries from the episode's show-notes timestamps.
    3. Generate chapter summaries (draft + judge, batched single call).
    4. Generate key takeaways (draft + judge, full transcript).
    5. Write results to episode_summaries/<date>.json (permanent storage).
    """
    if not os.path.exists(SCRAPED_PATH) or not os.path.exists(TRANSCRIPT_PATH):
        print(
            f"{SCRAPED_PATH} and {TRANSCRIPT_PATH} are both required. "
            "Run scrape_show_notes.py and transcribe_episode.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(SCRAPED_PATH) as f:
        episodes = json.load(f)
    if not episodes:
        print("No new episodes to summarize.")
        sys.exit(1)
    episode = episodes[-1]

    with open(TRANSCRIPT_PATH) as f:
        transcript = json.load(f)
    if not transcript:
        print(f"{TRANSCRIPT_PATH} is empty.", file=sys.stderr)
        sys.exit(1)

    load_dotenv(os.path.join(ROOT_DIR, ".env"))
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is not set in .env.", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key)

    os.makedirs(SUMMARIES_DIR, exist_ok=True)
    output_path = summary_path_for(episode)

    chapters = extract_chapters(episode["show_notes"])
    if not chapters:
        # Guardrail: scrape should already have caught this. Fail loudly
        # instead of writing an empty summary that later gets emailed.
        print("No timestamped chapters found in show notes; refusing to summarize.", file=sys.stderr)
        sys.exit(1)

    if "--repair" in sys.argv:
        # Reuse a previously rejected result and only fill the chapters it
        # is missing, instead of paying for the full draft + judge again.
        if not os.path.exists(REJECTED_PATH):
            print(f"--repair needs {REJECTED_PATH}; nothing to repair.", file=sys.stderr)
            sys.exit(1)
        with open(REJECTED_PATH) as f:
            rejected = json.load(f)
        print("Repairing previously rejected summary (missing chapters only)...")
        chapter_summaries = fill_missing_chapters(
            client, rejected.get("chapter_summaries") or {}, build_chapter_blocks(chapters, transcript)
        )
        key_takeaways = rejected.get("key_takeaways") or []
        if not key_takeaways:
            print("Generating key takeaways (draft + judge pass)...")
            key_takeaways = generate_key_takeaways(client, " ".join(s["text"] for s in transcript))
    else:
        print(f"Summarizing {len(chapters)} chapter(s) (draft + judge pass)...")
        chapter_summaries = generate_chapter_summaries(client, chapters, transcript)

        full_text = " ".join(seg["text"] for seg in transcript)
        print("Generating key takeaways (draft + judge pass)...")
        key_takeaways = generate_key_takeaways(client, full_text)

    result = {"chapter_summaries": chapter_summaries, "key_takeaways": key_takeaways}

    # Guardrail: only valid summaries reach the permanent episode_summaries/
    # dir. A bad result is parked in .tmp for inspection and the chain stops.
    problems = summary_problems(result, episode["show_notes"])
    if problems:
        with open(REJECTED_PATH, "w") as f:
            json.dump(result, f, indent=2)
        print(
            "Summary failed validation, not saved to episode_summaries/:\n  - "
            + "\n  - ".join(problems)
            + f"\nRaw output parked at {REJECTED_PATH}",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Summarized {len(chapter_summaries)} chapter(s) -> {output_path}")


if __name__ == "__main__":
    main()
