# Workflow: Scrape MFM Show Notes & Email Subscribers

## Objective
Scrape new episode "Show Notes" from https://www.mfmpod.com/episodes/,
transcribe the real episode audio to ground per-timestamp summaries and a
Key Takeaways section in what was actually said, render all of it into a
styled HTML email with the MFM header image, and send that email to all
subscribers from a noreply sender — on a recurring schedule.

## Schedule
Runs on **GitHub Actions** in the public repo (two workflows), independent
of this Mac being on.

- **`.github/workflows/mfm-build.yml`**: Tue/Wed/Thu 10:00 PM ET -- scrape ->
  transcribe -> summarize -> build. Scrape exit 2 (no new episode) is a
  clean no-op. Manual run (Actions tab -> "MFM build" -> Run workflow)
  accepts an optional `date` like `Nov. 2, 2026` to build one episode.
- **`.github/workflows/mfm-send.yml`**: Wed/Thu/Fri 7:00 AM ET -- `send_email.py`.
- **State between runs** lives in the committed `state/` dir
  (`last_sent_episode.json`, `scraped_episode.json`, `email_body.html`,
  `last_built_episode.json`). Each job copies `state/` -> `.tmp/` before
  running and commits it back after, along with `episode_summaries/`. The
  two workflows share a concurrency group so they never touch state at once.
  Local runs still use `.tmp/` and do NOT update `state/` -- the repo is
  the source of truth for the schedule.
- **Timezone**: GitHub cron is UTC-only, so each workflow lists both the EDT
  and EST UTC hour; an "ET gate" step skips whichever trigger isn't the
  right New York time today. GitHub can start scheduled runs 5-30 min late.
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
   - Compares the episodes index against `.tmp/last_sent_episode.json` to
     find episodes newer than the last one successfully sent.
   - Writes `.tmp/scraped_episode.json` (a list; empty list = nothing new;
     each episode includes its `audio_url`, pulled from the page's
     `og:audio` meta tag).
2. `python3 tools/transcribe_episode.py`
   - Downloads the newest episode's audio, splits it into ~25-min chunks
     (re-encoded to 64kbps mono so each chunk stays under Whisper's 25MB
     upload limit regardless of source bitrate), transcribes each chunk via
     OpenAI Whisper, and stitches the per-chunk timestamps into one
     episode-wide timeline.
   - Writes `.tmp/transcript.json` (list of `{start, end, text}` segments).
3. `python3 tools/summarize_episode.py`
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
   - Writes `episode_summaries/<jun_24_2026>.json` (filename = the episode's
     published date, parsed from its show-notes date text, lowercased --
     e.g. "June 24, 2026" -> `jun_24_2026.json`). This directory lives at the
     project root, **not** in `.tmp` -- it's permanent, one file per episode,
     never overwritten or cleared.
4. `python3 tools/build_email.py`
   - Reads `.tmp/scraped_episode.json` + the newest episode's
     `episode_summaries/<jun_24_2026>.json` + `EMAIL_HEADER_IMAGE_URL`.
   - Writes `.tmp/email_body.html`.
   - Exits 1 if there were no new episodes, or if the newest episode's
     summary file is missing — **stop here, do not send**.
5. `python3 tools/send_email.py`
   - Sends `.tmp/email_body.html` to every address in
     `tools/data/subscribers.csv`, one email per subscriber.
   - Only updates `.tmp/last_sent_episode.json` if every send succeeds, so a
     partial failure can be safely retried without double-sending to anyone
     who already got it.

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
- **scrape** (before Whisper): newest episode has an `audio_url`, show
  notes, and at least one chapter timestamp.
- **transcribe** (before gpt-4o): every Whisper chunk returned segments and
  the transcript runs past the last chapter's start time.
- **summarize**: refuses to run with no chapters. Output must have a
  non-empty summary for EVERY chapter plus non-empty key takeaways; a bad
  result goes to `.tmp/rejected_summary.json`, never `episode_summaries/`.
  The batched call sometimes silently drops a chapter (seen Nov 2026 on a
  chapter 25s after the previous one, and one inside a long segment), so
  missing chapters are automatically re-requested on their own (a few
  cents). `summarize_episode.py --repair` re-uses the rejected file and
  only fills what's missing -- use it instead of a full paid re-run.
- **build**: same summary check, plus the header image URL must return
  HTTP 200 with an image content-type.
- **send**: no send unless the episode's summary file has both non-empty
  `chapter_summaries` and `key_takeaways`; no send if it's the episode in
  `last_sent_episode.json` (pass `--resend` to override deliberately).

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
- **Manual one-episode run**: `scrape_show_notes.py --date "Nov. 2, 2026"`
  scrapes exactly that episode, bypassing new-episode detection.
- **No new episode since last run**: `scrape_show_notes.py` writes an empty
  list; `build_email.py` exits 1; the scheduled agent should treat this as a
  normal no-op, not an error.
- **Site structure changes / scrape parse failure**: `scrape_show_notes.py`
  raises if `#show-notes .post-content-body` isn't found on an episode page —
  investigate the site's HTML before re-running rather than retrying blindly.
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
- **No published timestamps found**: if an episode's show notes have no
  `(mm:ss)` timestamps, `summarize_episode.py` writes empty
  `chapter_summaries`/`key_takeaways` rather than guessing chapter
  boundaries itself.

## Tools reference
- `tools/scrape_show_notes.py`
- `tools/transcribe_episode.py`
- `tools/summarize_episode.py`
- `tools/build_email.py`
- `tools/send_email.py`
- `tools/upload_header_image.py` (one-time only)
