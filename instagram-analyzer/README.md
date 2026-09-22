# Instagram Post Analyzer

A local tool that takes an Instagram post, carousel, or Reel URL and asks Gemini to synthesize the whole thing — every carousel image, or the full Reel video (visuals, on-screen text, and audio) — plus the caption, into one cohesive summary. Built to skip paid scraping APIs entirely.

**Want to run this somewhere other than your own machine?** See [DEPLOY.md](./DEPLOY.md) for deploying it to Render as a private, password-protected web app.

## Stack

- **FastAPI** — local API server
- **Instaloader** — free, open-source Instagram scraping (no Apify credits, no per-run cost)
- **Google Gemini** (`gemini-2.5-flash`) — reads the images and/or video + caption together and writes the synthesis. Video is uploaded through Gemini's File API so Gemini watches the actual clip, not just a cover frame.

## Setup

1. `cd` into this folder.
2. Copy `.env.example` to `.env` and fill in:
   - `GEMINI_API_KEY` — from [aistudio.google.com](https://aistudio.google.com)
   - `INSTAGRAM_USERNAME` / `INSTAGRAM_PASSWORD` — optional but recommended (Instagram increasingly requires login even for public posts). **Use a throwaway/secondary account, not your main one** — there's always some risk of an automated login getting flagged.
   - `ANALYZER_USERNAME` / `ANALYZER_PASSWORD` — leave both blank for local use (no login prompt, same as before). Set both before hosting this anywhere reachable from the open internet — see [DEPLOY.md](./DEPLOY.md).
3. Run `./start.sh`. First run creates a virtual environment and installs dependencies; every run after that just starts the server.

## Running it

```
./start.sh
```

Starts the server at `http://127.0.0.1:8000` with auto-reload. Leave the terminal window open while using it.

## Usage

**Browser (no terminal commands needed):** with the server running, open `http://127.0.0.1:8000` in a browser, paste the post/carousel/Reel URL, click Analyze.

**curl, if you'd rather:**

```
curl -X POST "http://127.0.0.1:8000/analyze" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://www.instagram.com/reel/SHORTCODE/"}'
```

Response shape:

```json
{
  "status": "success",
  "summary": "...",
  "total_slides_processed": 1,
  "image_count": 0,
  "video_count": 1,
  "content_type": "reel",
  "likes": 3,
  "post_date": "2026-07-28T18:56:14",
  "creator": "technology",
  "canonical_url": "https://www.instagram.com/reel/SHORTCODE/"
}
```

`content_type` is one of `single_image`, `carousel`, `reel`, or `carousel_with_video` (a carousel with one or more video slides mixed in).

## How it works

1. Extract the post shortcode from the URL (handles `/p/`, `/reel/`, `/reels/`, and `/tv/` links).
2. Instaloader logs in (session cached to `.ig_session` so it doesn't re-authenticate on every request) and fetches the post's metadata.
3. Sort the post's media into images vs. actual video URLs — a Reel's real video, or any video slide inside a carousel, not its thumbnail.
4. Images are downloaded and sent inline to Gemini. Videos are downloaded to a temp file and uploaded through Gemini's File API (handles larger files reliably), then the request waits for Gemini to finish processing before continuing.
5. Send everything — images, video(s), and caption — to Gemini with a synthesis prompt telling it to watch/read the full video (visuals + on-screen text + spoken audio) and every image, and write one cohesive narrative, not a slide-by-slide or frame-by-frame breakdown.
6. Return JSON with the summary, media counts, content type, and basic engagement metadata (likes, post date). Temp video files and the Gemini-side uploaded copy are deleted right after, win or lose.

## Save to Content Radar

After analyzing a post, the page shows a Title field (prefilled from the creator handle and content type) and a Tier dropdown (Core Signal / High Relevance / Useful Context / Background Noise / Skip — the same five tiers as `01 - Me/Content Radar/_Content Radar Index.md`). Clicking **Save to Content Radar**:

1. Writes a new note to `01 - Me/Content Radar/Entries/{Title}.md` with the standard frontmatter (`source`, `format`, `creator`, `url`, `date-captured`, `tier`) and the raw Gemini summary under "What it actually says." `format` records whether it came from a photo, carousel, Reel, or carousel-with-video.
2. Appends a row to `01 - Me/Content Radar/_Inbox.md` (created on first save) so it surfaces for the next batch review.

It deliberately does **not** write the "Why it matters" / "Action plan" sections or touch the curated `_Content Radar Index.md` — those require cross-referencing the Revenue Tracker and 12-Week Goals, which is the batch-review step Malik or Claude does by hand. Auto-saved entries are marked `auto-captured` in their tags and left as stubs until that pass happens.

By default this writes into `~/Documents/Obsidian Vault`. Set `VAULT_PATH` in `.env` if the vault lives somewhere else. Saving fails with a clear error if the Content Radar folder isn't found, or if a file with that title already exists (pick a different title rather than overwrite).

## Saving results (anyone else running this)

"Save to Content Radar" above is specific to this vault's folder structure and only appears when the app can see it (checked via `/config`) -- it won't show up for anyone else running this tool. Everyone gets three vault-agnostic options instead, all in the box under each result:

- **Copy** -- copies the result as Markdown (with frontmatter) to the clipboard. No setup.
- **Download .md** -- same content, saved as a file. No setup.
- **Send to Webhook** -- paste any URL (a Zapier/Make step into Notion, a personal script, whatever) and it POSTs the result there as JSON. The URL is remembered in your own browser only (`localStorage`) -- it's never written to this server or this repo.

## Known quirks

- **"login_required" / "Unable to fetch high quality image version" warnings in the server log are normal and non-fatal.** Instaloader tries an extra call for an ultra-high-res image variant that needs deeper mobile-app auth, that call fails, and it automatically falls back to the standard resolution it already has. The request still completes and returns 200.
- **Reels now take noticeably longer than a photo post** — the video has to download from Instagram's CDN, then upload to Gemini's File API, then Gemini has to finish processing it before the synthesis call can run. Expect several seconds to ~30s+ depending on video length/size, versus 1-2s for a photo carousel. The button/status text reflects this.
- First request after starting the server takes a bit longer (login + session setup).
- **"Error: Gemini failed to process the video file." on a Reel/video, with a clean 200 download** — this is Gemini's video-processing backend failing internally (`error.code=13`, "The file failed to be processed."), not the app or the video. Confirmed 2026-09-10: reproduced by uploading the exact downloaded file directly to Gemini's File API (bypassing this app), and by uploading a totally unrelated synthetic test video — both failed the same way, while a plain image upload through the same File API worked instantly. Photo/carousel posts are unaffected (images go to Gemini inline, not through the File API). It's been intermittent, not a hard outage — the same day it failed repeatedly it also succeeded on a different carousel+video post. **If this happens: just retry, possibly a few minutes later.** If it's still failing after several retries across different posts, test the same video file directly at aistudio.google.com (upload it there) — if it fails there too, it's confirmed fully on Google's side and worth checking that project's quota/billing rather than anything in this repo.
- **"Request failed: SyntaxError: Unexpected token 'I', \"Internal S\"... is not valid JSON" in the browser** — this meant the server crashed with an *unhandled* exception somewhere in the pipeline (not one of the app's own `HTTPException`s), so FastAPI's default error page (`Internal Server Error`, plain text) went back instead of JSON, and the frontend's `res.json()` choked trying to parse it. Fixed 2026-09-10: added a catch-all exception handler (see `unhandled_exception_handler` right after `app = FastAPI(...)`) so any future unexpected exception -- a Gemini SDK call throwing mid-poll, a new Instaloader exception type, anything -- always comes back as `{"detail": "Unexpected server error: ..."}` with a real status code, so you'll see the actual error message in the box instead of a cryptic JS parse error. Verified with FastAPI's TestClient by forcing a raw `RuntimeError` deep in `/analyze` and confirming the response is now valid JSON. This doesn't fix whatever transient thing triggers the underlying exception (that could be anything, including plain network flakiness) -- it just makes sure you can always read what happened instead of a dead end.

## Error responses

| Code | Meaning |
|---|---|
| 401 | Instagram is requiring login — add credentials to `.env` |
| 404 | Account doesn't exist |
| 429 | Rate-limited by Instagram — wait and retry |
| 500 | Instaloader or Gemini error — see the `detail` message. If the message is "Gemini failed to process the video file," see the Known Quirks entry above — it's Gemini's video backend, intermittent, just retry. |
| 504 | Gemini took too long to finish processing an uploaded video — retry |

## Security

- `.env` holds real credentials and is git-ignored — never commit it.
- `.env.example` is the safe template that's actually tracked in the repo.
- Use a secondary/throwaway Instagram account for login, not your primary one.

## Legal note

Scraping Instagram content runs against their Terms of Service. This is built for personal research and analysis, not for public-facing or commercial use.

## Files

- `app.py` — FastAPI app and full pipeline
- `requirements.txt` — Python dependencies
- `start.sh` — one-command setup + run script
- `.env` / `.env.example` — configuration
