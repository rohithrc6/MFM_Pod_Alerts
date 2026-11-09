"""Send the built email body to all subscribers via Resend.

Usage:
    python3 tools/send_email.py

--- STALE-EMAIL GUARD STRATEGY ---
A critical safety check runs before any email is sent: send_email.py
compares the episode URL stored in .tmp/last_built_episode.json (written
by build_email.py on every successful build) against the episode URL in
.tmp/scraped_episode.json (the current scrape cycle). If they don't match,
it means build_email.py was NOT run after the latest scrape -- for example
if the scrape job completed but the transcribe/summarise/build chain was
interrupted. In that case the script exits with a clear error rather than
silently sending stale content from a previous episode under the new
episode's subject line. This prevents the mis-matched subject/body bug
where subscribers receive the correct episode title but the wrong content.

Sends one email per subscriber (so addresses aren't exposed to each other)
from RESEND_FROM_EMAIL. Only updates .tmp/last_sent_episode.json if every
subscriber send succeeds, so a partial failure can be safely retried
without double-sending to anyone who already received it.
"""
import csv
import html
import json
import os
import sys

import resend
from dotenv import load_dotenv

from guards import summary_path_for, summary_problems

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = os.path.join(ROOT_DIR, ".tmp")
EMAIL_BODY_PATH = os.path.join(TMP_DIR, "email_body.html")
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


def main():
    """Orchestrate the full send cycle:
    1. Validate Resend credentials from .env.
    2. Confirm email_body.html exists and was built for the current scraped
       episode (stale-email guard via last_built_episode.json).
    3. Load the HTML body and derive the email subject from the episode title.
    4. Send one individual email per subscriber (no BCC -- addresses stay
       private from each other).
    5. On full success, write last_sent_episode.json so the next scrape cycle
       knows not to re-process this episode.
       On any failure, exit 1 WITHOUT writing last_sent_episode.json so the
       whole cycle can be safely retried.
    """
    load_dotenv(os.path.join(ROOT_DIR, ".env"))
    api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("RESEND_FROM_EMAIL")
    if not api_key or not from_email:
        print(
            "RESEND_API_KEY and/or RESEND_FROM_EMAIL are not set in .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    resend.api_key = api_key

    if not os.path.exists(EMAIL_BODY_PATH):
        print(f"{EMAIL_BODY_PATH} not found. Run build_email.py first.", file=sys.stderr)
        sys.exit(1)

    with open(EMAIL_BODY_PATH) as f:
        html_body = f.read()

    with open(SCRAPED_PATH) as f:
        episodes = json.load(f)
    if not episodes:
        print("No new episode this cycle. Skipping send.")
        sys.exit(0)
    newest_episode = episodes[-1]

    # Guard against stale email_body.html: confirm build_email.py was run
    # for this exact episode after scraping it, not a previous episode's build.
    if not os.path.exists(BUILT_MARKER_PATH):
        print(
            "No last_built_episode.json found -- build_email.py hasn't run yet "
            "for this cycle. Run build_email.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(BUILT_MARKER_PATH) as f:
        built = json.load(f)
    if built.get("url") != newest_episode["url"]:
        print(
            f"email_body.html was built for a different episode than what was "
            f"just scraped.\n"
            f"  Built for: {built.get('url')}\n"
            f"  Scraped:   {newest_episode['url']}\n"
            "Run build_email.py again before sending.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Guardrail: don't send the same episode twice (e.g. a build run failed
    # before resetting scraped_episode.json). --resend overrides on purpose.
    if os.path.exists(LAST_SENT_PATH) and "--resend" not in sys.argv:
        with open(LAST_SENT_PATH) as f:
            if json.load(f).get("url") == newest_episode["url"]:
                print(f"'{newest_episode['title']}' was already sent. Skipping (use --resend to force).")
                sys.exit(0)

    # Guardrail (last line of defence): never send unless the episode's
    # summary file has both non-empty chapter_summaries and key_takeaways.
    summary_path = summary_path_for(newest_episode)
    summary_data = None
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            summary_data = json.load(f)
    problems = summary_problems(summary_data) if summary_data else [f"{summary_path} not found"]
    if problems:
        print("Not sending:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        sys.exit(1)

    # Guardrail: the label (last_built_episode.json) and the email itself
    # (email_body.html) must both carry the scraped episode's title, so the
    # subject line and the body can never describe different episodes.
    title = newest_episode["title"]
    title_problems = []
    if built.get("title") != title:
        title_problems.append(
            f"last_built_episode.json title {built.get('title')!r} != scraped title {title!r}"
        )
    if html.escape(title) not in html_body:
        title_problems.append(f"email_body.html does not contain the title {title!r}")
    if title_problems:
        print("Not sending:\n  - " + "\n  - ".join(title_problems), file=sys.stderr)
        sys.exit(1)

    subject = f"MFM Show Notes: {title}"

    subscribers = load_subscribers()
    if not subscribers:
        print("No subscribers (SUBSCRIBERS env var / tools/data/subscribers.csv). Nothing to send.")
        sys.exit(1)

    failures = []
    for email in subscribers:
        try:
            resend.Emails.send(
                {
                    "from": from_email,
                    "to": [email],
                    "subject": subject,
                    "html": html_body,
                }
            )
            print(f"Sent to {mask(email)}")
        except Exception as exc:
            print(f"Failed to send to {mask(email)}: {exc}", file=sys.stderr)
            failures.append(email)

    if failures:
        print(
            f"{len(failures)} of {len(subscribers)} sends failed: {[mask(e) for e in failures]}. "
            "Not marking episode as sent -- retry to deliver to everyone.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(LAST_SENT_PATH, "w") as f:
        json.dump({"url": newest_episode["url"], "title": newest_episode["title"]}, f)

    print(f"All {len(subscribers)} subscriber(s) sent successfully.")


if __name__ == "__main__":
    main()
