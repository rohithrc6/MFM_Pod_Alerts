"""Scrape new MFM Pod episodes and their full Show Notes.

Usage:
    python3 tools/scrape_show_notes.py                    # all episodes newer than the last one sent
    python3 tools/scrape_show_notes.py --title "dying rich"   # exactly one episode, by (part of) its title

--- SITE STRUCTURE (HubSpot, since Nov 2026) ---
Listing:  https://mfmpod.com/podcast (10 per page; older pages at /podcast/p/2, /p/3 ...)
          cards  = .blog-podcast-show-episodes-item
          title/link = .blog-podcast-show-episodes-title-btn a   (no date on the listing)
Episode:  https://mfmpod.com/podcast/<slug>
          date   = .podcast-episode-hero-date time[datetime]   (the page also lists
                   other episodes' dates in a "latest episodes" grid -- ignore those)
          audio  = <audio src="...mp3">
          notes  = div.podcast-episode-show-notes p
The old WordPress selectors are kept as fallbacks.

--- HOW NEW-EPISODE DETECTION WORKS ---
Episodes are identified by TITLE (normalized; URL slug as fallback) -- see
guards.same_episode(). URLs all changed in the site move, so URLs/dates are
not reliable keys.
1. Load .tmp/last_sent_episode.json ({title, url} of the last episode sent).
2. Walk listing cards newest-first (following /p/2, /p/3 up to MAX_PAGES).
   Every card before the last-sent one is "new".
3. If the last-sent episode is NOT found within MAX_PAGES, stop with an error
   instead of treating the whole archive as new.
4. First-ever run (no last_sent_episode.json): only the latest episode.
5. Fetch each new episode's page for its date, audio URL and Show Notes.
   Written oldest-first so the per-episode emails go out in order.

Output: .tmp/scraped_episode.json -- list of episode dicts (empty list = nothing new).
Exit codes: 0 = new episode(s) scraped, 2 = nothing new (normal no-op), 1 = error.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from guards import check_episode, episode_key, norm_title, same_episode

BASE_URL = "https://mfmpod.com"
INDEX_URL = BASE_URL + "/podcast"
MAX_PAGES = 5  # 10 episodes/page -> covers ~2 months of missed runs
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
    """GET with a browser-like User-Agent (the site 403s the default one)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def absolute(href):
    return href if href.startswith("http") else BASE_URL + href


def list_episode_cards(html):
    """Episode cards on one listing page, newest first: [{title, url}]."""
    soup = BeautifulSoup(html, "html.parser")
    episodes = []
    for card in soup.select(".blog-podcast-show-episodes-item"):
        link = card.select_one(".blog-podcast-show-episodes-title-btn a")
        if link and link.get("href"):
            episodes.append({"title": link.get_text(strip=True), "url": absolute(link["href"])})
    if not episodes:  # old WordPress layout, kept as a fallback
        for card in soup.select('[data-object-type="episode"]'):
            link = card.select_one(".pp-card-title a")
            if link and link.get("href"):
                episodes.append({"title": link.get_text(strip=True), "url": absolute(link["href"])})
    return episodes


def iter_listing_cards():
    """Yield cards newest-first across listing pages 1..MAX_PAGES."""
    for page in range(1, MAX_PAGES + 1):
        url = INDEX_URL if page == 1 else f"{INDEX_URL}/p/{page}"
        cards = list_episode_cards(fetch(url))
        if not cards:
            if page == 1:
                raise ValueError(
                    f"No episode cards found on {url} -- the site layout changed again. "
                    "Inspect the HTML and update list_episode_cards()."
                )
            return
        yield from cards


def parse_episode_page(html, episode_url):
    """Extract (show_notes, audio_url, date_text) from an episode page.

    show_notes: list of {"text", "links"} paragraphs, cut at "Check Out Sam's Stuff".
    date_text:  e.g. "Nov 18, 2026" (display only; "" if the page has none).
    Raises ValueError if the show-notes container is missing.
    """
    soup = BeautifulSoup(html, "html.parser")

    audio_url = None
    meta = soup.find("meta", attrs={"property": "og:audio"})
    if meta and meta.get("content"):
        audio_url = meta["content"]
    else:
        tag = soup.select_one("audio[src], audio source[src]")
        audio_url = tag.get("src") if tag else None

    date_text = ""
    t = soup.select_one(".podcast-episode-hero-date time[datetime]")
    if t:
        try:
            date_text = datetime.strptime(t["datetime"][:10], "%Y-%m-%d").strftime("%b %d, %Y")
        except ValueError:
            date_text = t.get_text(strip=True)

    body = soup.select_one("div.podcast-episode-show-notes") or soup.select_one(
        "#show-notes .post-content-body"
    )
    if not body:
        raise ValueError(f"Could not find show notes body on {episode_url}")

    paragraphs = []
    for p in body.find_all("p"):
        # separator=" " stops adjacent tags' text gluing together.
        text = re.sub(r"\s+", " ", p.get_text(separator=" ", strip=True))
        if not text:
            continue
        # Sponsor-link sections are dropped entirely (straight or curly apostrophe).
        if "Check Out Sam" in text and "Stuff" in text:
            break
        links = [
            {"text": re.sub(r"\s+", " ", a.get_text(separator=" ", strip=True)), "href": a.get("href")}
            for a in p.find_all("a")
            if a.get("href")
        ]
        paragraphs.append({"text": text, "links": links})

    if paragraphs and paragraphs[-1]["text"] == "—":
        paragraphs.pop()

    return paragraphs, audio_url, date_text


def load_last_sent():
    """{title, url} of the last episode sent, or None on first-ever run."""
    if not os.path.exists(LAST_SENT_PATH):
        return None
    with open(LAST_SENT_PATH) as f:
        return json.load(f)


def arg_value(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        print(f"{flag} needs a value.", file=sys.stderr)
        sys.exit(1)
    return None


def select_new_episodes():
    """Cards to scrape, newest-first."""
    target = arg_value("--title")
    if target:
        # Manual one-off: exactly one episode whose title contains the text.
        want = norm_title(target)
        matches = [c for c in iter_listing_cards() if want in norm_title(c["title"])]
        if len(matches) != 1:
            print(
                f"--title {target!r} matched {len(matches)} episode(s)"
                + (": " + "; ".join(c["title"] for c in matches) if matches else "")
                + ". Use a more specific part of the title.",
                file=sys.stderr,
            )
            sys.exit(1)
        return matches

    last_sent = load_last_sent()
    if last_sent is None:
        # First-ever run: don't backfill the archive, just the latest episode.
        return [next(iter_listing_cards())]

    new = []
    for card in iter_listing_cards():
        if same_episode(card, last_sent):
            return new
        new.append(card)
    # Guardrail: never treat the whole archive as new because the last-sent
    # episode couldn't be matched (e.g. it was retitled, or a format change).
    print(
        f"Last sent episode {last_sent.get('title')!r} not found in the first "
        f"{MAX_PAGES} listing pages. Refusing to guess what's new -- check the "
        "title on mfmpod.com and fix .tmp/last_sent_episode.json (state/ on GitHub).",
        file=sys.stderr,
    )
    sys.exit(1)


def main():
    os.makedirs(TMP_DIR, exist_ok=True)
    new_episodes = select_new_episodes()

    if not new_episodes:
        print("No new episodes since last send.")
        with open(OUTPUT_PATH, "w") as f:
            json.dump([], f)
        sys.exit(2)  # intentional no-op; the build workflow treats 2 as a clean stop

    results = []
    for card in reversed(new_episodes):  # oldest first
        show_notes, audio_url, date_text = parse_episode_page(fetch(card["url"]), card["url"])
        results.append(
            {
                "title": card["title"],
                "url": card["url"],
                "key": episode_key(card),
                "date_text": date_text,
                "audio_url": audio_url,
                "show_notes": show_notes,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    with open(OUTPUT_PATH, "w") as f:
        json.dump(results, f, indent=2)

    # Guardrail: every scraped episode gets transcribed/summarized, so check
    # them all here (free) before any Whisper spend.
    failed = False
    for ep in results:
        problems = check_episode(ep)
        if problems:
            failed = True
            print(f"Scraped episode {ep['title']!r} failed validation:\n  - " + "\n  - ".join(problems), file=sys.stderr)
    if failed:
        sys.exit(1)

    print(f"Scraped {len(results)} new episode(s) -> {OUTPUT_PATH}")
    for ep in results:
        print(f"  - {ep['date_text'] or '(no date)'}: {ep['title']}")


if __name__ == "__main__":
    main()
