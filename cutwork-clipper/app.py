"""CUTWORK Clipper: a local web app that finds, marks and exports short clips.

Run:  python app.py   then open http://127.0.0.1:5177
"""
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from clipper import moments as M
from clipper.media import MediaError, check_tools, free_gb, probe, run, tool
from clipper.render import render_clip
from clipper.transcribe import TranscribeError, transcribe

ROOT = Path(__file__).resolve().parent
PROJECTS = Path(os.environ.get("CUTWORK_PROJECTS") or ROOT / "projects")
PROJECTS.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
P = {}                           # id -> project dict
LOCK = threading.Lock()          # guards project.json writes
JOB = threading.Lock()           # one video is prepared and transcribed at a time
MODELS = ("tiny", "base", "small", "medium", "large-v3")
FRAMINGS = ("face", "fit")
USER_ERRORS = (MediaError, TranscribeError)

log = logging.getLogger("cutwork")
log.setLevel(logging.INFO)
try:
    _h = RotatingFileHandler(PROJECTS / "clipper.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
except OSError:
    pass


def say(msg):
    """Print without ever failing: a Windows console can refuse characters it cannot show."""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(str(msg).encode("ascii", "replace").decode(), flush=True)
        except Exception:
            pass


LIBRARY = Path(os.environ.get("CUTWORK_LIBRARY") or ROOT.parent / "Clips")   # one folder per video, named after it


def folder_name(title):
    """A folder name that is safe on Windows, Mac and Linux, made from the video's title."""
    t = re.sub(r"\s*[:/\\|]\s*", " - ", str(title))
    t = re.sub(r'[<>"?*\x00-\x1f]', "", t)
    t = re.sub(r"\s+", " ", t).strip(" .-")[:80].strip(" .-")
    if t.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)\d", t.upper() or ""):
        t += "_"
    return t or "Untitled video"


def library_dir(p):
    """The video's own folder. Two different videos with the same title never share one."""
    if not p.get("folder"):
        name = folder_name(p.get("name"))
        taken = {q.get("folder") for q in P.values() if q is not p and q.get("folder")}
        p["folder"] = f"{name} ({p['id'][:4]})" if name in taken else name
    return LIBRARY / p["folder"]


def _cloud_dir():
    """A synced folder that also gets every finished clip, so the sync app uploads it.

    CUTWORK_CLOUD names the folder ("off" disables it). Without it, OneDrive is used when it
    is set up on this computer: the personal account first, then a work or school account.
    """
    raw = os.environ.get("CUTWORK_CLOUD")
    if raw is not None:
        return None if raw.strip().lower() in ("", "off", "0", "no") else Path(raw.strip())
    for key in ("OneDriveConsumer", "OneDriveCommercial", "OneDrive"):
        v = os.environ.get(key)
        try:
            if v and Path(v).is_dir():
                return Path(v) / "CUTWORK Clips"
        except OSError:
            pass
    return None


CLOUD = _cloud_dir()


def sync_library(p):
    """Copy this video's finished clips into its folder, and into the cloud folder when there is one.

    Never raises: the clips in projects/ are the master copy.
    """
    library_dir(p)      # settles the folder name
    for root in (LIBRARY, CLOUD):
        if not root:
            continue
        try:
            dest = root / p["folder"]
            dest.mkdir(parents=True, exist_ok=True)
            src = pdir(p["id"]) / "clips"
            for c in p["export"].get("clips", []):
                f = src / c["file"]
                t = dest / c["file"]
                if f.is_file() and (not t.is_file() or t.stat().st_size != f.stat().st_size):
                    tmp = dest / (c["file"] + ".part")
                    shutil.copy2(f, tmp)
                    os.replace(tmp, t)
        except Exception:
            log.exception("could not copy clips to %s for %s", root, p.get("id"))


def pdir(pid):
    return PROJECTS / pid


def save(p):
    """Write project.json in one step, so a crash or power cut never leaves half a file."""
    with LOCK:
        try:
            d = pdir(p["id"])
            if not d.is_dir():
                return                      # the project was deleted while a job was finishing
            tmp = d / "project.json.tmp"
            tmp.write_text(json.dumps(p, indent=1), encoding="utf-8")
            os.replace(tmp, d / "project.json")
        except Exception:
            log.exception("could not save project %s", p.get("id"))


def load_all():
    for f in PROJECTS.glob("*/project.json"):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
            if p["status"]["stage"] not in ("ready", "error"):
                p["status"] = {"stage": "error", "pct": 0,
                               "error": "The app was closed while this video was being prepared. Press Try again."}
            p.setdefault("export", {"running": False, "clips": [], "error": None})
            p["export"]["running"] = False
            p.setdefault("moments", [])
            P[p["id"]] = p
        except Exception:
            log.exception("could not load %s", f)


def words_of(pid):
    f = pdir(pid) / "transcript.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))["words"] if f.exists() else []
    except Exception:
        return []


def set_status(p, stage, pct=0, error=None):
    p["status"] = {"stage": stage, "pct": pct, "error": error}


def friendly(e):
    """Text for the page. Known problems read as written; anything else is logged in full."""
    if isinstance(e, USER_ERRORS):
        return str(e)
    if isinstance(e, MemoryError):
        return "This computer ran out of memory. Close other programs and try again."
    if isinstance(e, PermissionError):
        return "A file could not be read or written. Close any program that has the video open, then try again."
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "The disk is full. Free some space and try again."
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip() or type(e).__name__
    return f"Something unexpected went wrong ({type(e).__name__}: {msg[:300]}). Details are in projects/clipper.log."


def _download(p, url, d):
    try:
        import yt_dlp
    except Exception:
        raise MediaError("Links need the yt-dlp package. Run the start script again to install it, or upload the file.")
    for old in d.glob("source.*"):
        old.unlink(missing_ok=True)
    opts = {"outtmpl": str(d / "source.%(ext)s"), "quiet": True, "noprogress": True, "no_warnings": True,
            "noplaylist": True, "format": "bv*[height<=1080]+ba/b[height<=1080]/b", "merge_output_format": "mp4",
            "ffmpeg_location": str(Path(tool("ffmpeg")).parent), "retries": 3, "socket_timeout": 30}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
            p["name"] = (info.get("title") or p["name"])[:120]
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
        raise MediaError(f"That link could not be downloaded: {msg[:300]}")
    files = [f for f in d.glob("source.*") if f.suffix not in (".part", ".ytdl")]
    if not files:
        raise MediaError("That link did not give a video file.")
    p["source"] = str(max(files, key=lambda f: f.stat().st_size))


def process(p, url=None):
    d = pdir(p["id"])
    try:
        if JOB.locked():
            set_status(p, "Waiting for another video to finish")
        with JOB:
            if not d.is_dir():
                return
            check_tools()
            gb = free_gb(PROJECTS)
            if gb is not None and gb < 1.0:
                raise MediaError(f"Only {gb:.1f} GB of disk space is free. Free at least 1 GB and try again.")
            if url and not (p.get("source") and Path(p["source"]).is_file()):
                set_status(p, "Downloading video")
                _download(p, url, d)
            src = p.get("source") or ""
            meta = probe(src)
            if not meta["has_video"]:
                raise MediaError("This file has sound but no picture, so there is nothing to clip.")
            if not meta["has_audio"]:
                raise MediaError("This video has no audio track, so it can't be transcribed.")
            p["duration"] = meta["duration"]
            set_status(p, "Preparing audio and preview"); save(p)
            run(["ffmpeg", "-y", "-hide_banner", "-i", src,
                 "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(d / "audio.wav"),
                 "-map", "0:v:0", "-map", "0:a:0", "-vf", "scale=-2:360", "-pix_fmt", "yuv420p",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-c:a", "aac", "-b:a", "64k", "-ac", "2",
                 "-max_muxing_queue_size", "2048", "-movflags", "+faststart", str(d / "proxy.mp4")])
            set_status(p, "Transcribing (the first video downloads the speech model)")
            s = p["settings"]
            tr = transcribe(d / "audio.wav", s["model"], s["language"],
                            lambda pct: set_status(p, "Transcribing", pct))
            (d / "transcript.json").write_text(json.dumps(tr), encoding="utf-8")
            (d / "audio.wav").unlink(missing_ok=True)
            if not tr["words"]:
                raise MediaError("No speech was found in this video.")
            set_status(p, "Finding moments")
            found = M.find_moments(tr["words"], s["min_len"], s["max_len"], s["count"], s["keywords"])
            dur = p["duration"] or 0
            if dur:     # a moment can never start past the end of the video or run beyond it
                found = [dict(m, end=round(min(m["end"], dur), 2)) for m in found if m["start"] < dur - 3]
            p["moments"] = found
            set_status(p, "ready", 100)
    except BaseException as e:      # the status must always change, whatever went wrong
        set_status(p, "error", 0, friendly(e))
        if not isinstance(e, USER_ERRORS):
            log.exception("processing failed for %s", p.get("id"))
        say(f"[cutwork] {p.get('id')}: {p['status']['error']}")
        try:
            (d / "audio.wav").unlink(missing_ok=True)
        except OSError:
            pass
    save(p)


@app.errorhandler(Exception)
def on_error(e):
    """Every failure answers with JSON, so the page can always show a message."""
    if isinstance(e, HTTPException):
        text = {404: "Not found.", 413: "That file is too large to upload. Use the file path box instead.",
                405: "Not allowed."}.get(e.code, e.description)
        return jsonify({"error": text}), e.code
    log.exception("request failed: %s %s", request.method, request.path)
    return jsonify({"error": friendly(e)}), 500


@app.get("/")
def index():
    r = send_file(ROOT / "static" / "index.html")
    r.headers["Cache-Control"] = "no-store"
    return r


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><rect width="256" height="256" rx="48" fill="#111"/>'
           '<path fill="#F4F2EE" d="M36 76a40 40 0 0 1 40-40h4l44 164v20H76a40 40 0 0 1-40-40z"/>'
           '<path fill="#FF4F1A" d="M96 18h62l48 184h-62z"/>'
           '<path fill="#F4F2EE" d="M174 36h6a40 40 0 0 1 40 40v104a40 40 0 0 1-24 37z"/></svg>')


@app.get("/favicon.ico")
def favicon():
    return app.response_class(FAVICON, mimetype="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/health")
def health():
    return jsonify({"app": "cutwork-clipper", "ffmpeg": bool(tool("ffmpeg")), "free_gb": free_gb(PROJECTS)})


@app.get("/api/projects")
def list_projects():
    items = sorted(P.values(), key=lambda p: -p.get("created", 0))
    return jsonify([{k: p.get(k) for k in ("id", "name", "status", "duration", "created")} for p in items])


def _num(form, key, default, lo, hi, kind=float):
    raw = (form.get(key) or "").strip()
    try:
        v = kind(float(raw)) if raw else default
    except ValueError:
        raise ValueError(f"'{raw}' is not a number.")
    if v != v:  # NaN
        raise ValueError("That is not a number.")
    return max(lo, min(hi, v))


@app.post("/api/projects")
def create_project():
    f = request.form
    try:
        settings = {
            "model": f.get("model", "small") if f.get("model", "small") in MODELS else "small",
            "language": (f.get("language", "").strip().lower()[:8] or None),
            "min_len": _num(f, "min_len", 20.0, 3.0, 600.0), "max_len": _num(f, "max_len", 45.0, 5.0, 900.0),
            "count": _num(f, "count", 12, 1, 40, int), "keywords": f.get("keywords", "").strip()[:2000] or None,
        }
    except ValueError as e:
        return jsonify({"error": f"Check the clip length and count boxes. {e}"}), 400
    if settings["min_len"] >= settings["max_len"]:
        return jsonify({"error": "Shortest clip must be less than longest clip."}), 400
    try:
        check_tools()
    except MediaError as e:
        return jsonify({"error": str(e)}), 400
    url = f.get("url", "").strip()
    path = f.get("path", "").strip().strip('"').strip("'").strip()
    up = request.files.get("file")
    pid = uuid.uuid4().hex[:10]
    d = pdir(pid)
    p = {"id": pid, "name": "Untitled", "created": time.time(), "settings": settings, "url": None, "source": None,
         "moments": [], "export": {"running": False, "clips": [], "error": None},
         "status": {"stage": "Starting", "pct": 0, "error": None}, "duration": 0}
    if up and up.filename:
        ext = re.sub(r"[^A-Za-z0-9.]", "", Path(up.filename).suffix)[:8] or ".mp4"
        d.mkdir(parents=True)
        dest = d / ("source" + ext)
        try:
            up.save(dest)
        except Exception as e:
            shutil.rmtree(d, ignore_errors=True)
            return jsonify({"error": "The upload did not finish. " + friendly(e)}), 400
        if dest.stat().st_size == 0:
            shutil.rmtree(d, ignore_errors=True)
            return jsonify({"error": "That file is empty."}), 400
        p["source"], p["name"] = str(dest), (Path(up.filename).stem or "Untitled")[:120]
    elif path:
        try:
            ok = Path(path).is_file()
        except OSError:
            ok = False
        if not ok:
            return jsonify({"error": f"File not found: {path}"}), 400
        d.mkdir(parents=True)
        p["source"], p["name"] = str(Path(path).resolve()), Path(path).stem[:120]
    elif url:
        if not re.match(r"^https?://\S+$", url):
            return jsonify({"error": "That link does not look right. It should start with http:// or https://"}), 400
        d.mkdir(parents=True)
        p["name"], p["url"] = url[:120], url
    else:
        return jsonify({"error": "Choose a video file, a file path, or a link."}), 400
    P[pid] = p
    save(p)
    threading.Thread(target=process, args=(p, p["url"]), daemon=True).start()
    return jsonify({"id": pid})


def get(pid):
    if pid not in P:
        abort(404)
    return P[pid]


@app.get("/api/projects/<pid>")
def project(pid):
    return jsonify(get(pid))


@app.post("/api/projects/<pid>/retry")
def retry(pid):
    p = get(pid)
    if p["status"]["stage"] != "error":
        return jsonify({"error": "This video is not in a failed state."}), 400
    if not p.get("url") and not (p.get("source") and Path(p["source"]).is_file()):
        return jsonify({"error": "The original video file is no longer there. Add it again."}), 400
    set_status(p, "Starting")
    save(p)
    threading.Thread(target=process, args=(p, p.get("url")), daemon=True).start()
    return jsonify({"ok": True})


@app.delete("/api/projects/<pid>")
def delete_project(pid):
    p = get(pid)
    if p["status"]["stage"] not in ("ready", "error") or p["export"].get("running"):
        return jsonify({"error": "This video is still being worked on. Wait for it to finish first."}), 400
    P.pop(pid, None)
    d = pdir(pid)
    if re.fullmatch(r"[0-9a-f]{10}", pid) and d.is_dir() and d.parent == PROJECTS:
        shutil.rmtree(d, ignore_errors=True)
    return jsonify({"ok": True})


@app.get("/api/projects/<pid>/claude_prompt")
def claude_prompt(pid):
    s = get(pid)["settings"]
    words = words_of(pid)
    if not words:
        return jsonify({"error": "There is no transcript for this video yet."}), 400
    return jsonify({"prompt": M.claude_prompt(words, int(s["min_len"]), int(s["max_len"]))})


@app.post("/api/projects/<pid>/import")
def import_moments(pid):
    p = get(pid)
    raw = str((request.get_json(silent=True) or {}).get("text", ""))
    m = re.search(r"\[.*\]", raw, re.S)
    try:
        items = json.loads(m.group(0)) if m else None
        items = [it for it in items if isinstance(it, dict) and "start" in it and "end" in it]
        new = M.import_moments(items, words_of(pid))
        if p.get("duration"):
            new = [n for n in new if n["start"] < p["duration"]]
    except Exception:
        new = None
    if not new:
        return jsonify({"error": "Couldn't read that. Paste the JSON list exactly as Claude gave it."}), 400
    base = max([m["id"] for m in p["moments"]], default=0)
    for n, m_ in enumerate(new, 1):
        m_["id"] = base + n
    p["moments"] = new + p["moments"]
    save(p)
    return jsonify({"added": len(new)})


def do_export(p, picks, opts):
    ex = p["export"]
    failed = []
    try:
        out = pdir(p["id"]) / "clips"
        out.mkdir(exist_ok=True)
        words = words_of(p["id"])
        safe = re.sub(r"[^A-Za-z0-9]+", "-", p["name"]).strip("-")[:40] or "clip"
        for m in picks:   # one bad clip does not stop the others
            name = f"{safe}_{int(m['start']):05d}s_{opts['framing']}.mp4"
            try:
                render_clip(p["source"], out / name, words, m["start"], m["end"], hook=m["hook"],
                            framing=opts["framing"], captions=opts["captions"], watermark=opts["watermark"])
                ex["clips"] = [c for c in ex["clips"] if c["file"] != name]
                ex["clips"].insert(0, {"file": name, "start": m["start"], "end": m["end"],
                                       "hook": m["hook"], "text": m["text"]})
                ex["done"] += 1
            except BaseException as e:
                failed.append(f"{int(m['start'] // 60)}:{int(m['start'] % 60):02d}: {friendly(e)}")
                if not isinstance(e, USER_ERRORS):
                    log.exception("render failed for %s at %s", p.get("id"), m["start"])
            sync_library(p)       # each finished clip also lands in this video's own folder
            save(p)
    except BaseException as e:
        failed.append(friendly(e))
        log.exception("export failed for %s", p.get("id"))
    if failed:
        ex["error"] = f"{len(failed)} of {len(picks)} clips failed. " + " | ".join(failed[:3])
    ex["running"] = False
    save(p)


@app.post("/api/projects/<pid>/export")
def export(pid):
    p = get(pid)
    if p["status"]["stage"] != "ready":
        return jsonify({"error": "This video is not ready yet."}), 400
    body = request.get_json(silent=True) or {}
    raw = body.get("moments")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "Tick at least one moment first."}), 400
    picks = []
    for m in raw[:60]:
        try:
            s, e = float(m["start"]), float(m["end"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "A start or end time is not a number."}), 400
        if s != s or e != e or s < 0:
            return jsonify({"error": "A start or end time is not a valid number."}), 400
        if e - s < 3:
            return jsonify({"error": "Each clip must be at least 3 seconds long."}), 400
        if e - s > 600:
            return jsonify({"error": "A clip is longer than 10 minutes. Shorten it."}), 400
        if p.get("duration") and s >= p["duration"] - 1:
            return jsonify({"error": "A clip starts after the end of the video."}), 400
        picks.append({"id": m.get("id"), "start": round(s, 2), "end": round(e, 2),
                      "hook": str(m.get("hook") or "").strip()[:70], "text": str(m.get("text") or "")[:2000]})
    if not p.get("source") or not Path(p["source"]).is_file():
        return jsonify({"error": "The original video file has been moved or deleted, so clips can't be exported. "
                                 "Add the video again."}), 400
    with LOCK:
        if p["export"].get("running"):
            return jsonify({"error": "An export is already running."}), 400
        p["export"].update(running=True, error=None, done=0, total=len(picks))
    by_id = {m["id"]: m for m in p["moments"]}
    for m in picks:  # remember the user's edits
        if m["id"] in by_id:
            by_id[m["id"]].update(start=m["start"], end=m["end"], hook=m["hook"])
    opts = {"framing": body.get("framing") if body.get("framing") in FRAMINGS else "face",
            "captions": bool(body.get("captions", True)),
            "watermark": True}      # every clip carries the CUTWORK mark; removing it will be a paid plan
    threading.Thread(target=do_export, args=(p, picks, opts), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/media/<pid>/proxy")
def media_proxy(pid):
    get(pid)
    f = pdir(pid) / "proxy.mp4"
    if not f.exists():
        abort(404)
    return send_file(f, conditional=True)


@app.get("/media/<pid>/clips/<name>")
def media_clip(pid, name):
    get(pid)
    f = pdir(pid) / "clips" / Path(name).name
    if not f.is_file() or f.suffix != ".mp4":
        abort(404)
    return send_file(f, conditional=True, as_attachment=request.args.get("dl") == "1")


def _port_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return json.loads(r.read()).get("app") == "cutwork-clipper"
    except Exception:
        return False


def main():
    load_all()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5177))
    local = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1", "localhost") else host
    show = os.environ.get("CUTWORK_NO_BROWSER") != "1"
    if not _port_free(host, port):
        if _already_running(port):
            say(f"\n  CUTWORK Clipper is already running: http://{local}:{port}\n")
            if show:
                webbrowser.open(f"http://{local}:{port}")
            return
        for port in range(port + 1, port + 20):
            if _port_free(host, port):
                break
        else:
            say("  No free port was found. Close other programs and try again.")
            sys.exit(1)
    if not tool("ffmpeg"):
        say("\n  WARNING: ffmpeg was not found. Install it (see README) before adding a video.")
    say(f"\n  CUTWORK Clipper is running: http://{local}:{port}\n  Keep this window open. Close it to stop the app.")
    say(f"  Finished clips are filed per video in: {LIBRARY}")
    say(f"  A copy of each clip also goes to: {CLOUD}\n" if CLOUD else "")
    if show:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{local}:{port}")).start()
    try:
        from waitress import serve
    except Exception:
        serve = None
    if serve:
        logging.getLogger("waitress.queue").setLevel(logging.ERROR)
        serve(app, host=host, port=port, threads=16, max_request_body_size=64 * 2 ** 30, channel_timeout=3600)
    else:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
