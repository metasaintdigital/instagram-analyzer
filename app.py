import ipaddress
import os
import re
import secrets
import socket
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
import instaloader
from instaloader.exceptions import (
    InstaloaderException,
    ProfileNotExistsException,
    LoginRequiredException,
    ConnectionException,
)
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
IG_USERNAME = os.getenv("INSTAGRAM_USERNAME")  # optional, improves reliability
IG_PASSWORD = os.getenv("INSTAGRAM_PASSWORD")  # optional
SESSION_FILE = Path(".ig_session")

# Gate the whole app behind HTTP Basic Auth when hosted publicly. Leave both
# unset for local-only use (matches old behavior, no login prompt). Set both
# before deploying anywhere reachable from the open internet -- this app
# spends your Gemini quota and logs into Instagram on every /analyze call.
ANALYZER_USERNAME = os.getenv("ANALYZER_USERNAME")
ANALYZER_PASSWORD = os.getenv("ANALYZER_PASSWORD")
_basic_auth = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    """No-op if ANALYZER_USERNAME/PASSWORD aren't set (local dev, unchanged
    behavior). Once both are set, every gated route requires them via the
    browser's built-in Basic Auth prompt."""
    if not ANALYZER_USERNAME or not ANALYZER_PASSWORD:
        return
    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, ANALYZER_USERNAME)
        and secrets.compare_digest(credentials.password, ANALYZER_PASSWORD)
    )
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )

# Where the Obsidian vault lives, so "Save to Content Radar" can write straight
# into it. Override VAULT_PATH in .env if the vault ever moves.
VAULT_PATH = Path(os.getenv("VAULT_PATH", str(Path.home() / "Documents" / "Obsidian Vault")))
RADAR_DIR = VAULT_PATH / "01 - Me" / "Content Radar"
ENTRIES_DIR = RADAR_DIR / "Entries"
INBOX_FILE = RADAR_DIR / "_Inbox.md"

RADAR_TIERS = [
    "Core Signal",
    "High Relevance",
    "Useful Context",
    "Background Noise",
    "Skip",
]

# Reels/videos over this size skip the Gemini File API's synchronous "wait for
# ACTIVE" loop being worth it inline — still processed, just may take longer.
VIDEO_DOWNLOAD_TIMEOUT = 60
GEMINI_FILE_PROCESSING_TIMEOUT = 90  # seconds to wait for Gemini to finish processing an uploaded video

if not GEMINI_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")

gemini_client = genai.Client(api_key=GEMINI_KEY)

app = FastAPI(title="Instagram Post Analyzer (Instaloader + Gemini)")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all so a bug or transient failure anywhere in the pipeline (a
    Gemini SDK call throwing mid-poll, a library exception type we didn't
    anticipate, etc.) always comes back as JSON the frontend can parse and
    show, instead of Starlette's default plain-text "Internal Server Error"
    -- which the browser's fetch().json() can't parse at all, and which used
    to surface in the UI as a cryptic "SyntaxError: Unexpected token" instead
    of the real problem. This does not affect HTTPException responses raised
    on purpose elsewhere in the app (401/404/429/500/504 with a real detail
    message) -- FastAPI still routes those to its own more specific handler
    first; this only catches what nothing else caught."""
    return JSONResponse(status_code=500, content={"detail": f"Unexpected server error: {exc}"})

SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")

# A normal browser UA helps avoid 403s when pulling image/video bytes off the CDN.
MEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def get_loader() -> instaloader.Instaloader:
    """
    Build an Instaloader instance. Logging in (optional, via env vars) makes
    fetches much more reliable — Instagram increasingly rate-limits or blocks
    anonymous requests. A session file is cached to disk so we don't log in
    on every single request (repeated logins can trigger a security check).
    """
    L = instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
    )

    if IG_USERNAME and IG_PASSWORD:
        if SESSION_FILE.exists():
            try:
                L.load_session_from_file(IG_USERNAME, str(SESSION_FILE))
            except Exception:
                L.login(IG_USERNAME, IG_PASSWORD)
                L.save_session_to_file(str(SESSION_FILE))
        else:
            L.login(IG_USERNAME, IG_PASSWORD)
            L.save_session_to_file(str(SESSION_FILE))

    return L


def extract_shortcode(url: str) -> str:
    match = SHORTCODE_RE.search(url)
    if not match:
        raise HTTPException(
            status_code=400,
            detail="Could not find a post/reel shortcode in that URL.",
        )
    return match.group(1)


class PostRequest(BaseModel):
    url: str


class SaveRequest(BaseModel):
    title: str
    tier: str
    creator: str | None = None
    url: str
    summary: str
    likes: int | None = None
    post_date: str | None = None
    total_slides_processed: int | None = None
    content_type: str | None = None


class WebhookSaveRequest(BaseModel):
    """Generic save path for anyone running this tool -- not just the Obsidian
    vault it was originally built around. Posts the result as JSON to any
    URL the caller supplies (a Zapier/Make step into Notion, a personal
    script, etc)."""
    webhook_url: str
    title: str
    creator: str | None = None
    url: str
    summary: str
    likes: int | None = None
    post_date: str | None = None
    total_slides_processed: int | None = None
    content_type: str | None = None


def _is_safe_webhook_url(url: str) -> bool:
    """Basic SSRF guard. This endpoint lets any authenticated user of this
    shared instance make the server issue an outbound POST, so it shouldn't
    be usable to reach internal/private network addresses -- only public
    http(s) hosts are allowed."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def safe_filename(title: str) -> str:
    """Strip filesystem-unsafe characters, keep it otherwise readable (matches
    the existing 'Creator — Topic' naming convention already used in Entries/)."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "", title).strip()
    return cleaned or "Untitled Content Radar Entry"


def download_to_temp(url: str, suffix: str) -> str:
    """Stream a remote file to a temp path on disk. Used for video, which is
    too large/unreliable to hold fully in memory or send inline to Gemini."""
    resp = requests.get(url, headers=MEDIA_HEADERS, timeout=VIDEO_DOWNLOAD_TIMEOUT, stream=True)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if chunk:
                tmp.write(chunk)
    finally:
        tmp.close()
    return tmp.name


def upload_video_to_gemini(path: str):
    """Upload a local video file to Gemini's File API and block until it's
    ACTIVE (ready to be referenced in a generate_content call). Gemini reads
    the video natively — visual frames, on-screen text, AND the audio track —
    which is what makes real Reel analysis possible instead of just a cover
    thumbnail."""
    uploaded = gemini_client.files.upload(file=path, config={"mime_type": "video/mp4"})
    start = time.time()
    while uploaded.state.name == "PROCESSING":
        if time.time() - start > GEMINI_FILE_PROCESSING_TIMEOUT:
            raise HTTPException(
                status_code=504,
                detail="Gemini took too long to process the video. Try again in a moment.",
            )
        time.sleep(2)
        uploaded = gemini_client.files.get(name=uploaded.name)

    if uploaded.state.name == "FAILED":
        raise HTTPException(status_code=500, detail="Gemini failed to process the video file.")

    return uploaded


PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Instagram Post Analyzer</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0A0A0A; color: #F5F5F0; max-width: 640px; margin: 60px auto; padding: 0 20px; }
  h1 { font-size: 20px; font-weight: 600; margin-bottom: 4px; }
  p.sub { color: #999; margin-top: 0; font-size: 14px; }
  label { font-size: 13px; color: #999; display: block; margin: 14px 0 6px; }
  input[type=text], select { width: 100%; box-sizing: border-box; padding: 12px 14px; font-size: 15px;
         border-radius: 8px; border: 1px solid #333; background: #1a1a1a; color: #F5F5F0; }
  button { margin-top: 12px; padding: 10px 20px; font-size: 14px; font-weight: 600;
         border-radius: 8px; border: none; background: #D4AF37; color: #0A0A0A; cursor: pointer; }
  button.secondary { background: #262626; color: #F5F5F0; border: 1px solid #333; }
  button:disabled { opacity: 0.5; cursor: default; }
  #result { margin-top: 28px; padding: 18px; border-radius: 10px; background: #151515;
         border: 1px solid #262626; display: none; white-space: pre-wrap; line-height: 1.5; }
  #result.show { display: block; }
  #saveBox { display: none; margin-top: 18px; padding: 18px; border-radius: 10px;
         background: #151515; border: 1px solid #262626; }
  #exportBox { display: none; margin-top: 18px; padding: 18px; border-radius: 10px;
         background: #151515; border: 1px solid #262626; }
  #exportBox button { margin-right: 8px; }
  .hint { color: #666; font-size: 12px; margin: 4px 0 0; }
  .meta { color: #999; font-size: 13px; margin-bottom: 10px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #262626;
         color: #D4AF37; font-size: 11px; font-weight: 600; text-transform: uppercase;
         letter-spacing: 0.03em; margin-right: 6px; }
  .error { color: #ff6b6b; }
  .success { color: #7CFC9E; }
  a { color: #D4AF37; }
</style>
</head>
<body>
  <h1>Instagram Post Analyzer</h1>
  <p class="sub">Paste a post, carousel, or Reel URL. No curl needed.</p>
  <input type="text" id="url" placeholder="https://www.instagram.com/p/... or /reel/..." />
  <br>
  <button id="go" onclick="analyze()">Analyze</button>
  <div id="result"></div>

  <div id="saveBox">
    <label for="title">Title (used as the filename in Content Radar)</label>
    <input type="text" id="title" />
    <label for="tier">Tier</label>
    <select id="tier">
      <option>Core Signal</option>
      <option>High Relevance</option>
      <option selected>Useful Context</option>
      <option>Background Noise</option>
      <option>Skip</option>
    </select>
    <button id="saveBtn" onclick="saveToRadar()">Save to Content Radar</button>
    <div id="saveStatus"></div>
  </div>

  <div id="exportBox">
    <label for="webhookUrl">Webhook URL (optional)</label>
    <input type="text" id="webhookUrl" placeholder="https://hooks.zapier.com/... or your own endpoint" />
    <p class="hint">Works for anyone running this tool, no vault needed. Copy/download need no setup at all; the webhook sends this result as JSON wherever you point it (Zapier/Make into Notion, a personal script, etc) -- saved in this browser only.</p>
    <button class="secondary" onclick="copyResult()">Copy</button>
    <button class="secondary" onclick="downloadMarkdown()">Download .md</button>
    <button id="webhookBtn" onclick="sendToWebhook()">Send to Webhook</button>
    <div id="exportStatus"></div>
  </div>

<script>
let lastResult = null;
let lastUrl = null;
let vaultUnavailable = false;

(async function checkConfig() {
  try {
    const res = await fetch('/config');
    if (res.ok) {
      const cfg = await res.json();
      vaultUnavailable = !cfg.vault_available;
    }
  } catch (e) {}
})();

(function initWebhookField() {
  try {
    const saved = localStorage.getItem('webhookUrl');
    if (saved) document.getElementById('webhookUrl').value = saved;
  } catch (e) {}
  document.getElementById('webhookUrl').addEventListener('change', function() {
    try { localStorage.setItem('webhookUrl', this.value.trim()); } catch (e) {}
  });
})();

const CONTENT_TYPE_LABELS = {
  reel: 'Reel',
  carousel: 'Carousel',
  carousel_with_video: 'Carousel + Video',
  single_image: 'Photo'
};

async function analyze() {
  const url = document.getElementById('url').value.trim();
  const btn = document.getElementById('go');
  const box = document.getElementById('result');
  const saveBox = document.getElementById('saveBox');
  if (!url) return;
  btn.disabled = true;
  btn.textContent = 'Analyzing...';
  box.className = 'show';
  saveBox.style.display = 'none';
  box.innerHTML = 'Fetching post + running synthesis, this can take a bit longer for Reels (full video, not just a thumbnail)...';
  try {
    const res = await fetch('/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    if (!res.ok) {
      box.innerHTML = '<span class="error">Error: ' + (data.detail || res.status) + '</span>';
    } else {
      lastResult = data;
      lastUrl = data.canonical_url || url;
      const typeLabel = CONTENT_TYPE_LABELS[data.content_type] || '';
      const mediaBits = [];
      if (data.image_count) mediaBits.push(data.image_count + ' image(s)');
      if (data.video_count) mediaBits.push(data.video_count + ' video(s)');
      box.innerHTML =
        '<div class="meta">' + (typeLabel ? '<span class="badge">' + typeLabel + '</span>' : '') +
        '@' + (data.creator || 'unknown') + ' &middot; ' +
        (mediaBits.join(', ') || (data.total_slides_processed + ' slide(s)')) + ' &middot; ' +
        (data.likes ?? '?') + ' likes &middot; ' + (data.post_date || '') +
        '<br><a href="' + lastUrl + '" target="_blank">' + lastUrl + '</a></div>' +
        data.summary;
      const creatorPart = data.creator ? (data.creator.charAt(0).toUpperCase() + data.creator.slice(1)) : 'Unknown';
      document.getElementById('title').value = creatorPart + ' — Instagram ' + (typeLabel || 'Post');
      saveBox.style.display = 'block';
      document.querySelectorAll('#saveBox input, #saveBox select, #saveBox button').forEach(el => el.disabled = vaultUnavailable);
      document.getElementById('saveStatus').innerHTML = vaultUnavailable
        ? '<span class="error">Content Radar save only works on the local copy (needs your Mac\\'s vault mounted).</span>'
        : '';
      document.getElementById('exportBox').style.display = 'block';
      document.getElementById('exportStatus').innerHTML = '';
    }
  } catch (e) {
    box.innerHTML = '<span class="error">Request failed: ' + e + '</span>';
  }
  btn.disabled = false;
  btn.textContent = 'Analyze';
}

async function saveToRadar() {
  if (!lastResult) return;
  const btn = document.getElementById('saveBtn');
  const status = document.getElementById('saveStatus');
  const title = document.getElementById('title').value.trim();
  const tier = document.getElementById('tier').value;
  if (!title) { status.innerHTML = '<span class="error">Title required.</span>'; return; }
  btn.disabled = true;
  btn.textContent = 'Saving...';
  status.innerHTML = '';
  try {
    const res = await fetch('/save-to-radar', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        title: title,
        tier: tier,
        creator: lastResult.creator,
        url: lastUrl,
        summary: lastResult.summary,
        likes: lastResult.likes,
        post_date: lastResult.post_date,
        total_slides_processed: lastResult.total_slides_processed,
        content_type: lastResult.content_type
      })
    });
    const data = await res.json();
    if (!res.ok) {
      status.innerHTML = '<span class="error">Error: ' + (data.detail || res.status) + '</span>';
    } else {
      status.innerHTML = '<span class="success">Saved to ' + data.path + '</span>';
    }
  } catch (e) {
    status.innerHTML = '<span class="error">Request failed: ' + e + '</span>';
  }
  btn.disabled = false;
  btn.textContent = 'Save to Content Radar';
}

function exportTitle() {
  const t = document.getElementById('title').value.trim();
  if (t) return t;
  const creatorPart = lastResult && lastResult.creator
    ? (lastResult.creator.charAt(0).toUpperCase() + lastResult.creator.slice(1)) : 'Unknown';
  const typeLabel = lastResult ? (CONTENT_TYPE_LABELS[lastResult.content_type] || 'Post') : 'Post';
  return creatorPart + ' — Instagram ' + typeLabel;
}

function resultAsMarkdown() {
  const today = new Date().toISOString().slice(0, 10);
  const creator = lastResult.creator ? ('"@' + lastResult.creator + '"') : '"unknown"';
  return '---\\n' +
    'source: instagram\\n' +
    'creator: ' + creator + '\\n' +
    'url: ' + (lastUrl || '') + '\\n' +
    'date-captured: ' + today + '\\n' +
    'content-type: ' + (lastResult.content_type || 'unknown') + '\\n' +
    '---\\n\\n' +
    '# ' + exportTitle() + '\\n\\n' +
    lastResult.summary + '\\n';
}

async function copyResult() {
  if (!lastResult) return;
  const status = document.getElementById('exportStatus');
  try {
    await navigator.clipboard.writeText(resultAsMarkdown());
    status.innerHTML = '<span class="success">Copied.</span>';
  } catch (e) {
    status.innerHTML = '<span class="error">Copy failed: ' + e + '</span>';
  }
}

function downloadMarkdown() {
  if (!lastResult) return;
  const blob = new Blob([resultAsMarkdown()], {type: 'text/markdown'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = exportTitle().replace(/[\\/:*?"<>|]/g, '') + '.md';
  document.body.appendChild(a);
  a.click();
  a.remove();
  document.getElementById('exportStatus').innerHTML = '<span class="success">Downloaded.</span>';
}

async function sendToWebhook() {
  if (!lastResult) return;
  const webhookUrl = document.getElementById('webhookUrl').value.trim();
  const status = document.getElementById('exportStatus');
  if (!webhookUrl) { status.innerHTML = '<span class="error">Enter a webhook URL first.</span>'; return; }
  try { localStorage.setItem('webhookUrl', webhookUrl); } catch (e) {}
  const btn = document.getElementById('webhookBtn');
  btn.disabled = true;
  btn.textContent = 'Sending...';
  status.innerHTML = '';
  try {
    const res = await fetch('/save-to-webhook', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        webhook_url: webhookUrl,
        title: exportTitle(),
        creator: lastResult.creator,
        url: lastUrl,
        summary: lastResult.summary,
        likes: lastResult.likes,
        post_date: lastResult.post_date,
        total_slides_processed: lastResult.total_slides_processed,
        content_type: lastResult.content_type
      })
    });
    const data = await res.json();
    if (!res.ok) {
      status.innerHTML = '<span class="error">Error: ' + (data.detail || res.status) + '</span>';
    } else {
      status.innerHTML = '<span class="success">Sent.</span>';
    }
  } catch (e) {
    status.innerHTML = '<span class="error">Request failed: ' + e + '</span>';
  }
  btn.disabled = false;
  btn.textContent = 'Send to Webhook';
}

document.getElementById('url').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') analyze();
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home(_: None = Depends(require_auth)):
    return PAGE


@app.get("/config")
def config(_: None = Depends(require_auth)):
    """Lets the frontend know whether the Obsidian vault is reachable from
    wherever this instance is running, so it can hide the Content Radar save
    step instead of letting it fail after the fact -- e.g. a hosted copy has
    no access to your Mac's vault."""
    return {"vault_available": ENTRIES_DIR.exists()}


@app.post("/save-to-radar")
def save_to_radar(payload: SaveRequest, _: None = Depends(require_auth)):
    if payload.tier not in RADAR_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Tier must be one of: {', '.join(RADAR_TIERS)}",
        )

    if not ENTRIES_DIR.exists():
        raise HTTPException(
            status_code=500,
            detail=(
                f"Content Radar Entries folder not found at {ENTRIES_DIR}. "
                "Check VAULT_PATH in .env if the vault lives somewhere else."
            ),
        )

    today = datetime.now().strftime("%Y-%m-%d")
    filename = safe_filename(payload.title)
    entry_path = ENTRIES_DIR / f"{filename}.md"

    if entry_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"An entry named '{filename}.md' already exists. Use a different title.",
        )

    creator_line = f'"@{payload.creator}"' if payload.creator else '"unknown"'
    format_line = payload.content_type or "unknown"

    entry_content = f"""---
tags: [content-radar, auto-captured]
source: instagram
format: {format_line}
creator: {creator_line}
url: {payload.url}
date-captured: {today}
tier: {payload.tier}
---

# {filename}

## What it actually says

{payload.summary}

## Why it matters, several angles

*Not yet reviewed, auto-captured via the Instagram Analyzer's "Save to Content Radar" button. Revisit in a batch pass, cross-reference against the Revenue Tracker and 12-Week Goals, before acting on this.*

## Action plan

*Pending batch review.*
"""
    entry_path.write_text(entry_content, encoding="utf-8")

    inbox_header = (
        "---\n"
        "tags: [content-radar, inbox]\n"
        "---\n\n"
        "# Content Radar — Inbox (Auto-Saved, Not Yet Batch-Reviewed)\n\n"
        "Entries land here via the Instagram Analyzer's \"Save to Content Radar\" button. "
        "Promote to `_Content Radar Index.md` (with the full Why-it-matters / Action plan "
        "writeup and revenue routing) during a batch review pass.\n\n"
        "| Date | Tier | Creator | Note |\n"
        "|---|---|---|---|\n"
    )
    row = f"| {today} | {payload.tier} | {payload.creator or 'unknown'} | [[01 - Me/Content Radar/Entries/{filename}]] |\n"

    if INBOX_FILE.exists():
        with INBOX_FILE.open("a", encoding="utf-8") as f:
            f.write(row)
    else:
        INBOX_FILE.write_text(inbox_header + row, encoding="utf-8")

    return {"status": "saved", "path": str(entry_path)}


@app.post("/save-to-webhook")
def save_to_webhook(payload: WebhookSaveRequest, _: None = Depends(require_auth)):
    """Generic save path: POSTs the analysis as JSON to any URL the caller
    provides. Runs server-side (not from the browser) so the target doesn't
    need to support CORS -- a plain webhook receiver is enough."""
    if not _is_safe_webhook_url(payload.webhook_url):
        raise HTTPException(
            status_code=400,
            detail="That webhook URL isn't allowed (must be a public http/https address).",
        )
    body = {
        "title": payload.title,
        "creator": payload.creator,
        "url": payload.url,
        "summary": payload.summary,
        "likes": payload.likes,
        "post_date": payload.post_date,
        "total_slides_processed": payload.total_slides_processed,
        "content_type": payload.content_type,
        "source": "instagram-post-analyzer",
    }
    try:
        resp = requests.post(payload.webhook_url, json=body, timeout=10)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Couldn't reach that webhook: {e}")
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Webhook responded with {resp.status_code}: {resp.text[:200]}",
        )
    return {"status": "sent", "webhook_status": resp.status_code}


@app.post("/analyze")
def analyze_instagram_post(payload: PostRequest, _: None = Depends(require_auth)):
    shortcode = extract_shortcode(payload.url)

    # -------------------------------------------------------------
    # 1. FETCH POST METADATA VIA INSTALOADER
    # -------------------------------------------------------------
    try:
        L = get_loader()
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except LoginRequiredException:
        raise HTTPException(
            status_code=401,
            detail=(
                "Instagram is requiring login to view this post. Set "
                "INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD in your .env and retry."
            ),
        )
    except ProfileNotExistsException:
        raise HTTPException(status_code=404, detail="That account doesn't exist.")
    except ConnectionException as e:
        raise HTTPException(status_code=429, detail=f"Instagram connection issue (likely rate-limited): {e}")
    except InstaloaderException as e:
        raise HTTPException(status_code=500, detail=f"Instaloader error: {e}")

    caption_text = post.caption or "No caption provided."

    # -------------------------------------------------------------
    # 2. COLLECT MEDIA URLS — separates images from actual video URLs so
    #    Reels (and any video slide inside a carousel) get the real video
    #    sent to Gemini, not just a cover-frame thumbnail.
    # -------------------------------------------------------------
    image_urls: list[str] = []
    video_urls: list[str] = []

    try:
        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                if getattr(node, "is_video", False) and getattr(node, "video_url", None):
                    video_urls.append(node.video_url)
                else:
                    image_urls.append(node.display_url)
        elif getattr(post, "is_video", False) and getattr(post, "video_url", None):
            # Standalone Reel/video post.
            video_urls.append(post.video_url)
        else:
            image_urls.append(post.url)
    except InstaloaderException as e:
        raise HTTPException(status_code=500, detail=f"Error reading post media: {e}")

    if not image_urls and not video_urls:
        raise HTTPException(status_code=400, detail="No image or video content found on this post.")

    if post.typename == "GraphSidecar":
        content_type = "carousel_with_video" if video_urls else "carousel"
    elif video_urls:
        content_type = "reel"
    else:
        content_type = "single_image"

    # -------------------------------------------------------------
    # 3. DOWNLOAD IMAGES (in memory — small, fast, sent inline to Gemini)
    # -------------------------------------------------------------
    image_parts = []
    for url in image_urls:
        try:
            res = requests.get(url, headers=MEDIA_HEADERS, timeout=10)
            if res.status_code == 200:
                image_parts.append(
                    types.Part.from_bytes(data=res.content, mime_type="image/jpeg")
                )
        except requests.RequestException:
            continue

    # -------------------------------------------------------------
    # 4. DOWNLOAD + UPLOAD VIDEOS (Reels / video carousel slides) — videos
    #    are downloaded to a temp file, then handed to Gemini's File API so
    #    Gemini watches the actual clip: visual frames, on-screen text, and
    #    the spoken/audio track, not just a still frame.
    # -------------------------------------------------------------
    video_parts = []
    temp_video_paths = []
    uploaded_gemini_files = []
    try:
        for url in video_urls:
            try:
                temp_path = download_to_temp(url, suffix=".mp4")
            except requests.RequestException:
                continue
            temp_video_paths.append(temp_path)
            uploaded = upload_video_to_gemini(temp_path)
            uploaded_gemini_files.append(uploaded)
            video_parts.append(uploaded)

        if not image_parts and not video_parts:
            raise HTTPException(status_code=400, detail="Failed to download post media.")

        # ---------------------------------------------------------
        # 5. GEMINI SYNTHESIS PROMPT
        # ---------------------------------------------------------
        media_bits = []
        if video_parts:
            media_bits.append(
                f"{len(video_parts)} video(s) — this is Reel/video content, so watch the "
                "full clip: on-screen visuals, any on-screen text, AND the spoken/audio "
                "narration throughout, not just the opening frame"
            )
        if image_parts:
            media_bits.append(f"{len(image_parts)} image(s)/carousel slide(s)")
        media_desc = " and ".join(media_bits)

        prompt = f"""
        You are analyzing an Instagram post containing {media_desc}, plus a caption, for
        someone who builds AI-driven marketing and content tooling for his own agency and
        wants to know if there's something here worth trying, reusing, or adapting for his
        own workflow.

        POST CAPTION:
        \"\"\"{caption_text}\"\"\"

        INSTRUCTIONS:
        Watch/read everything closely and literally -- every frame of video (visuals,
        on-screen text/UI, spoken audio) and every image, not just the general gist.
        Creators making this kind of content very often show their actual process on
        screen -- typing a specific prompt, entering specific settings, clicking through a
        specific sequence of steps, naming the exact tool/model they used -- sometimes
        while the caption or voiceover stays vague. That on-screen detail is the entire
        point of this analysis: catch it precisely, don't paraphrase past it.

        If this post is about a specific AI tool, app, technique, or workflow (true for
        most posts like this), structure your answer with these labeled fields, each on
        its own line, skipping any field the post gives no real information for:

        Tool/Technique: exact name as given, and the creator/company if named
        What it does: the actual input -> output mechanic, as specifically as the post
          shows or claims -- not marketing language
        How to access it: open-source/GitHub repo, paid product, waitlist, API, browser
          extension, etc, and the exact link or handle if visible anywhere in the images,
          video, or caption
        How it works technically: any real detail shown or claimed about the underlying
          approach (model used, pipeline steps, integrations) -- omit this field entirely
          if the post gives no real technical detail beyond "it's AI"
        Steps shown, if any: if the video/images actually show someone DOING the
          technique -- typing, clicking, adjusting a setting, following a sequence --
          write out the literal steps in order, numbered, specific enough that someone
          could copy them: the exact prompt text typed (word for word if it's legible
          on screen), the exact setting/parameter values shown, the exact sequence of
          clicks/menus/tools used, in the order they happened. This is the field to get
          right over any other -- prioritize catching every visible/audible action over
          writing clean prose. If the post only talks/announces with no actual procedure
          shown on screen, write "No procedure demonstrated on screen" here rather than
          inventing steps or restating the caption.
        Worth noting: any claim, number, limitation, or caveat worth remembering
          (engagement numbers, "still in beta," a specific example used in the demo, a
          stated limitation)

        Then, underneath, a short 2-3 sentence narrative for context: what's actually
        happening in the post and why it's circulating.

        If the post is NOT about a specific tool or technique (personal content, pure
        commentary, nothing reusable), skip the structured fields entirely and just write
        a clean 1-2 paragraph synthesis instead.

        Do not repeat the post's own marketing language ("game-changer," "revolutionary")
        unless you are directly quoting it and labeling it as the post's own claim.
        """

        # ---------------------------------------------------------
        # 6. GENERATE RESPONSE
        # ---------------------------------------------------------
        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",  # check aistudio.google.com for the current default if this errors
                contents=[*image_parts, *video_parts, prompt],
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"AI processing error: {str(e)}")

        return {
            "status": "success",
            "summary": response.text,
            "total_slides_processed": len(image_parts) + len(video_parts),
            "image_count": len(image_parts),
            "video_count": len(video_parts),
            "content_type": content_type,
            "likes": getattr(post, "likes", None),
            "post_date": post.date_utc.isoformat() if post.date_utc else None,
            "creator": getattr(post, "owner_username", None),
            "canonical_url": f"https://www.instagram.com/p/{shortcode}/",
        }
    finally:
        # Clean up local temp files and remote Gemini file uploads either way,
        # so a failed run doesn't leave disk clutter or orphaned Gemini files.
        for path in temp_video_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        for f in uploaded_gemini_files:
            try:
                gemini_client.files.delete(name=f.name)
            except Exception:
                pass


# Run server with: uvicorn app:app --reload
