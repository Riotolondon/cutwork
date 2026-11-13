"""CUTWORK link helper: downloads a video from a link so the web app can clip it.

A browser cannot download from YouTube and similar sites by itself, so the web app asks
this small server to do it and then reads the finished file from here.

Run:  python server.py     (uses the clipper's Python packages: flask, waitress, yt-dlp)
"""
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request, send_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cutwork-clipper"))
try:
    from clipper.media import tool          # finds ffmpeg wherever it is installed
except Exception:                            # running without the clipper next to it
    def tool(name):
        return shutil.which(name)

PORT = int(os.environ.get("PORT", 5188))
HOST = os.environ.get("HOST", "127.0.0.1")
WORK = Path(os.environ.get("CUTWORK_LINK_DIR") or Path(tempfile.gettempdir()) / "cutwork-link")
MAX_SECONDS = int(os.environ.get("CUTWORK_LINK_MAX_SECONDS", 3 * 3600))
MAX_BYTES = int(1.8 * 2 ** 30)              # the web app cannot read a bigger file
KEEP_SECONDS = 3600
MAX_WAITING = 3
# Pages that may call this server. Add your own site with CUTWORK_LINK_ORIGINS="https://a.com,https://b.com".
ORIGINS = {o.strip().rstrip("/") for o in (os.environ.get("CUTWORK_LINK_ORIGINS") or
           "https://riotolondon.github.io").split(",") if o.strip()}
LOCAL = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$")
# Only well-known video sites: this server must never be usable to reach other addresses.
SITES = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
         "x.com", "twitter.com", "twitch.tv", "dailymotion.com", "loom.com", "streamable.com")

app = Flask(__name__)
JOBS = {}
LOCK = threading.Lock()
ONE = threading.Lock()          # one download at a time


def allowed_site(url):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == s or host.endswith("." + s) for s in SITES)


@app.after_request
def cors(r):
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin in ORIGINS or LOCAL.match(origin):
        r.headers["Access-Control-Allow-Origin"] = origin
        r.headers["Vary"] = "Origin"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
        r.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range"
        r.headers["Access-Control-Allow-Private-Network"] = "true"
    return r


def clean_up():
    now = time.time()
    with LOCK:
        for jid in [j for j, v in JOBS.items() if now - v["made"] > KEEP_SECONDS]:
            shutil.rmtree(WORK / jid, ignore_errors=True)
            JOBS.pop(jid, None)
    if WORK.is_dir():
        for d in WORK.iterdir():            # folders left by an earlier run
            if d.is_dir() and d.name not in JOBS and now - d.stat().st_mtime > KEEP_SECONDS:
                shutil.rmtree(d, ignore_errors=True)


def download(job, url):
    d = WORK / job["id"]
    try:
        import yt_dlp
        if ONE.locked():
            job["stage"] = "Waiting for another download to finish"
        with ONE:
            job["stage"] = "Looking up the video"

            def hook(p):
                if p.get("status") == "downloading":
                    total = p.get("total_bytes") or p.get("total_bytes_estimate") or 0
                    if total:
                        job["pct"] = min(99, int(p.get("downloaded_bytes", 0) / total * 100))
                    job["eta"] = p.get("eta")
                    job["stage"] = "Downloading the video"
                elif p.get("status") == "finished":
                    job["stage"] = "Putting picture and sound together"

            def too_big(info, *, incomplete):
                if info.get("is_live"):
                    return "Live streams cannot be clipped until they have ended."
                if (info.get("duration") or 0) > MAX_SECONDS:
                    return f"This video is longer than {MAX_SECONDS // 3600} hours."
                return None

            opts = {"outtmpl": str(d / "video.%(ext)s"), "quiet": True, "noprogress": True, "no_warnings": True,
                    "noplaylist": True, "merge_output_format": "mp4", "retries": 3, "socket_timeout": 30,
                    # H.264 picture and AAC sound, so every browser can play and clip the file
                    "format": "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/b[vcodec^=avc1][height<=1080]/"
                              "bv*[height<=1080]+ba/b[height<=1080]/b",
                    "max_filesize": MAX_BYTES, "match_filter": too_big, "progress_hooks": [hook]}
            ff = tool("ffmpeg")
            if ff:
                opts["ffmpeg_location"] = str(Path(ff).parent)
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=True)
            if not info:
                raise RuntimeError("This video cannot be downloaded (it may be live, private or too long).")
            job["title"] = str(info.get("title") or "video")[:120]
            files = [f for f in d.glob("video.*") if f.suffix not in (".part", ".ytdl")]
            if not files:
                raise RuntimeError("That link did not give a video file. It may be too long or too large.")
            f = max(files, key=lambda x: x.stat().st_size)
            if f.stat().st_size > MAX_BYTES:
                raise RuntimeError("This video is larger than 1.8 GB, which is more than the web app can open.")
            job.update(file=str(f), size=f.stat().st_size, pct=100, stage="ready")
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
        job.update(stage="error", error=("That link could not be downloaded: " + msg[:300]))
        shutil.rmtree(d, ignore_errors=True)


@app.get("/health")
def health():
    return jsonify({"app": "cutwork-link", "ffmpeg": bool(tool("ffmpeg"))})


@app.route("/fetch", methods=["POST", "OPTIONS"])
def fetch():
    if request.method == "OPTIONS":
        return "", 204
    url = str((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not re.match(r"^https?://\S+$", url):
        return jsonify({"error": "That link does not look right. It should start with https://"}), 400
    if not allowed_site(url):
        return jsonify({"error": "Links from that site are not supported. Supported: YouTube, Vimeo, TikTok, "
                                 "Instagram, Facebook, X, Twitch, Dailymotion, Loom, Streamable."}), 400
    clean_up()
    with LOCK:
        if sum(1 for j in JOBS.values() if j["stage"] not in ("ready", "error")) >= MAX_WAITING:
            return jsonify({"error": "The link helper is busy. Try again in a few minutes."}), 429
        jid = uuid.uuid4().hex[:16]
        job = JOBS[jid] = {"id": jid, "stage": "Starting", "pct": 0, "eta": None, "title": "", "size": 0,
                           "error": None, "made": time.time()}
    (WORK / jid).mkdir(parents=True, exist_ok=True)
    threading.Thread(target=download, args=(job, url), daemon=True).start()
    return jsonify({"id": jid})


def get(jid):
    job = JOBS.get(jid)
    if not job:
        abort(404)
    return job


@app.get("/status/<jid>")
def status(jid):
    j = get(jid)
    return jsonify({k: j[k] for k in ("stage", "pct", "eta", "title", "size", "error")})


@app.route("/file/<jid>", methods=["GET", "OPTIONS"])
def file(jid):
    if request.method == "OPTIONS":
        return "", 204
    j = get(jid)
    if j["stage"] != "ready" or not Path(j.get("file", "")).is_file():
        abort(404)
    return send_file(j["file"], mimetype="video/mp4", conditional=True)


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found. The download may have expired; add the link again."}), 404


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    clean_up()
    print(f"\n  CUTWORK link helper is running on http://{HOST}:{PORT}\n  Keep this window open. Close it to stop.\n",
          flush=True)
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT, threads=8, channel_timeout=3600)
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True)
