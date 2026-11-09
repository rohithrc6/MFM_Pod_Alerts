"""Render the scraped episode(s) into a styled HTML email body.

Usage:
    python3 tools/build_email.py

Reads .tmp/scraped_episode.json (written by scrape_show_notes.py),
episode_summaries/<date>.json (written by summarize_episode.py, one
file per episode, named after its published date), and
EMAIL_HEADER_IMAGE_URL from .env, writes .tmp/email_body.html. If
scraped_episode.json is an empty list, writes nothing and exits 1 so the
caller knows to skip sending.

Theme mirrors My First Million's own branding: black + signature yellow
(#f5d90a, the color of mfmpod.com's own subscribe button) on a cream
background, bold uppercase headers. Links render as small favicon icons
(via Google's favicon service) instead of raw URLs to keep the body clean.

--- RENDERING PIPELINE ---
1. render_show_notes_sections() splits the raw scraped show_notes list into
   three separate HTML buckets:
     - description_html : the episode intro paragraph(s) before "Show Notes:"
     - notes_html       : the "Show Notes:" header + timestamp list, each
                          item expanded with its transcript-grounded summary
     - misc_html        : the recurring sponsor-plug line (e.g. "20+ hours of
                          investing wisdom"), moved to the bottom as a Misc
                          section since it's an ad, not show content
2. render_key_takeaways() renders the 7 categorised bullets from the summary
   file using the same header style as "Show Notes:" for visual consistency.
3. render_episode_section() assembles the final order:
   title → date → Listen button → description → Show Notes → Key Takeaways
   → Misc
4. wrap_shell() wraps the assembled content in a full email-safe HTML document
   (table-based layout, inline CSS) with the black header band containing the
   Oswald-font tagline and centred header image, and the black footer band.
5. On success, writes .tmp/last_built_episode.json so send_email.py can verify
   the email was freshly built for the current scraped episode.
"""
import html
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = os.path.join(ROOT_DIR, ".tmp")
INPUT_PATH = os.path.join(TMP_DIR, "scraped_episode.json")
SUMMARIES_DIR = os.path.join(ROOT_DIR, "episode_summaries")
OUTPUT_PATH = os.path.join(TMP_DIR, "email_body.html")
BUILT_MARKER_PATH = os.path.join(TMP_DIR, "last_built_episode.json")


from guards import TIMESTAMP_RE, summary_path_for, summary_problems  # noqa: E402


def header_image_problem(url):
    """Guardrail: the header image must actually load for recipients.
    (Cloudinary silently started returning 401 once -- the email went out
    with a broken image.) Returns a problem string, or None if OK."""
    try:
        resp = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        return f"header image unreachable: {exc}"
    ctype = resp.headers.get("content-type", "")
    if resp.status_code != 200 or not ctype.startswith("image/") or not resp.content:
        return f"header image returned HTTP {resp.status_code} ({ctype or 'no content-type'}): {url}"
    return None

# "*label:* https://example.com" -- the recurring sponsor-plug line at the
# top of every episode's show notes. We hyperlink the label itself instead
# of showing a separate icon for it.
SPONSOR_BLURB_RE = re.compile(r"^\*(.+?):\*\s*(\S+)$")

BLACK = "#111111"
YELLOW = "#f5d90a"
CREAM = "#f7f3ea"
GREY = "#888888"
FONT_STACK = "'DM Sans', Helvetica, Arial, sans-serif"


def favicon_url(href):
    """Build a URL to Google's favicon service for the domain in the given href.

    Input:  href (str) -- full URL whose domain we want a favicon for,
                          e.g. "https://x.com/theSamParr".
    Output: str -- Google favicon service URL, e.g.
            "https://www.google.com/s2/favicons?sz=64&domain=x.com"
            Returns a generic globe icon for domains with no favicon.
    """
    domain = urlparse(href).netloc or href
    if domain.startswith("www."):
        domain = domain[4:]
    return f"https://www.google.com/s2/favicons?sz=64&domain={domain}"


def icon_link(href):
    """Render an anchor tag containing the site's 16×16 favicon image.

    Used to replace bare URL text in the email body with a small branded icon
    so subscribers see a recognisable logo rather than a long raw URL.

    Input:  href (str) -- the destination URL.
    Output: str -- HTML string, e.g. <a href="..."><img src="..."/></a>.
    """
    return (
        f'<a href="{html.escape(href)}" style="text-decoration:none;">'
        f'<img src="{html.escape(favicon_url(href))}" width="16" height="16" '
        f'style="vertical-align:middle;border-radius:3px;border:none;" alt="link"></a>'
    )


def clean_text(text):
    """Apply light vocab and punctuation cleanup to scraped text without
    changing the underlying meaning or wording.

    Input:  text (str) -- raw paragraph text from scraped show notes.
    Output: str -- cleaned text with:
              - literal *asterisk* markdown markers removed (the site
                uses *word* for informal bold emphasis; we apply real CSS bold)
              - runs of 2+ dots collapsed to ". " (e.g. "it...apply" →
                "it. Apply")
              - repeated whitespace collapsed to a single space
              - first letter of every sentence capitalised (covers both the
                start of the string and letters immediately following . ! ?)
    """
    text = text.replace("*", "")
    text = re.sub(r"\.{2,}", ". ", text)  # "Get after it...apply" -> "Get after it. apply"
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"(^|[.!?]\s+)([a-z])",
        lambda m: m.group(1) + m.group(2).upper(),
        text,
    )
    return text


def linkify(text, links):
    """Replace raw URL text occurrences with favicon-icon hyperlinks and
    HTML-escape all surrounding plain text.

    Also consumes any enclosing parentheses around a link
    (e.g. "Sam Parr ( https://x.com/theSamParr )" → "Sam Parr [x-icon]")
    so brackets don't appear next to icons in the rendered email.
    Longer link texts are matched first (sorted descending) to avoid partial
    substring matches.

    Input:  text  (str)  -- raw paragraph text containing URL strings.
            links (list) -- list of {"text": str, "href": str} dicts from
                           the scraper; "text" is the display string in the
                           original HTML (usually the raw URL itself).
    Output: str -- HTML-safe string where every matched link-text occurrence
            has been replaced by an icon_link() and the rest is html.escaped.
    """
    valid_links = [l for l in links if l.get("text")]
    if not valid_links:
        return html.escape(text)

    valid_links.sort(key=lambda l: len(l["text"]), reverse=True)
    alt = "|".join(re.escape(l["text"]) for l in valid_links)
    pattern = re.compile(r"\(\s*(" + alt + r")\s*\)|(" + alt + r")")
    href_by_text = {l["text"]: l["href"] for l in valid_links}

    out = []
    pos = 0
    for m in pattern.finditer(text):
        matched_text = m.group(1) or m.group(2)
        out.append(html.escape(text[pos : m.start()]))
        out.append(icon_link(href_by_text[matched_text]))
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def render_header(text):
    """Render a section heading in the MFM theme style.

    Input:  text (str) -- section label, e.g. "Show Notes:" or "Key Takeaways:".
    Output: str -- HTML <p> styled as bold, uppercase, black text with a
            4px solid yellow left border (the consistent section-header look
            used for Show Notes, Key Takeaways, and Misc throughout the email).
    """
    return (
        f'<p style="font-weight:700;text-transform:uppercase;letter-spacing:0.5px;'
        f'margin:18px 0 10px 0;color:{BLACK};border-left:4px solid {YELLOW};'
        f'padding-left:8px;">{html.escape(clean_text(text))}</p>'
    )


def render_divider():
    """Render a thin yellow horizontal rule used as a section separator.

    Input:  none.
    Output: str -- HTML <hr> styled with a 2px solid yellow border.
    """
    return f'<hr style="border:none;border-top:2px solid {YELLOW};margin:20px 0;">'


def render_sponsor_blurb(match, links):
    """Render the recurring sponsor-plug line as a bold hyperlinked label
    rather than showing the raw URL or an icon.

    The show notes always open with a line like "*20+ hours of investing wisdom:*
    https://clickhubspot.com/cbah". This function turns the label text itself
    into the clickable link, dropping the bare URL from display entirely.

    Input:  match (re.Match) -- result of SPONSOR_BLURB_RE on the paragraph
                                text; group(1) is the label, group(2) the URL.
            links (list)     -- paragraph's link list; links[0]["href"] is the
                                destination URL.
    Output: str -- HTML <p> with a bold yellow-underlined anchor wrapping the
            cleaned label text.
    """
    label = clean_text(match.group(1))
    href = links[0]["href"]
    return (
        f'<p style="margin:0 0 16px 0;">'
        f'<a href="{html.escape(href)}" style="color:{BLACK};font-weight:700;'
        f'text-decoration:underline;text-decoration-color:{YELLOW};">{html.escape(label)}</a>'
        f"</p>"
    )


def render_generic(text, links):
    """Render a standard body paragraph with linkified text.

    Input:  text  (str)  -- raw paragraph text.
            links (list) -- associated link objects {"text", "href"}.
    Output: str -- HTML <p> with clean_text applied and all URL occurrences
            replaced with favicon icon links via linkify().
    """
    return (
        f'<p style="margin:0 0 12px 0;line-height:1.5;color:{BLACK};">'
        f"{linkify(clean_text(text), links)}</p>"
    )


def render_show_notes_sections(show_notes, chapter_summaries=None):
    """Splits the episode body into three pieces so the caller can reorder
    them: description (the episode-description paragraph(s) before "Show
    Notes:", excluding the sponsor blurb), notes (the "Show Notes:" header +
    timestamp items with summaries), and misc (the recurring sponsor-plug
    line, e.g. "20+ hours of investing wisdom"). The "Check Out Sam's/
    Shaan's Stuff" sponsor-link sections are dropped during scraping
    (scrape_show_notes.py stops before them), so there's nothing to render
    after the timestamp list. Returns (description_html, notes_html,
    misc_html)."""
    chapter_summaries = chapter_summaries or {}
    description_parts, notes_parts, misc_parts = [], [], []
    timestamp_items = []
    in_notes = False

    def flush_timestamps():
        nonlocal timestamp_items
        if timestamp_items:
            items_html = "".join(
                f'<li style="margin-bottom:14px;">'
                f'<span style="background-color:{BLACK};color:{YELLOW};font-weight:700;'
                f'font-size:12px;padding:2px 6px;border-radius:3px;">{t}</span> '
                f'<span style="color:{BLACK};font-weight:600;">{body}</span>'
                + (
                    f'<div style="margin-top:1em;color:{GREY};font-size:13px;line-height:1.4;">'
                    f"{html.escape(chapter_summaries[t])}</div>"
                    if chapter_summaries.get(t)
                    else ""
                )
                + "</li>"
                for t, body in timestamp_items
            )
            notes_parts.append(
                f'<ul style="list-style:none;padding-left:0;margin:0 0 20px 0;">{items_html}</ul>'
            )
        timestamp_items = []

    for para in show_notes:
        text = para["text"]
        links = para["links"]

        if not in_notes:
            if text == "Show Notes:":
                in_notes = True
                notes_parts.append(render_header(text))
            elif text == "—":
                description_parts.append(render_divider())
            else:
                sponsor_match = SPONSOR_BLURB_RE.match(text)
                if sponsor_match and links:
                    misc_parts.append(render_sponsor_blurb(sponsor_match, links))
                else:
                    description_parts.append(render_generic(text, links))
            continue

        ts_match = TIMESTAMP_RE.match(text)
        if ts_match:
            timestamp_items.append((ts_match.group(1), html.escape(clean_text(ts_match.group(2)))))

    flush_timestamps()
    return "".join(description_parts), "".join(notes_parts), "".join(misc_parts)


def render_key_takeaways(takeaways):
    """Render the Key Takeaways bullet list with consistent section-header styling.

    Each takeaway is one distinct topic with a short category label and a
    1-2 sentence explanation. The heading uses the same yellow left-border
    style as "Show Notes:" for visual consistency.

    Input:  takeaways (list of dicts) -- each {"category": str, "text": str}.
            Returns empty string if takeaways is empty or None.
    Output: str -- HTML render_header("Key Takeaways:") followed by a
            <ul> of yellow-square-bulleted <li> items in the form
            "<b>Category:</b> takeaway text".
    """
    if not takeaways:
        return ""
    items_html = "".join(
        f'<li style="margin-bottom:10px;color:{BLACK};line-height:1.5;">'
        f'<span style="display:inline-block;width:8px;height:8px;background-color:{YELLOW};'
        f'margin-right:6px;"></span>'
        f'<strong>{html.escape(t.get("category", ""))}:</strong> {html.escape(t.get("text", ""))}</li>'
        for t in takeaways
        if t.get("text")
    )
    return (
        render_header("Key Takeaways:")
        + f'<ul style="list-style:none;padding-left:0;margin:0 0 20px 0;">{items_html}</ul>'
    )


def render_listen_button(url):
    """Render the primary CTA button linking to the episode page.

    Input:  url (str) -- absolute URL of the episode on mfmpod.com.
    Output: str -- HTML <a> styled as a solid yellow button with black bold
            uppercase text ("LISTEN TO THE EPISODE").
    """
    return (
        f'<a href="{html.escape(url)}" style="background-color:{YELLOW};color:{BLACK};'
        f'text-decoration:none;text-transform:uppercase;padding:12px 22px;border-radius:4px;'
        f'font-weight:700;letter-spacing:0.5px;display:inline-block;">Listen to the Episode</a>'
    )


def render_episode_section(episode, heading_tag="h1", summary_data=None):
    """Assemble the complete HTML block for one episode in the correct
    reading order: title → date → Listen button → description → Show Notes
    (timestamps with summaries) → Key Takeaways → Misc (sponsor plug).

    Input:  episode     (dict)      -- single episode dict from scraped_episode.json;
                                      must have "title", "url", "date_text", "show_notes".
            heading_tag (str)       -- HTML heading tag to use for the title,
                                      "h1" for single-episode emails, "h2" for
                                      multi-episode catch-up emails.
            summary_data (dict)     -- contents of the episode's
                                      episode_summaries/<date>.json file; expected
                                      keys: "chapter_summaries" (dict), "key_takeaways"
                                      (list). Pass None if summary data is unavailable
                                      for this episode (older episodes in a catch-up).
    Output: str -- fully assembled HTML for this episode's section of the email.
    """
    summary_data = summary_data or {}
    description_html, notes_html, misc_html = render_show_notes_sections(
        episode["show_notes"], summary_data.get("chapter_summaries")
    )
    misc_section = (render_header("Misc:") + misc_html) if misc_html else ""
    return (
        f'<{heading_tag} style="font-size:24px;color:{BLACK};margin:0 0 6px 0;'
        f'text-transform:uppercase;letter-spacing:0.3px;">{html.escape(episode["title"])}</{heading_tag}>'
        f'<p style="color:{GREY};font-size:13px;margin:0 0 16px 0;text-transform:uppercase;'
        f'letter-spacing:1px;">{html.escape(episode["date_text"])}</p>'
        f'<p style="margin:0 0 20px 0;">{render_listen_button(episode["url"])}</p>'
        f'<hr style="border:none;border-top:2px solid {YELLOW};margin:0 0 20px 0;">'
        f"{description_html}"
        f"{notes_html}"
        f"{render_key_takeaways(summary_data.get('key_takeaways'))}"
        f"{misc_section}"
    )


def wrap_shell(body_html, header_image_url):
    """Wrap the assembled episode body HTML in the full email document shell.

    Produces a table-based layout (required for broad email-client
    compatibility) with:
      - Cream outer background.
      - 600px centred white card with a 6px yellow top border.
      - Black header band: "MFM EPISODE HIGHLIGHTS" in Oswald bold yellow,
        centred header image below it.
      - Main content cell for body_html.
      - Black footer band with the subscription disclaimer.

    Input:  body_html        (str) -- rendered episode HTML from
                                      render_episode_section().
            header_image_url (str) -- public Cloudinary URL for image.png.
    Output: str -- complete HTML document ready to send as the email body.
    """
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://fonts.googleapis.com/css?family=DM+Sans:400,500,700&display=swap">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Oswald:wght@700&display=swap">
</head>
<body style="margin:0;padding:0;background-color:{CREAM};font-family:{FONT_STACK};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{CREAM};padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:8px;overflow:hidden;max-width:600px;border-top:6px solid {YELLOW};">
<tr><td align="center" style="padding:28px 24px 24px 24px;background-color:{BLACK};">
<p style="margin:0 0 16px 0;color:{YELLOW};font-family:'Oswald',Arial,sans-serif;font-weight:700;font-size:22px;text-transform:uppercase;letter-spacing:1px;">MFM Episode Highlights</p>
<img src="{html.escape(header_image_url)}" alt="My First Million" width="180" style="display:block;max-width:180px;height:auto;margin:0 auto;border-radius:6px;">
</td></tr>
<tr><td style="padding:28px 24px;">
{body_html}
</td></tr>
<tr><td style="padding:18px 24px;background-color:{BLACK};text-align:center;color:#aaaaaa;font-size:12px;">
You're receiving this because you subscribed to <span style="color:{YELLOW};font-weight:700;">My First Million</span> show notes updates, curated by Rohith Challa, fan of the pod.
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def main():
    """Orchestrate the full email-build cycle:
    1. Load episode(s) from scraped_episode.json and summaries from the
       per-episode file in episode_summaries/.
    2. Render HTML for each episode section, then wrap in the document shell.
    3. Write .tmp/email_body.html (the file send_email.py will send).
    4. Write .tmp/last_built_episode.json so send_email.py can verify the
       body was built for the current scraped episode before sending.
    """
    if not os.path.exists(INPUT_PATH):
        print(f"{INPUT_PATH} not found. Run scrape_show_notes.py first.", file=sys.stderr)
        sys.exit(1)

    with open(INPUT_PATH) as f:
        episodes = json.load(f)

    if not episodes:
        print("No new episodes to build an email for. Skipping.")
        sys.exit(1)

    load_dotenv(os.path.join(ROOT_DIR, ".env"))
    header_image_url = os.environ.get("EMAIL_HEADER_IMAGE_URL")
    if not header_image_url:
        print(
            "EMAIL_HEADER_IMAGE_URL is not set in .env. "
            "Run tools/upload_header_image.py once and add the printed URL.",
            file=sys.stderr,
        )
        sys.exit(1)

    newest_episode = episodes[-1]
    summary_path = summary_path_for(newest_episode)
    if not os.path.exists(summary_path):
        print(
            f"{summary_path} not found. Run transcribe_episode.py and "
            "summarize_episode.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(summary_path) as f:
        summary_data = json.load(f)

    # Guardrails: never build (and so never send) an email with empty
    # summaries/takeaways or a broken header image.
    problems = summary_problems(summary_data, newest_episode["show_notes"])
    image_problem = header_image_problem(header_image_url)
    if image_problem:
        problems.append(image_problem)
    if problems:
        print("Not building email:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        sys.exit(1)

    # Most runs will have exactly one new episode; if multiple (e.g. a missed
    # run), build one email covering all of them as separate sections.
    # summary_data only ever covers the newest episode (transcribe/summarize
    # only process episodes[-1]), so only that section gets it.
    if len(episodes) == 1:
        body_html = render_episode_section(episodes[0], summary_data=summary_data)
    else:
        sections = "".join(
            render_episode_section(
                ep,
                heading_tag="h2",
                summary_data=summary_data if ep is episodes[-1] else None,
            )
            + f'<hr style="border:none;border-top:2px solid {YELLOW};margin:28px 0;">'
            for ep in episodes
        )
        body_html = sections

    final_html = wrap_shell(body_html, header_image_url)

    with open(OUTPUT_PATH, "w") as f:
        f.write(final_html)

    # Record which episode this build covers so send_email.py can verify
    # email_body.html is fresh before sending (guards against sending stale
    # content if Job 1 was partially interrupted between scrape and build).
    with open(BUILT_MARKER_PATH, "w") as f:
        json.dump({"url": newest_episode["url"], "title": newest_episode["title"]}, f)

    print(f"Built email body -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
