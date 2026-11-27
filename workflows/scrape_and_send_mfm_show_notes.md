# Workflow: Scrape MFM Show Notes & Email Subscribers

## Objective
Scrape new episode "Show Notes" from https://mfmpod.com/podcast,
transcribe the real episode audio to ground per-timestamp summaries and a
Key Takeaways section in what was actually said, render each episode into
its own styled HTML email with the MFM header image, and send them to all
subscribers from a noreply sender — on a recurring schedule. When several
episodes come out between runs, each gets its own email, oldest first.

## Schedule
Runs on **GitHub Actions** in the public repo (two workflows), independent
of this Mac being on.

- **`.github/workflows/mfm-build.yml`**: Mon-Fri 10:17 PM ET -- scrape ->
  transcribe -> summarize -> build. Scrape exit 2 (no new episode) is a
  clean no-op. Processes EVERY episode published since the last send.
  Manual run (Actions tab -> "MFM build" -> Run workflow) accepts an
  optional `title` (any unique part of an episode title, e.g. `dying rich`)
  to build one specific episode.
- **`.github/workflows/mfm-send.yml`**: Tue-Sat 6:47 AM ET (the morning after each build) -- `send_email.py`.
- **State between runs** lives in the committed `state/` dir:
  `last_sent_episode.json` ({title, url} of the newest episode sent -- the
  scraper's "what's new" reference), `scraped_episode.json` (this cycle's
  episodes, oldest first), `emails/<slug>.html` (one email per episode) and
  `last_built_episode.json` (list of {url, title, key}, one per email).
  Build restores only `last_sent_episode.json`, then REPLACES the other
  hand-off files (even on failure, so stale emails never linger); send
  restores all of `state/` and saves `last_sent_episode.json` back (even on
  failure, since earlier episodes in the run may already have gone out).
  Build also commits `episode_summaries/`. The two workflows share a
  concurrency group so they never touch state at once.
  Local runs still use `.tmp/` and do NOT update `state/` -- the repo is
  the source of truth for the schedule.
- **Timezone**: GitHub cron is UTC-only, so each workflow lists both the EDT
  and EST UTC hour; an "ET gate" step skips whichever trigger isn't the
  right New York time today. Times are set off the top of the hour because
  GitHub queues on-the-hour jobs longest; even so, Nov 2026 runs started ~7h
  late (no start-time guarantee on GitHub cron).
- **Secrets** (repo Settings -> Secrets and variables -> Actions):
  `OPENAI_API_KEY`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, `SUBSCRIBERS`
  (comma-separated emails -- the CSV is gitignored in the public repo).
  The header image URL is derived from the repo itself (raw.githubusercontent.com).
- Actions logs on a public repo are public: `send_email.py` masks addresses
  (`r***@gmail.com`). Never print secrets or full emails from any tool.

### History: why not launchd
Until Nov 2026 this ran as two macOS `launchd` jobs. They failed every run
from July on: (1) macOS privacy protection blocks launchd jobs from reading
`~/Documents` ("Operation not permitted", exit 126); (2) iCloud "Desktop &
Documents" sync with Optimize Storage evicted project files (venv packages,
summaries, images) to the cloud, and a background read of an evicted file
fails with `OSError: [Errno 11] Resource deadlock avoided`; (3) the loaded
job config was stale (plist edited but never reloaded) and pointed at the
pre-rename folder. Don't move scheduling back to launchd while the project
lives in an iCloud-synced ~/Documents.

## Required inputs (`.env`)
| Var | Purpose |
|---|---|
| `RESEND_API_KEY` | Auth for sending email via Resend |
| `RESEND_FROM_EMAIL` | `noreply@rohithxai.com` -- verified custom domain |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | One-time credentials to upload the header image (not needed for recurring runs) |
| `EMAIL_HEADER_IMAGE_URL` | Public URL of the uploaded header image; used in every email |
| `OPENAI_API_KEY` | Whisper transcription + gpt-4o summarization (draft + judge pass). ~$0.70-1.20/episode (~$0.35-0.50 Whisper + ~$0.35-0.70 of gpt-4o calls, since each summary/takeaways pass runs twice: draft, then a judge pass that rewrites it against the transcript) |

## One-time setup (manual, before first scheduled run)
1. Done: Resend account created, API key in `.env` as `RESEND_API_KEY`, the
   `rohithxai.com` domain verified under Domains (SPF/DKIM/DMARC DNS records
   added), and `RESEND_FROM_EMAIL` set to `noreply@rohithxai.com`.
2. Create a free Cloudinary account, copy the **Cloud Name**, **API Key**,
   and **API Secret** shown on its dashboard into `.env` as
   `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`.
3. Run `python3 tools/upload_header_image.py` once; copy the printed URL into
   `.env` as `EMAIL_HEADER_IMAGE_URL`.
4. Add real subscriber emails (one per line under the `email` header) to
   `tools/data/subscribers.csv`.
5. Create an OpenAI account, generate an API key (platform.openai.com →
   API Keys), put it in `.env` as `OPENAI_API_KEY`. Requires `ffmpeg`
   installed locally (`brew install ffmpeg` on macOS) for audio chunking.

## Recurring run sequence
1. `python3 tools/scrape_show_notes.py`
   - Walks the listing (https://mfmpod.com/podcast, then /podcast/p/2 ... up
     to 5 pages) newest-first; every episode before the one in
     `.tmp/last_sent_episode.json` is new. **Episodes are matched by TITLE**
     (case/quote/whitespace-insensitive; URL slug as fallback) -- not by URL
     or date, which both changed in the Nov 2026 site move.
   - If the last-sent episode isn't found in those pages, it stops with an
     error rather than treating the whole archive as new.
   - Fetches each new episode's page for its date (display only), audio URL
     and show notes. Writes `.tmp/scraped_episode.json` (oldest first; empty
     list = nothing new, exit 2).
2. `python3 tools/transcribe_episode.py`
   - For each scraped episode (skipping any that already has a valid
     summary): downloads the audio, splits it into ~25-min chunks
     (re-encoded to 64kbps mono so each chunk stays under Whisper's 25MB
     upload limit regardless of source bitrate), transcribes each chunk via
     OpenAI Whisper, and stitches the per-chunk timestamps into one
     episode-wide timeline.
   - Writes `.tmp/transcripts/<slug>.json` (list of `{start, end, text}` segments).
3. `python3 tools/summarize_episode.py`
   - For each scraped episode (skipping any with a valid summary already):
   - Slices the transcript at each of MFM's own published show-notes
     timestamps. For both the per-chapter summaries and the Key Takeaways
     list, runs a **draft pass** with gpt-4o (grounded strictly in that
     slice's/the full transcript's real text), then a **judge pass** where
     the model re-reads the transcript against its own draft and rewrites it
     to fill in anything missed (named frameworks/terms the hosts coin,
     concrete examples, business lessons, uncertainty flags) — thoroughness
     over brevity, length is not capped.
   - Chapter summaries: 3-6 sentences each, each opening differently from the
     others, prioritizing concrete real-life examples and flagging any
     uncertainty the hosts express about a fact/number.
   - Key Takeaways: **exactly 7 bullets**, each `{"category": "...", "text":
     "..."}` covering exactly one topic (business lesson, resource,
     framework, etc.) with a short category label. If the same topic/company
     comes up in multiple places in the transcript, all of those mentions
     must be consolidated into a single bullet -- never split one topic
     across multiple bullets. The judge pass explicitly re-checks for both
     duplicate-topic bullets and the exact count of 7; `summarize_episode.py`
     prints a warning to stderr if the final count isn't 7.
   - Writes `episode_summaries/<slug>.json` (slug = last part of the
     episode URL, e.g. `shes-making-40m-selling-a-game-to-rich-moms.json`).
     Files before Nov 2026 are date-named (`nov_02_2026.json`) -- history
     only. This directory lives at the project root, **not** in `.tmp` --
     permanent, one file per episode.
4. `python3 tools/build_email.py`
   - For each scraped episode: reads its `episode_summaries/<slug>.json` +
     `EMAIL_HEADER_IMAGE_URL`, writes `.tmp/emails/<slug>.html`. Records all
     of them in `.tmp/last_built_episode.json`.
   - Exits 1 (builds nothing) if any episode's summary is missing/invalid or
     the header image doesn't load — **stop here, do not send**.
5. `python3 tools/send_email.py`
   - Sends each episode's email, oldest first, as its own email (subject
     `MFM Show Notes: <title>`) to every subscriber (`SUBSCRIBERS` env var
     on GitHub, `tools/data/subscribers.csv` locally), one email per
     subscriber. Skips episodes up to and including `last_sent_episode.json`.
   - Advances `.tmp/last_sent_episode.json` after each episode reaches every
     subscriber; on a failure it stops there, so a retry resumes with the
     failed episode and never re-sends earlier ones.

## Email structure (top to bottom)
1. Black header band: "MFM EPISODE HIGHLIGHTS" tagline in MFM yellow (font:
   Oswald, bold), then the centered header image (`email_assets/image.png`,
   uploaded once to Cloudinary).
2. Episode title, date, "Listen to the Episode" button.
3. Episode description paragraph (the non-sponsor-blurb intro text from the
   episode page, before "Show Notes:").
4. **Show Notes**: header + timestamp list, each with its transcript-grounded
   summary directly beneath (one line-space gap).
5. **Key Takeaways**: categorized bullet list, heading styled identically to
   "Show Notes:" (not a separate boxed card).
6. **Misc**: the recurring sponsor-plug line (e.g. "20+ hours of investing
   wisdom") that appears at the very top of MFM's own show notes -- moved to
   the end since it's a sponsor ad, not show content.
7. Black footer band: subscription disclaimer, crediting "Rohith Challa, fan
   of the pod."

Note: "Check Out Sam's Stuff:" / "Check Out Shaan's Stuff:" sponsor-link
sections are intentionally **not included at all** anymore (removed per
user request) -- `scrape_show_notes.py` stops scraping before that content
even appears in `scraped_episode.json`, so there's nothing for
`build_email.py` to render there.

This order was iterated on directly with the user (Rohith) — if asked to
reorder again, update this section so it stays the single source of truth
for "where does X go" rather than re-deriving it from the code each time.

## Inbox tab placement
Sending from a verified custom domain (`noreply@rohithxai.com`, verified with
Resend via SPF/DKIM/DMARC DNS records) as of this writing — this removes the
biggest deliverability red flag (the shared `onboarding@resend.dev` sandbox
sender, which Gmail's classifier treats as a strong bulk-mail signal). Gmail's
Primary vs. Updates/Promotions tab placement is still ultimately Google's ML
model, driven by domain reputation (which builds up over time/sends) and
recipient engagement, not something a sender can force outright — but a
custom verified domain is the strongest lever available, and it's now in
place.

## Guardrails (each step validates before the next, paid, step runs)
All checks live in `tools/guards.py`; any failure exits 1 and stops the chain.
- **scrape** (before Whisper): EVERY new episode has an `audio_url`, show
  notes, and at least one chapter timestamp; and the last-sent episode was
  found on the listing (otherwise it refuses to guess what's new).
- **transcribe** (before gpt-4o): every Whisper chunk returned segments and
  the transcript runs past the last chapter's start time.
- **summarize**: refuses to run with no chapters. Output must have a
  non-empty summary for EVERY chapter plus non-empty key takeaways; a bad
  result goes to `.tmp/rejected/<slug>.json`, never `episode_summaries/`.
  The batched call sometimes silently drops a chapter (seen Nov 2026 on a
  chapter 25s after the previous one, and one inside a long segment), so
  missing chapters are automatically re-requested on their own (a few
  cents). `summarize_episode.py --repair` re-uses the rejected file and
  only fills what's missing -- use it instead of a full paid re-run.
- **transcribe/summarize skip**: an episode whose saved summary already
  passes validation is skipped, so a repeat build (manual re-run, or a retry
  after a failure) never pays for it twice.
- **build**: same summary check for every episode, plus the header image URL
  must return HTTP 200 with an image content-type.
- **send** (per episode, before sending it): its email exists and
  `last_built_episode.json` lists that exact episode (url + title); the
  email contains the episode title; its summary has non-empty
  `chapter_summaries` and `key_takeaways`. Episodes up to `last_sent` are
  skipped (pass `--resend` to send the whole cycle again deliberately).

## Edge cases & handling
- **Site format drift** (Nov 2026): dates changed from "June 24, 2026" to
  "Nov. 2, 2026" / "Sept. 30, 2026", and timestamps scrape as
  "( 0:55 ) Topic" with inner spaces. Both parsers in `guards.py` accept
  either form. If the scrape guard reports "no chapter timestamps", check
  the raw show notes against `guards.TIMESTAMP_RE` first.
- **Header image** (Nov 2026): the Cloudinary URL started returning HTTP 401
  "Unauthenticated access" and emails went out with a broken image. The
  image is now served from the public repo (`email_assets/image.png` via
  raw.githubusercontent.com); `upload_header_image.py`/Cloudinary are no
  longer used.
- **Site move to HubSpot** (Nov 2026): listing moved to
  https://mfmpod.com/podcast (old /episodes/ redirects there), cards are
  `.blog-podcast-show-episodes-item` with no date, episode URLs became
  /podcast/<slug>, audio is an `<audio src>` tag, show notes are
  `div.podcast-episode-show-notes p`, and the episode's own date is
  `.podcast-episode-hero-date time[datetime]` (other dates on the page belong
  to a "latest episodes" grid). Builds failed Nov 17-21 until the scraper
  was updated; the scrape guard stopped them before any OpenAI spend.
  Timestamps also appear as `0:00 - Topic` (no brackets) on some episodes.
- **Manual one-episode run**: `scrape_show_notes.py --title "dying rich"`
  scrapes exactly the one episode whose title contains that text (errors if
  it matches 0 or several), bypassing new-episode detection.
- **No new episode since last run**: `scrape_show_notes.py` writes an empty
  list; `build_email.py` exits 1; the scheduled agent should treat this as a
  normal no-op, not an error.
- **Site structure changes / scrape parse failure**: `scrape_show_notes.py`
  raises if the listing has no episode cards or an episode page has no
  show-notes container — investigate the site's HTML before re-running rather
  than retrying blindly.
- **mfmpod.com blocks default User-Agent**: confirmed 403 with no UA header;
  the scraper always sends a browser-like `User-Agent`. If blocking changes
  (e.g. Cloudflare challenge), this will need a real headless browser instead
  of `requests`.
- **Partial send failure**: `send_email.py` does not mark the episode as sent
  if any subscriber send fails — safe to re-run, though already-succeeded
  subscribers will receive a duplicate. For large subscriber lists this
  should be revisited (e.g. per-subscriber send tracking) if duplicates
  become a real problem.
- **First-ever run** (no `.tmp/last_sent_episode.json` yet): only the single
  latest episode is scraped/sent, not the entire archive.
- **Content scope**: scraping stops as soon as it reaches "Check Out Sam's
  Stuff:" — that section, "Check Out Shaan's Stuff:", sponsor disclaimers,
  the Beehiiv newsletter promo, and the production-credits line are all
  dropped entirely, not just the boilerplate after them. A trailing "—"
  divider left dangling by that cut is also popped off.
- **Link rendering**: `build_email.py` replaces raw URLs in the body with
  small favicon-icon hyperlinks (via Google's favicon service, keyed off the
  link's domain) instead of showing bare links — x.com links show the X
  logo, other sites show their own favicon or a generic fallback icon. The
  recurring sponsor-plug line at the top of every episode ("*label:*
  https://...") is special-cased to hyperlink the label text itself instead
  of showing an icon for it.
- **Text cleanup**: stray `*asterisk*` emphasis markers from the source site
  are stripped (we apply our own bold/yellow styling instead), and each
  line's first letter is capitalized. This is cosmetic only — wording and
  meaning are left untouched.
- **Summaries are transcript-grounded, never invented**: per-timestamp
  summaries and Key Takeaways are generated only from the real Whisper
  transcript of that segment/episode — never from the timestamp topic title
  alone. If `transcribe_episode.py` or `summarize_episode.py` fail (e.g. no
  `audio_url` found, or a transcript chunk comes back empty),
  `build_email.py` should not silently substitute a guessed summary — fix
  the upstream step instead.
- **No published timestamps found**: the scrape guard stops the run (no
  Whisper spend) -- chapter boundaries are never guessed.

## Tools reference
- `tools/scrape_show_notes.py`
- `tools/transcribe_episode.py`
- `tools/summarize_episode.py`
- `tools/build_email.py`
- `tools/send_email.py`
- `tools/upload_header_image.py` (one-time only)
