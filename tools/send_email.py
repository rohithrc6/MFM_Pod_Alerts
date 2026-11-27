"""Send each built episode email to all subscribers via Resend.

Usage:
    python3 tools/send_email.py            # send episodes not yet sent, oldest first
    python3 tools/send_email.py --resend   # send every episode in this cycle again

--- WHAT GETS SENT ---
.tmp/scraped_episode.json lists this cycle's new episodes (oldest first);
build_email.py wrote one email per episode to .tmp/emails/<slug>.html. Each
episode is a separate email with its own subject. Episodes up to and
including .tmp/last_sent_episode.json are skipped, so a retry after a
partial failure never re-sends an episode that already went out.

--- GUARDRAILS (per episode, before sending it) ---
- its email file exists and last_built_episode.json lists this exact
  episode (url + title) -- i.e. the email wasn't left over from an older build
- the email body contains the episode title (subject and body match)
- its episode_summaries/<slug>.json has non-empty chapter_summaries and
  key_takeaways
Any failure stops the run (exit 1) before that episode is sent.

Sends one email per subscriber (addresses aren't exposed to each other) from
RESEND_FROM_EMAIL. last_sent_episode.json advances only after an episode
reaches every subscriber; a partial failure stops there so it can be retried.
"""
import csv
import html
import json
import os
import sys

import resend
from dotenv import load_dotenv

from guards import email_path_for, same_episode, summary_path_for, summary_problems

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = os.path.join(ROOT_DIR, ".tmp")
SCRAPED_PATH = os.path.join(TMP_DIR, "scraped_episode.json")
BUILT_MARKER_PATH = os.path.join(TMP_DIR, "last_built_episode.json")
SUBSCRIBERS_PATH = os.path.join(ROOT_DIR, "tools", "data", "subscribers.csv")
LAST_SENT_PATH = os.path.join(TMP_DIR, "last_sent_episode.json")


def load_subscribers():
    """Subscriber addresses, from the SUBSCRIBERS env var (comma-separated;
    how GitHub Actions supplies them, via a repo secret, since the CSV is
    gitignored in the public repo) or else tools/data/subscribers.csv
    (local runs; "email" header column, blank rows skipped)."""
    env_list = os.environ.get("SUBSCRIBERS", "")
    if env_list.strip():
        return [e.strip() for e in env_list.split(",") if e.strip()]
    if not os.path.exists(SUBSCRIBERS_PATH):
        return []
    with open(SUBSCRIBERS_PATH, newline="") as f:
        reader = csv.DictReader(f)
        return [row["email"].strip() for row in reader if row.get("email", "").strip()]


def mask(email):
    """r***@gmail.com -- Actions logs are public on a public repo."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def episode_problems(episode, built):
    """Everything that must hold before this episode's email may be sent."""
    problems = []
    if not any(b.get("url") == episode["url"] and b.get("title") == episode["title"] for b in built):
        problems.append("last_built_episode.json has no matching entry -- email is from an older build")
    email_path = email_path_for(episode)
    if not os.path.exists(email_path):
        problems.append(f"{email_path} not found -- run build_email.py")
    elif html.escape(episode["title"]) not in open(email_path).read():
        problems.append(f"{email_path} does not contain the title {episode['title']!r}")
    summary = load_json(summary_path_for(episode), None)
    problems += summary_problems(summary) if summary else [f"{summary_path_for(episode)} not found"]
    return problems


def send_to_all(subscribers, from_email, subject, html_body):
    failures = []
    for email in subscribers:
        try:
            resend.Emails.send({"from": from_email, "to": [email], "subject": subject, "html": html_body})
            print(f"  Sent to {mask(email)}")
        except Exception as exc:
            print(f"  Failed to send to {mask(email)}: {exc}", file=sys.stderr)
            failures.append(email)
    return failures


def main():
    load_dotenv(os.path.join(ROOT_DIR, ".env"))
    api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("RESEND_FROM_EMAIL")
    if not api_key or not from_email:
        print("RESEND_API_KEY and/or RESEND_FROM_EMAIL are not set.", file=sys.stderr)
        sys.exit(1)
    resend.api_key = api_key

    episodes = load_json(SCRAPED_PATH, [])
    if not episodes:
        print("No new episode this cycle. Skipping send.")
        sys.exit(0)

    built = load_json(BUILT_MARKER_PATH, [])
    if isinstance(built, dict):  # pre-Nov-2026 single-episode marker
        built = [built]

    # Skip everything up to and including the last episode sent (the list is
    # oldest-first), unless --resend.
    pending = episodes
    last_sent = load_json(LAST_SENT_PATH, None)
    if last_sent and "--resend" not in sys.argv:
        for i, ep in enumerate(episodes):
            if same_episode(ep, last_sent):
                pending = episodes[i + 1:]
                break
    if not pending:
        print("All episodes in this cycle were already sent. Skipping (use --resend to force).")
        sys.exit(0)

    subscribers = load_subscribers()
    if not subscribers:
        print("No subscribers (SUBSCRIBERS env var / tools/data/subscribers.csv). Nothing to send.")
        sys.exit(1)

    for n, episode in enumerate(pending, 1):
        print(f"[{n}/{len(pending)}] {episode['title']}")
        problems = episode_problems(episode, built)
        if problems:
            print("Not sending this or any later episode:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            sys.exit(1)

        with open(email_path_for(episode)) as f:
            html_body = f.read()
        failures = send_to_all(subscribers, from_email, f"MFM Show Notes: {episode['title']}", html_body)
        if failures:
            print(
                f"{len(failures)} of {len(subscribers)} sends failed: {[mask(e) for e in failures]}. "
                "Not marking this episode as sent -- retry to deliver to everyone.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Advance after each fully-delivered episode, so a later failure
        # never causes this one to be re-sent.
        with open(LAST_SENT_PATH, "w") as f:
            json.dump({"url": episode["url"], "title": episode["title"]}, f)
        print(f"  All {len(subscribers)} subscriber(s) sent.")

    print(f"Sent {len(pending)} episode email(s).")


if __name__ == "__main__":
    main()
