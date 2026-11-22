# FreeMediaTools Downloader Backend

Small FastAPI + yt-dlp backend for FreeMediaTools
(https://freemediatools.netlify.app/). The static site alone cannot
download platform videos reliably — Invidious returns 502 and the
Cobalt API now requires JWT auth. This server does the real work:
it fetches a video with yt-dlp and streams a merged MP4 / MP3 back.

## Why it is fast — and why it tries two YouTube clients

YouTube's Android player client is fast (~3 s) but, since YouTube's
SABR experiment, it often returns **only progressive itag 18
(360p)** — stderr says some Android formats "have been skipped as
they are missing a URL". The default web client returned the **full**
format list in ~5 s on 2026-11-22 (it had been ~77 s / timeouts
before, so it is intermittent). The app therefore tries Android
first and falls back to the web client for `/api/info` when Android
returns only itag 18; downloads use web for ≥720p and Android for
≤360p, each with the other client as fallback. The `tv` player
client fails outright ("Remote end closed connection without
response") — do not use it.

Verified on the test video (2026-11-22): web-client 720p download
= 14 MB merged MP4, 1280×676, AV1+Opus, 145 s, in ~16 s total.

## Endpoints

| Route | What it does |
|---|---|
| `GET /` | `{"status":"ok"}` health check |
| `GET /api/info?url=<video url>` | title, duration, thumbnail, formats (progressive MP4s first, then best video+audio combos for 360/720/1080p, plus audio) |
| `GET /api/download?url=<url>&quality=720` | Merged MP4 file, `Content-Disposition: attachment`. Cached in `./cache/` by video+quality, so a repeat download is instant. Quality capped at 1080p. |
| `GET /api/audio?url=<url>` | MP3 file, cached the same way. |

Only `http(s)` URLs on youtube.com, youtu.be, instagram.com,
tiktok.com, facebook.com / fb.watch are accepted; anything else
gets a 400. Errors never expose yt-dlp output — a friendly message
is returned instead. CORS is open for
`https://freemediatools.netlify.app` (and `*` for testing).

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# system deps needed: ffmpeg, nodejs (yt-dlp needs node for the JS runtime)
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
# or: PORT=8000 python3 app.py
curl "http://localhost:8000/api/info?url=https://youtu.be/z_e1d5_VVfw"
```

## Deploy (free)

**Render (recommended, Docker):**
1. Push this folder to a GitHub repo.
2. Render → New → Web Service → pick the repo, runtime **Docker**.
3. Free plan. No env vars needed (`$PORT` is set by Render).
4. Note the service URL, e.g. `https://freemediatools-backend.onrender.com`,
   and point the FreeMediaTools site at `<url>/api/info` etc.
   Free Render instances sleep after inactivity — the first request
   after a nap takes ~30–60 s to wake, then it is fast. That is a
   Render free-tier limit, not a bug in this app.

**Hugging Face Space (Docker SDK):** same Dockerfile works;
create a Space with the Docker SDK, upload these files, and use the
Space URL as the backend URL. HF Spaces also sleep on the free tier.

**Any VPS:** `pip install -r requirements.txt`, install ffmpeg +
nodejs, run `uvicorn app:app --host 0.0.0.0 --port $PORT` behind
a reverse proxy. A VPS that stays awake has no cold-start delay.

## Notes / limits

- Merged files are cached in `./cache/` (keyed by URL + quality).
  Clear old files periodically on a small disk (`rm cache/*`).
- YouTube/Instagram actively block download servers, so a format
  can stop working at any time; updating yt-dlp
  (`pip install -U yt-dlp`) is usually the fix.
- Only download videos you own or that are free to download.
