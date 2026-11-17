"""CUTWORK Poster: a local window for posting a finished clip to TikTok, following TikTok's Direct Post UX rules.

Run:  python poster.py      then the page opens at http://127.0.0.1:5190
Uses post.py for sign-in and upload. Standard library only.
"""
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import post  # noqa: E402

PORT = int(os.environ.get("CUTWORK_POSTER_PORT", 5190))
HOME = Path.home()
ROOTS = [p for p in (HERE.parent / "Clips",
                     HOME / "OneDrive - Durban University of Technology" / "CUTWORK Clips",
                     HOME / "OneDrive" / "CUTWORK Clips") if p.is_dir()]
JOBS = {}
AUTH = {"running": False, "error": None}


def ffprobe():
    try:
        sys.path.insert(0, str(HERE.parent / "cutwork-clipper"))
        from clipper.media import tool
        return tool("ffprobe")
    except Exception:
        return None


FFPROBE = ffprobe()
_DUR = {}


def duration(path):
    key = (str(path), path.stat().st_mtime)
    if key not in _DUR:
        d = None
        if FFPROBE:
            try:
                out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                     capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                d = float(out.stdout.strip())
            except (ValueError, OSError, subprocess.SubprocessError):
                d = None
        _DUR[key] = d
    return _DUR[key]


def allowed(path):
    try:
        p = Path(path).resolve()
    except OSError:
        return None
    if p.suffix.lower() not in (".mp4", ".mov", ".webm") or not p.is_file():
        return None
    if any(r.resolve() in p.parents for r in ROOTS):
        return p
    return None


def list_clips():
    out, seen = [], set()
    for root in ROOTS:      # the local Clips folder comes first; its OneDrive copy is the same clip
        for f in sorted(root.rglob("*")):
            if f.suffix.lower() in (".mp4", ".mov", ".webm") and f.is_file() and ".part" not in f.name:
                key = (f.name, f.stat().st_size)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"path": str(f), "name": f.name, "folder": f.parent.name, "mb": round(f.stat().st_size / 2**20, 1),
                            "mtime": f.stat().st_mtime})
    out.sort(key=lambda c: -c["mtime"])
    return out


def account():
    tokens = post.load_tokens()
    if "tiktok" not in tokens:
        return {"connected": False, "auth_running": AUTH["running"], "auth_error": AUTH["error"]}
    try:
        info = post.tt_creator_info()
    except post.PostError as e:
        return {"connected": True, "error": str(e)}
    try:
        user = post.tt_user_info()
    except post.PostError:
        user = {}
    return {"connected": True, "nickname": info.get("creator_nickname") or user.get("display_name"),
            "username": info.get("creator_username"), "avatar": info.get("creator_avatar_url") or user.get("avatar_url"),
            "privacy_options": info.get("privacy_level_options") or [], "comment_disabled": bool(info.get("comment_disabled")),
            "duet_disabled": bool(info.get("duet_disabled")), "stitch_disabled": bool(info.get("stitch_disabled")),
            "max_duration": info.get("max_video_post_duration_sec")}


def run_auth():
    AUTH.update(running=True, error=None)
    try:
        post.auth_tiktok(post.load_env())
    except post.PostError as e:
        AUTH["error"] = str(e)
    except Exception as e:      # noqa: BLE001  shown on the page rather than lost
        AUTH["error"] = f"{type(e).__name__}: {e}"
    AUTH["running"] = False


def run_post(job, clip, body):
    def progress(msg):
        job["log"].append(msg.strip())
    try:
        res = post.post_tiktok(clip, body["title"], privacy=body["privacy"], allow_comment=body["allow_comment"],
                               allow_duet=body["allow_duet"], allow_stitch=body["allow_stitch"],
                               brand_organic=body["your_brand"], brand_content=body["branded"], progress=progress)
        job.update(state="done", result=res)
        post.log_post({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "clip": str(clip), "platform": "tiktok", "ok": True,
                       "caption": body["title"], "privacy": body["privacy"], **res})
    except post.PostError as e:
        job.update(state="error", error=str(e))
        post.log_post({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "clip": str(clip), "platform": "tiktok", "ok": False,
                       "error": str(e)})
    except Exception as e:      # noqa: BLE001
        job.update(state="error", error=f"{type(e).__name__}: {e}")


def validate(body, acct):
    """The same rules the page enforces, checked again here so nothing is sent that the page would not allow."""
    clip = allowed(body.get("path", ""))
    if not clip:
        return None, "Choose a clip from the list."
    if not isinstance(body.get("title"), str) or len(body["title"]) > 2200:
        return None, "The caption must be text of at most 2,200 characters."
    if body.get("privacy") not in acct.get("privacy_options", []):
        return None, "Choose who can see this post."
    for k in ("allow_comment", "allow_duet", "allow_stitch", "disclose", "your_brand", "branded"):
        body[k] = bool(body.get(k))
    if body["disclose"] and not (body["your_brand"] or body["branded"]):
        return None, "You need to indicate if your content promotes yourself, a third party, or both."
    if not body["disclose"]:
        body["your_brand"] = body["branded"] = False
    if body["branded"] and body["privacy"] == "SELF_ONLY":
        return None, "Branded content visibility cannot be set to private."
    d, mx = duration(clip), acct.get("max_duration")
    if d and mx and d > mx:
        return None, f"This clip is {d:.0f} s long; your account can post up to {mx} s."
    if not body.get("consent"):
        return None, "Press Post to confirm."
    return clip, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_error(403)
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        u = urlsplit(self.path)
        if u.path == "/":
            raw = (HERE / "poster.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif u.path == "/favicon.svg":
            raw = (HERE.parent / "docs" / "favicon.svg").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(raw)
        elif u.path == "/api/account":
            self._json(account())
        elif u.path == "/api/clips":
            self._json({"clips": list_clips(), "roots": [str(r) for r in ROOTS]})
        elif u.path == "/api/duration":
            p = allowed(parse_qs(u.query).get("path", [""])[0])
            self._json({"duration": duration(p) if p else None})
        elif u.path == "/media":
            p = allowed(parse_qs(u.query).get("path", [""])[0])
            if not p:
                return self.send_error(404)
            self._send_file(p)
        elif u.path.startswith("/api/job/"):
            job = JOBS.get(u.path.rsplit("/", 1)[-1])
            self._json(job or {"state": "missing"}, 200 if job else 404)
        else:
            self.send_error(404)

    def _send_file(self, p):
        size = p.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = size - int(m.group(2))
            end = min(end, size - 1)
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                left -= len(chunk)

    def do_POST(self):
        if not self._local_only():
            return
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send_error(403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json({"error": "Bad request."}, 400)
        path = urlsplit(self.path).path
        if path == "/api/connect":
            if not AUTH["running"]:
                threading.Thread(target=run_auth, daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/disconnect":
            tokens = post.load_tokens()
            tokens.pop("tiktok", None)
            post.save_tokens(tokens)
            return self._json({"ok": True})
        if path == "/api/post":
            acct = account()
            if not acct.get("connected") or acct.get("error"):
                return self._json({"error": acct.get("error") or "Connect your TikTok account first."}, 400)
            clip, err = validate(body, acct)
            if err:
                return self._json({"error": err}, 400)
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"state": "running", "log": [], "clip": clip.name}
            threading.Thread(target=run_post, args=(JOBS[jid], clip, body), daemon=True).start()
            return self._json({"job": jid})
        self.send_error(404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    post.say(f"\n  CUTWORK Poster is running: {url}\n  Keep this window open. Close it to stop.\n")
    if os.environ.get("CUTWORK_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
