"""FreeMediaTools downloader backend.

Small FastAPI app that runs yt-dlp as a subprocess.
No Cobalt API (now needs JWT auth), no Invidious (502).

Key trick (verified 2026-11-22): YouTube's Android player client is
fast (~3 s) but YouTube's SABR experiment often leaves it with only
progressive itag 18 (360p) — stderr:
  "Some android client https formats have been skipped as they are
  missing a URL. YouTube may have enabled the SABR-only streaming
  experiment for the current session."
The default web client returned the FULL format list in ~5 s the
same day (it had been ~77 s / timeouts before, so it is intermittent).
The app therefore tries Android first for /api/info and falls back
to the web client when Android returns only itag 18; downloads use
web for >=720p and Android for <=360p, each with the other client
as fallback. The `tv` player client fails outright ("Remote end
closed connection without response", retries 1-3, "The page needs
to be reloaded") — do not use it.

Endpoints:
  GET /                      -> {"status": "ok"}
  GET /api/info?url=...      -> title, duration, thumbnail, formats
  GET /api/download?url=...&quality=720  -> merged MP4 file (cached)
  GET /api/audio?url=...     -> MP3 file (cached)

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

ALLOWED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)

YTDLP_BASE = [
    "yt-dlp",
    "--js-runtimes", "node",
    "--extractor-args", "youtube:player_client=android",
    "--no-playlist",
    "--no-warnings",
]

# Default (web) client — no --extractor-args. Slower/intermittent in the
# past (~77 s once), but on 2026-11-22 it returned the FULL format list
# in ~5 s. Use it when the Android client only returns itag 18.
YTDLP_WEB = [
    "yt-dlp",
    "--js-runtimes", "node",
    "--no-playlist",
    "--no-warnings",
]

app = FastAPI(title="FreeMediaTools Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://freemediatools.netlify.app", "*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _host_allowed(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def _friendly_error(url: str) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"error": "Is video ka download nahi ho saka. Link check karein ya dobara try karein."},
    )


def _run_ytdlp(args, timeout=120, client="android"):
    """Run yt-dlp, return (returncode, stdout, stderr).
    client="android" (fast, but YouTube's SABR experiment often leaves
    only progressive itag 18) or "web" (full format list, occasionally
    slow / JS-challenge warnings)."""
    base = YTDLP_BASE if client == "android" else YTDLP_WEB
    try:
        p = subprocess.run(
            base + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "yt-dlp not installed"


def _usable_format_count(data) -> int:
    if not data:
        return 0
    n = 0
    for f in data.get("formats") or []:
        if f.get("url") and f.get("ext") not in ("mhtml",):
            n += 1
    return n


def _label_height(f) -> str:
    """Human quality label. itag 18 is 360p even though yt-dlp reports
    height 338 for this video; itag 22 is 720p. Otherwise round the
    reported height up to the nearest standard bucket."""
    fid = str(f.get("format_id") or "")
    if fid == "18":
        return "360p"
    if fid == "22":
        return "720p"
    h = f.get("height") or 0
    for bucket in (144, 240, 360, 480, 720, 1080, 1440, 2160):
        if h <= bucket:
            return f"{bucket}p"
    return f"{h}p"


def _get_info(url: str):
    """Return parsed yt-dlp -J dict, or None on failure.
    Retries once per client, then falls back to the other client:
    Android is fast but SABR often leaves only itag 18; the web
    client returns the full list (720p/1080p separate video+audio)
    when it works. Extraction can fail intermittently — one failure
    is never treated as broken."""
    android_data = None
    for _attempt in range(2):
        rc, out, _err = _run_ytdlp(["-J", url], timeout=120, client="android")
        if rc == 0 and out.strip():
            try:
                data = json.loads(out)
            except Exception:
                data = None
            if data:
                if _usable_format_count(data) > 2:
                    return data
                android_data = data
                break
    # Web client fallback for the full format list.
    for _attempt in range(2):
        rc, out, _err = _run_ytdlp(["-J", url], timeout=180, client="web")
        if rc == 0 and out.strip():
            try:
                data = json.loads(out)
            except Exception:
                data = None
            if data and _usable_format_count(data) > 0:
                return data
    return android_data


def _sanitise(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name or "video")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] or "video"


def _cache_key(url: str, suffix: str) -> str:
    return hashlib.sha256(f"{url}|{suffix}".encode()).hexdigest()[:16]


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/api/info")
def info(url: str = Query(...)):
    if not url.startswith(("http://", "https://")) or not _host_allowed(url):
        return JSONResponse(status_code=400, content={"error": "Sirf YouTube, Instagram, TikTok ya Facebook ka link paste karein."})
    data = _get_info(url)
    if not data:
        return _friendly_error(url)

    formats = []
    seen = set()
    all_fmts = data.get("formats") or []

    def _add(quality, ext, filesize, fmt_url, progressive):
        key = (quality, ext, progressive)
        if key in seen or not fmt_url:
            return
        seen.add(key)
        formats.append({
            "quality": quality,
            "ext": ext,
            "filesize": filesize,
            "url": fmt_url,
            "progressive": progressive,
        })

    # 1) Progressive MP4s first (video+audio in one file): itag 18 (360p), 22 (720p)
    for f in all_fmts:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") in (None, "none") or f.get("acodec") in (None, "none"):
            continue
        h = f.get("height")
        if not h:
            continue
        _add(_label_height(f), "mp4", f.get("filesize") or f.get("filesize_approx"),
              f.get("url"), True)

    # 2) Best video+audio combos for 360/720/1080 (merged by /api/download).
    # Skip a target already covered by a progressive format (avoids the
    # duplicate 360p entry seen when YouTube only returns itag 18).
    progressive_labels = {f["quality"] for f in formats if f["progressive"]}
    for target in (360, 720, 1080):
        if f"{target}p" in progressive_labels:
            continue
        video = None
        for f in all_fmts:
            if f.get("vcodec") in (None, "none"):
                continue
            h = f.get("height") or 0
            if h > target or h == 0:
                continue
            if f.get("ext") not in ("mp4", "webm"):
                continue
            if video is None or h > (video.get("height") or 0):
                video = f
        if not video:
            continue
        audio = None
        for f in all_fmts:
            if f.get("acodec") in (None, "none") or f.get("vcodec") not in (None, "none"):
                continue
            audio = f
            break
        size = None
        if video.get("filesize") and audio and audio.get("filesize"):
            size = video["filesize"] + audio["filesize"]
        elif video.get("filesize_approx"):
            size = video["filesize_approx"]
        _add(f"{target}p", "mp4", size, video.get("url"), False)

    # 3) Audio options. YouTube's Android client often returns NO
    # audio-only format (SABR experiment) — only itag 18. In that case
    # still advertise an MP3 option with url=None: the frontend should
    # call /api/audio, which extracts MP3 from the downloaded video.
    audio_fmt = None
    for f in all_fmts:
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none"):
            audio_fmt = f
            break
    if audio_fmt:
        _add("audio", audio_fmt.get("ext") or "m4a",
              audio_fmt.get("filesize") or audio_fmt.get("filesize_approx"),
              audio_fmt.get("url"), False)
    elif any(f["progressive"] for f in formats):
        _add("audio", "mp3", None, None, False)

    return {
        "title": data.get("title"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "id": data.get("id"),
        "formats": formats,
    }


@app.get("/api/download")
def download(url: str = Query(...), quality: int = Query(720)):
    if not url.startswith(("http://", "https://")) or not _host_allowed(url):
        return JSONResponse(status_code=400, content={"error": "Sirf YouTube, Instagram, TikTok ya Facebook ka link paste karein."})
    quality = max(144, min(1080, int(quality)))
    key = _cache_key(url, f"video{quality}")
    out_path = CACHE_DIR / f"{key}_{quality}p.mp4"
    if out_path.exists() and out_path.stat().st_size > 100_000:
        title = _sanitise(out_path.stem)
        return FileResponse(out_path, media_type="video/mp4",
                            filename=f"{title}.mp4")

    data = _get_info(url)
    if not data:
        return _friendly_error(url)
    title = _sanitise(data.get("title") or "video")
    tmp_path = CACHE_DIR / f"{key}_{quality}p.tmp.mp4"
    fmt = f"bv*[height<={quality}]+ba/b[height<={quality}]"
    # 720p/1080p need the web client's separate video+audio formats;
    # <=360p is served fastest by Android's progressive itag 18.
    # Fall back to the other client if the first one fails.
    clients = ["web", "android"] if quality >= 720 else ["android", "web"]
    ok = False
    for client in clients:
        rc, _out, _err = _run_ytdlp(
            ["-f", fmt, "--merge-output-format", "mp4",
             "-o", str(tmp_path), url],
            timeout=600, client=client,
        )
        if rc == 0 and tmp_path.exists() and tmp_path.stat().st_size > 100_000:
            ok = True
            break
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    if not ok:
        return _friendly_error(url)
    tmp_path.rename(out_path)
    return FileResponse(out_path, media_type="video/mp4",
                        filename=f"{title}.mp4")


@app.get("/api/audio")
def audio(url: str = Query(...)):
    if not url.startswith(("http://", "https://")) or not _host_allowed(url):
        return JSONResponse(status_code=400, content={"error": "Sirf YouTube, Instagram, TikTok ya Facebook ka link paste karein."})
    key = _cache_key(url, "audio")
    out_path = CACHE_DIR / f"{key}_audio.mp3"
    if out_path.exists() and out_path.stat().st_size > 10_000:
        return FileResponse(out_path, media_type="audio/mpeg",
                            filename="audio.mp3")

    data = _get_info(url)
    if not data:
        return _friendly_error(url)
    title = _sanitise(data.get("title") or "audio")
    tmp_path = CACHE_DIR / f"{key}_audio.tmp.mp3"
    ok = False
    for client in ("android", "web"):
        rc, _out, _err = _run_ytdlp(
            ["-f", "ba/b", "-x", "--audio-format", "mp3",
             "--audio-quality", "0", "-o", str(tmp_path), url],
            timeout=600, client=client,
        )
        if rc == 0 and tmp_path.exists() and tmp_path.stat().st_size > 10_000:
            ok = True
            break
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    if not ok:
        return _friendly_error(url)
    tmp_path.rename(out_path)
    return FileResponse(out_path, media_type="audio/mpeg",
                        filename=f"{title}.mp3")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
