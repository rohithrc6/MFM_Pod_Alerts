"""Scrape new MFM Pod episodes and their full Show Notes.

Usage:
    python3 tools/scrape_show_notes.py

--- HOW NEW-EPISODE DETECTION WORKS ---
1. Load .tmp/last_sent_episode.json to get the URL of the last episode
   that was successfully sent to subscribers.
2. Fetch the /episodes/ index page and parse all episode cards (title,
   URL, date) in the order they appear (newest first).
3. Walk the cards from the top. Any card whose URL has NOT yet been seen
   in last_sent_episode.json is "new". Stop walking the moment we hit the
   URL that matches last_sent_episode.json -- everything from that point
   down is already known.
4. On first-ever run (no last_sent_episode.json), treat only the single
   latest card as new (avoids backfilling the entire archive on day one).
5. For each new episode, fetch its episode page and extract Show Notes.
   Episodes are written oldest-first so multi-episode catch-up emails read
   chronologically.

Output: .tmp/scraped_episode.json  -- list of episode dicts (empty list
        if nothing new, which is the normal no-op signal for downstream steps).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from guards import check_episode

INDEX_URL = "https://www.mfmpod.com/episodes/"
BASE_URL = "https://www.mfmpod.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = os.path.join(ROOT_DIR, ".tmp")
LAST_SENT_PATH = os.path.join(TMP_DIR, "last_sent_episode.json")
OUTPUT_PATH = os.path.join(TMP_DIR, "scraped_episode.json")


def fetch(url):
    """Fetch a URL with a browser-like User-Agent header (required -- the site
    returns 403 for default Python/requests UA).

    Input:  url (str) -- fully qualified URL to fetch.
    Output: response body as a string.
    Raises: requests.HTTPError if the server returns a non-2xx status.
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def list_episode_cards(html):
    """Parse the /episodes/ index page and extract metadata for every episode
    card listed on it.

    Input:  html (str) -- raw HTML of the /episodes/ index page.
    Output: list of dicts, each containing:
              - title    (str) episode title
              - url      (str) absolute URL to the episode page
              - date_text (str) human-readable date, e.g. "June 24, 2026"
            Cards are in the same order as the page (newest first).
    """
    soup = BeautifulSoup(html, "html.parser")
    episodes = []
    for card in soup.select('[data-object-type="episode"]'):
        date_el = card.select_one(".pp-card-date")
        title_el = card.select_one(".pp-card-title a")
        if not title_el:
            continue
        href = title_el.get("href", "")
        url = href if href.startswith("http") else BASE_URL + href
        episodes.append(
            {
                "title": title_el.get_text(strip=True),
                "url": url,
                "date_text": date_el.get_text(strip=True) if date_el else "",
            }
        )
    return episodes


def find_audio_url(soup):
    """Extract the direct MP3 download URL from an episode page's Open Graph
    audio meta tag (<meta property="og:audio">).

    Input:  soup (BeautifulSoup) -- parsed episode page.
    Output: audio URL string, or None if the meta tag is absent.
    """
    meta = soup.find("meta", attrs={"property": "og:audio"})
    return meta.get("content") if meta else None


def parse_episode_page(html, episode_url):
    """Extract Show Notes paragraphs and the audio URL from an individual
    episode page.

    The Show Notes live inside #show-notes .post-content-body as a sequence
    of <p> tags. Scraping stops as soon as "Check Out Sam's Stuff" is
    encountered (the sponsor-links sections are intentionally excluded).
    A trailing em-dash divider left dangling by that cut is also removed.

    Input:  html (str)         -- raw HTML of the episode page.
            episode_url (str)  -- used only in the error message if the
                                  show-notes container is missing.
    Output: tuple of:
              - paragraphs (list of dicts): each dict has
                  "text" (str)   plain text of the paragraph
                  "links" (list) each item is {"text": str, "href": str}
              - audio_url (str or None): direct MP3 URL from og:audio.
    Raises: ValueError if the #show-notes container is not found on the page.
    """
    soup = BeautifulSoup(html, "html.parser")
    audio_url = find_audio_url(soup)

    body = soup.select_one("#show-notes .post-content-body")
    if not body:
        raise ValueError(f"Could not find show notes body on {episode_url}")

    paragraphs = []
    for p in body.find_all("p"):
        # separator=" " avoids adjacent tags' text gluing together with no
        # space (e.g. a link directly followed by more text); collapse any
        # resulting double spaces.
        text = re.sub(r"\s+", " ", p.get_text(separator=" ", strip=True))
        if not text:
            continue

        # "Check Out Sam's/Shaan's Stuff" sponsor-link sections are dropped
        # entirely -- stop scraping as soon as that section starts.
        if "Check Out Sam's Stuff" in text:
            break

        links = [
            {
                "text": re.sub(r"\s+", " ", a.get_text(separator=" ", strip=True)),
                "href": a.get("href"),
            }
            for a in p.find_all("a")
            if a.get("href")
        ]
        paragraphs.append({"text": text, "links": links})

    # Drop a trailing "—" divider left dangling with nothing after it now
    # that the section that followed it has been cut.
    if paragraphs and paragraphs[-1]["text"] == "—":
        paragraphs.pop()

    return paragraphs, audio_url


def load_last_sent_url():
    """Read the episode URL that was most recently sent to subscribers.

    Input:  none -- reads from .tmp/last_sent_episode.json on disk.
    Output: URL string of the last sent episode, or None if the file does
            not exist (first-ever run).
    """
    if not os.path.exists(LAST_SENT_PATH):
        return None
    with open(LAST_SENT_PATH) as f:
        return json.load(f).get("url")


def main():
    """Orchestrate the full scrape cycle:
    1. Fetch the episodes index and parse all cards.
    2. Compare against last_sent_episode.json to identify new episodes.
    3. Fetch and parse Show Notes for each new episode.
    4. Write results to .tmp/scraped_episode.json (empty list = nothing new).
    """
    os.makedirs(TMP_DIR, exist_ok=True)

    index_html = fetch(INDEX_URL)
    cards = list_episode_cards(index_html)
    if not cards:
        print("No episode cards found on index page.", file=sys.stderr)
        sys.exit(1)

    last_sent_url = load_last_sent_url()

    # Optional: --date "Nov. 2, 2026" (or "November 2, 2026") scrapes exactly
    # that one episode, bypassing new-episode detection. Used for manual
    # one-off sends / catch-ups.
    target_date = None
    if "--date" in sys.argv:
        target_date = sys.argv[sys.argv.index("--date") + 1]

    def norm(d):
        month, rest = d.strip().split(" ", 1)
        return (month.rstrip(".")[:3].lower(), rest.strip())

    if target_date:
        new_episodes = [c for c in cards if c["date_text"] and norm(c["date_text"]) == norm(target_date)]
        if not new_episodes:
            print(f"No episode found on index dated {target_date}.", file=sys.stderr)
            sys.exit(1)
    elif last_sent_url is None:
        # First-ever run: don't backfill the whole archive, just the latest episode.
        new_episodes = [cards[0]]
    else:
        new_episodes = []
        for card in cards:
            if card["url"] == last_sent_url:
                break
            new_episodes.append(card)

    if not new_episodes:
        print("No new episodes since last send.")
        with open(OUTPUT_PATH, "w") as f:
            json.dump([], f)
        sys.exit(2)  # exit 2 = intentional no-op; run_pipeline.sh treats this as clean stop

    results = []
    for card in reversed(new_episodes):  # oldest-new-episode first
        episode_html = fetch(card["url"])
        show_notes, audio_url = parse_episode_page(episode_html, card["url"])
        results.append(
            {
                "title": card["title"],
                "url": card["url"],
                "date_text": card["date_text"],
                "audio_url": audio_url,
                "show_notes": show_notes,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    with open(OUTPUT_PATH, "w") as f:
        json.dump(results, f, indent=2)

    # Guardrail: the newest episode is the one transcribed/summarized next.
    # Stop here (free) rather than after paying for Whisper if it has no
    # audio or no chapter timestamps to summarize against.
    problems = check_episode(results[-1])
    if problems:
        print(
            f"Scraped episode '{results[-1]['title']}' failed validation:\n  - "
            + "\n  - ".join(problems),
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Scraped {len(results)} new episode(s) -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
