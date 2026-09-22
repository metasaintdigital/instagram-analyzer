# Deploying to Render

This turns the local tool into a live URL you (or anyone you share it with) can hit from a
browser — no need to keep your Mac running or the terminal window open.

## What changes when it's hosted

- **Basic Auth is required.** Set `ANALYZER_USERNAME` / `ANALYZER_PASSWORD` and every route
  asks for that login via the browser's built-in prompt. Without them, this would be an open
  endpoint on the public internet that spends your Gemini quota and logs into Instagram for
  anyone who finds the URL.
- **"Save to Content Radar" won't work from the hosted copy.** It writes directly into your
  Obsidian vault, which lives on your Mac — a Render server has no way to reach it. The page
  detects this automatically (`/config` reports `vault_available: false`) and disables that
  button with an explanatory note. Use the local copy when you want to save entries.
- **The filesystem is ephemeral.** Render's free tier wipes local disk on every restart/redeploy,
  so `.ig_session` (the cached Instagram login) gets regenerated on the first request after a
  restart rather than persisting. Not a problem functionally, just means an occasional extra
  login — another reason to use a throwaway IG account, not your main one.
- **Free tier spins down after 15 minutes idle** and takes ~30-60s to wake back up on the next
  request. Fine for an on-demand personal tool; if that wait ever gets annoying, Render's paid
  "Starter" tier ($7/mo) keeps it always-on.

## One-time setup

1. **Push this folder to its own GitHub repo** (separate from `eln-command-center` — a hosting
   platform only needs this one app, not your whole agency codebase). This repo does not need
   to be public; Render can deploy from a private repo once it has access.

2. **Deploy via Render Blueprint** (reads `render.yaml` in this folder automatically):
   - Go to `https://dashboard.render.com/select-repo?type=blueprint`
   - Connect your GitHub account if you haven't, and pick the new repo
   - Render shows the `instagram-analyzer` service it found and a form for every secret
     (`GEMINI_API_KEY`, `INSTAGRAM_USERNAME`, `INSTAGRAM_PASSWORD`, `ANALYZER_USERNAME`,
     `ANALYZER_PASSWORD`) — fill these in with the same values from your local `.env`
     (pick a *new* username/password for `ANALYZER_USERNAME`/`ANALYZER_PASSWORD`, this isn't
     tied to anything else)
   - Click **Apply** — first deploy takes a few minutes
   - Render may ask you to add a card for verification before it will create the free service;
     it will not charge you unless you exceed the free tier's 750 instance-hours/month

3. **Open the URL Render gives you** (something like `https://instagram-analyzer-xxxx.onrender.com`).
   Your browser will prompt for the username/password you set for `ANALYZER_USERNAME`/`PASSWORD`.

## Rotating or revoking access

- To change the login, edit `ANALYZER_USERNAME`/`ANALYZER_PASSWORD` in the service's
  **Environment** tab on Render and save — it redeploys automatically.
- To pull it offline, suspend or delete the service from the Render dashboard.

## Costs to expect

- **Render free tier:** $0, 750 instance-hours/month (effectively unlimited for an
  on-demand personal tool that sleeps when idle), ephemeral disk, spins down after 15 min idle.
- **Gemini API:** pay-as-you-go on your own Google AI Studio key, same as running it locally —
  hosting doesn't change this cost, it's per-analysis either way.
