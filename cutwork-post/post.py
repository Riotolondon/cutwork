"""cutwork-post: post finished CUTWORK clips to Instagram Reels and TikTok from this computer.

Uses the official APIs directly (no paid service):
  Instagram  Instagram API with Instagram Login, graph.instagram.com (Business or Creator account)
  TikTok     Content Posting API, Direct Post (open.tiktokapis.com)

Commands:
  python post.py check                          what is set up and what is missing
  python post.py auth instagram                 sign in once (opens the browser)
  python post.py auth tiktok                    sign in once (opens the browser)
  python post.py post CLIP.mp4 --caption "..."  post one clip to both (or --to instagram / --to tiktok)
  python post.py history                        what has been posted

Instagram only accepts a video from a public web address, so while a clip is being posted it is served from this
computer through Tailscale Funnel on port 8443 at a random, one-off path, and the port is closed again afterwards.
Standard library only.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
TOKENS_FILE = HERE / "tokens.json"
LOG_FILE = HERE / "posted.jsonl"

IG_GRAPH = "https://graph.instagram.com"
IG_VERSION = "v27.0"
TT_API = "https://open.tiktokapis.com"
FUNNEL_PORT = 8443          # 443 is taken by the CUTWORK link helper
LOCAL_PORT = 5199           # where the one-off clip server listens on this computer
TT_REDIRECT_PORT = 3455


class PostError(RuntimeError):
    """A problem the user can act on; printed as it is."""


def say(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(str(msg).encode("ascii", "replace").decode(), flush=True)


# ------------------------------------------------------------------------------------------------ settings and tokens
def load_env():
    env = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("IG_APP_ID", "IG_APP_SECRET", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_PRIVACY", "TIKTOK_PKCE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def need(env, *keys):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise PostError(f"Missing in {ENV_FILE.name}: {', '.join(missing)}. See README.md, step by step.")


def load_tokens():
    try:
        return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_tokens(tokens):
    tmp = TOKENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=1), encoding="utf-8")
    os.replace(tmp, TOKENS_FILE)


# ------------------------------------------------------------------------------------------------ HTTP
def http(method, url, *, params=None, form=None, json_body=None, data=None, headers=None, timeout=120):
    """Returns parsed JSON (or text). Raises PostError with the service's own message on failure."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = dict(headers or {})
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        h.setdefault("Content-Type", "application/json; charset=UTF-8")
    elif data is not None:
        body = data
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except urllib.error.URLError as e:
        raise PostError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {e.reason}")
    try:
        out = json.loads(raw) if raw else {}
    except ValueError:
        out = {"_text": raw[:500].decode("utf-8", "replace")}
    if status >= 400:
        raise PostError(f"{urllib.parse.urlsplit(url).netloc} answered {status}: {describe_error(out)}")
    if isinstance(out, dict):
        out["_status"] = status
    return out


def describe_error(out):
    if not isinstance(out, dict):
        return str(out)[:300]
    e = out.get("error")
    if isinstance(e, dict):
        parts = [str(e.get(k)) for k in ("code", "type", "message", "error_user_msg") if e.get(k)]
        return " / ".join(parts) or json.dumps(e)[:300]
    if e:
        return f"{e}: {out.get('error_description') or out.get('error_message') or ''}".strip()
    return json.dumps(out)[:300]


# ------------------------------------------------------------------------------------------------ Tailscale Funnel
def tailscale_exe():
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not Path(exe).exists() and not shutil.which("tailscale"):
        raise PostError("Tailscale is needed for Instagram (it gives the clip a public address) and was not found.")
    return exe


def public_host():
    out = subprocess.run([tailscale_exe(), "status", "--json"], capture_output=True, text=True, encoding="utf-8")
    try:
        host = json.loads(out.stdout)["Self"]["DNSName"].rstrip(".")
    except (ValueError, KeyError, TypeError):
        raise PostError("Could not read this computer's Tailscale name. Is Tailscale running and signed in?")
    return host


class Funnel:
    """Serve one handler on 127.0.0.1:LOCAL_PORT and publish it at https://<host>:8443 until closed."""

    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), self.handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        r = subprocess.run([tailscale_exe(), "funnel", "--bg", f"--https={FUNNEL_PORT}", str(LOCAL_PORT)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode != 0 or "not enabled" in (r.stdout + r.stderr).lower():
            self.server.shutdown()
            raise PostError("Tailscale Funnel could not open port 8443: " + (r.stdout + r.stderr).strip()[:300])
        self.base = f"https://{public_host()}:{FUNNEL_PORT}"
        return self

    def __exit__(self, *exc):
        subprocess.run([tailscale_exe(), "funnel", f"--https={FUNNEL_PORT}", "off"], capture_output=True, timeout=60)
        self.server.shutdown()
        self.server.server_close()


def file_handler(path, secret_path):
    """Serves exactly one file at one unguessable path; everything else is 404."""
    size = path.stat().st_size

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _head(self, start, end, code):
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()

        def _range(self):
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                a, _, b = rng[6:].split(",")[0].partition("-")
                try:
                    start = int(a) if a else size - int(b)
                    end = int(b) if (a and b) else size - 1
                    if 0 <= start <= end < size:
                        return start, min(end, size - 1), 206
                except ValueError:
                    pass
            return 0, size - 1, 200

        def do_HEAD(self):
            if self.path.split("?")[0] != secret_path:
                return self.send_error(404)
            self._head(*self._range())

        def do_GET(self):
            if self.path.split("?")[0] != secret_path:
                return self.send_error(404)
            start, end, code = self._range()
            self._head(start, end, code)
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)
    return H


def wait_for_callback(server_factory, timeout=300):
    """Run a one-shot callback server; returns the query dict of the first request to the callback path."""
    got = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if not ("code" in q or "error" in q):
                return self.send_error(404)
            got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in q
            self.wfile.write(("<html><body style='font-family:system-ui;background:#111;color:#F4F2EE;padding:40px'>"
                              f"<h2>{'Signed in. You can close this tab.' if ok else 'Sign-in failed.'}</h2>"
                              f"<p>{'' if ok else q.get('error_description') or q.get('error_reason') or q.get('error')}</p>"
                              "</body></html>").encode())
            done.set()
    return server_factory(H), got, done


# ------------------------------------------------------------------------------------------------ Instagram
IG_SCOPES = "instagram_business_basic,instagram_business_content_publish"


def ig_redirect_uri():
    return f"https://{public_host()}:{FUNNEL_PORT}/instagram/callback"


def auth_instagram(env):
    need(env, "IG_APP_ID", "IG_APP_SECRET")
    redirect = ig_redirect_uri()
    state = secrets.token_urlsafe(16)
    url = "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode(
        {"client_id": env["IG_APP_ID"], "redirect_uri": redirect, "response_type": "code", "scope": IG_SCOPES,
         "state": state})
    holder = {}

    def factory(handler):
        holder["f"] = Funnel(handler)
        return holder["f"]

    funnel, got, done = wait_for_callback(factory)
    with funnel:
        say(f"Opening Instagram sign-in. The redirect address registered in your Meta app must be exactly:\n  {redirect}\n")
        webbrowser.open(url)
        say("Waiting for you to approve in the browser (5 minutes)...")
        if not done.wait(300):
            raise PostError("No answer from Instagram within 5 minutes. Run the command again.")
    if "code" not in got:
        raise PostError(f"Instagram sign-in was refused: {got.get('error_description') or got.get('error')}")
    if got.get("state") != state:
        raise PostError("The sign-in answer did not match this request. Run the command again.")
    code = got["code"].split("#")[0]
    short = http("POST", "https://api.instagram.com/oauth/access_token",
                 form={"client_id": env["IG_APP_ID"], "client_secret": env["IG_APP_SECRET"],
                       "grant_type": "authorization_code", "redirect_uri": redirect, "code": code})
    short = (short.get("data") or [short])[0]
    long_ = http("GET", f"{IG_GRAPH}/access_token",
                 params={"grant_type": "ig_exchange_token", "client_secret": env["IG_APP_SECRET"],
                         "access_token": short["access_token"]})
    token = long_["access_token"]
    me = http("GET", f"{IG_GRAPH}/{IG_VERSION}/me", params={"fields": "user_id,username", "access_token": token})
    tokens = load_tokens()
    tokens["instagram"] = {"access_token": token, "user_id": str(me.get("user_id") or short.get("user_id")),
                           "username": me.get("username"), "obtained_at": time.time(),
                           "expires_at": time.time() + int(long_.get("expires_in", 5184000))}
    save_tokens(tokens)
    say(f"Instagram connected: @{me.get('username')}")


def ig_token():
    t = load_tokens().get("instagram")
    if not t:
        raise PostError("Instagram is not connected yet. Run: python post.py auth instagram")
    now = time.time()
    if now > t["expires_at"]:
        raise PostError("The Instagram sign-in has expired (60 days). Run: python post.py auth instagram")
    if now - t["obtained_at"] > 86400 and t["expires_at"] - now < 30 * 86400:   # refresh quietly in the last month
        try:
            r = http("GET", f"{IG_GRAPH}/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": t["access_token"]})
            t.update(access_token=r["access_token"], obtained_at=now, expires_at=now + int(r.get("expires_in", 5184000)))
            tokens = load_tokens()
            tokens["instagram"] = t
            save_tokens(tokens)
        except PostError as e:
            say(f"  (could not refresh the Instagram sign-in: {e}; carrying on with the current one)")
    return t


def post_instagram(clip, caption, share_to_feed=True):
    t = ig_token()
    auth = {"Authorization": f"Bearer {t['access_token']}"}
    secret_path = f"/{secrets.token_urlsafe(24)}/{clip.name.replace(' ', '_')}"
    with Funnel(file_handler(clip, secret_path)) as f:
        video_url = f.base + urllib.parse.quote(secret_path)
        say("  Instagram: sending the clip address")
        c = http("POST", f"{IG_GRAPH}/{IG_VERSION}/{t['user_id']}/media", headers=auth,
                 json_body={"media_type": "REELS", "video_url": video_url, "caption": caption[:2200],
                            "share_to_feed": share_to_feed})
        cid = c["id"]
        deadline = time.time() + 15 * 60
        while True:
            s = http("GET", f"{IG_GRAPH}/{IG_VERSION}/{cid}", headers=auth, params={"fields": "status_code,status"})
            code = s.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PostError(f"Instagram could not process the clip ({code}: {s.get('status')}). Check README: video rules.")
            if time.time() > deadline:
                raise PostError("Instagram was still processing after 15 minutes. Try again later.")
            say(f"  Instagram: processing ({code or 'waiting'})")
            time.sleep(15)
    p = http("POST", f"{IG_GRAPH}/{IG_VERSION}/{t['user_id']}/media_publish", headers=auth, json_body={"creation_id": cid})
    mid = p["id"]
    try:
        link = http("GET", f"{IG_GRAPH}/{IG_VERSION}/{mid}", headers=auth, params={"fields": "permalink"}).get("permalink")
    except PostError:
        link = None
    return {"id": mid, "url": link}


# ------------------------------------------------------------------------------------------------ TikTok
TT_SCOPES = "user.info.basic,video.publish"


def tt_redirect_uri():
    return f"http://localhost:{TT_REDIRECT_PORT}/callback/"


def pkce_pair(style):
    verifier = secrets.token_urlsafe(64)[:64]
    digest = hashlib.sha256(verifier.encode()).digest()
    if style == "base64url":      # RFC 7636
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    else:                         # what TikTok's desktop page describes: hex of SHA-256
        challenge = digest.hex()
    return verifier, challenge


def auth_tiktok(env):
    need(env, "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET")
    redirect = tt_redirect_uri()
    state = secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair(env.get("TIKTOK_PKCE", "hex"))
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(
        {"client_key": env["TIKTOK_CLIENT_KEY"], "scope": TT_SCOPES, "response_type": "code", "redirect_uri": redirect,
         "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    server, got, done = wait_for_callback(lambda h: ThreadingHTTPServer(("127.0.0.1", TT_REDIRECT_PORT), h))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        say(f"Opening TikTok sign-in. The redirect address registered in your TikTok app must be exactly:\n  {redirect}\n")
        webbrowser.open(url)
        say("Waiting for you to approve in the browser (5 minutes)...")
        if not done.wait(300):
            raise PostError("No answer from TikTok within 5 minutes. Run the command again.")
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in got:
        raise PostError(f"TikTok sign-in was refused: {got.get('error_description') or got.get('error')}")
    if got.get("state") != state:
        raise PostError("The sign-in answer did not match this request. Run the command again.")
    tok = http("POST", f"{TT_API}/v2/oauth/token/",
               form={"client_key": env["TIKTOK_CLIENT_KEY"], "client_secret": env["TIKTOK_CLIENT_SECRET"],
                     "code": urllib.parse.unquote(got["code"]), "grant_type": "authorization_code",
                     "redirect_uri": redirect, "code_verifier": verifier})
    if "access_token" not in tok:
        hint = "" if env.get("TIKTOK_PKCE") == "base64url" else " If it mentions the code verifier, add TIKTOK_PKCE=base64url to .env and try again."
        raise PostError(f"TikTok did not give a token: {describe_error(tok)}.{hint}")
    store_tiktok(tok)
    info = tt_creator_info()
    say(f"TikTok connected: @{info.get('creator_username')}  (privacy options: {', '.join(info.get('privacy_level_options', []))})")


def store_tiktok(tok):
    now = time.time()
    tokens = load_tokens()
    tokens["tiktok"] = {"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                        "open_id": tok.get("open_id"), "expires_at": now + int(tok.get("expires_in", 7200)) - 120,
                        "refresh_expires_at": now + int(tok.get("refresh_expires_in", 15552000))}
    save_tokens(tokens)


def tt_token():
    t = load_tokens().get("tiktok")
    if not t:
        raise PostError("TikTok is not connected yet. Run: python post.py auth tiktok")
    if time.time() < t["expires_at"]:
        return t["access_token"]
    if time.time() > t["refresh_expires_at"]:
        raise PostError("The TikTok sign-in has expired. Run: python post.py auth tiktok")
    env = load_env()
    need(env, "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET")
    tok = http("POST", f"{TT_API}/v2/oauth/token/",
               form={"client_key": env["TIKTOK_CLIENT_KEY"], "client_secret": env["TIKTOK_CLIENT_SECRET"],
                     "grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
    if "access_token" not in tok:
        raise PostError(f"Could not renew the TikTok sign-in ({describe_error(tok)}). Run: python post.py auth tiktok")
    store_tiktok(tok)     # TikTok rotates the refresh token on every renewal
    return tok["access_token"]


def tt_headers():
    return {"Authorization": f"Bearer {tt_token()}"}


def tt_creator_info():
    r = http("POST", f"{TT_API}/v2/post/publish/creator_info/query/", headers=tt_headers(), json_body={})
    return r.get("data") or {}


def tt_user_info():
    """Basic profile (user.info.basic): shown so the creator can see which account is connected."""
    r = http("GET", f"{TT_API}/v2/user/info/", headers=tt_headers(),
             params={"fields": "open_id,avatar_url,display_name"})
    return (r.get("data") or {}).get("user") or {}


def tt_chunks(size):
    """(chunk_size, count) under TikTok's rules: whole file up to 64 MB, else 10 MB chunks with the last one absorbing the rest."""
    if size <= 64 * 1024 * 1024:
        return size, 1
    chunk = 10 * 1024 * 1024
    return chunk, size // chunk


def post_tiktok(clip, caption, privacy=None, allow_comment=True, allow_duet=True, allow_stitch=True,
                brand_organic=False, brand_content=False, progress=None):
    say_ = progress or say
    info = tt_creator_info()
    options = info.get("privacy_level_options") or []
    privacy = privacy or load_env().get("TIKTOK_PRIVACY") or "SELF_ONLY"
    if options and privacy not in options:
        raise PostError(f"TikTok does not allow privacy '{privacy}' for this account. Allowed: {', '.join(options)}")
    if brand_content and privacy == "SELF_ONLY":
        raise PostError("Branded content visibility cannot be set to private.")
    size = clip.stat().st_size
    chunk, count = tt_chunks(size)
    say_(f"  TikTok: starting upload ({size / 2**20:.1f} MB, {count} part{'s' if count > 1 else ''}, privacy {privacy})")
    r = http("POST", f"{TT_API}/v2/post/publish/video/init/", headers=tt_headers(), json_body={
        "post_info": {"title": caption[:2200], "privacy_level": privacy,
                      "disable_duet": not allow_duet or bool(info.get("duet_disabled")),
                      "disable_comment": not allow_comment or bool(info.get("comment_disabled")),
                      "disable_stitch": not allow_stitch or bool(info.get("stitch_disabled")),
                      "video_cover_timestamp_ms": 1000,
                      "brand_organic_toggle": bool(brand_organic), "brand_content_toggle": bool(brand_content)},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}})
    data = r.get("data") or {}
    publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
    if not (publish_id and upload_url):
        raise PostError(f"TikTok did not start the upload: {describe_error(r)}")
    with clip.open("rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            body = f.read(end - start + 1)
            for attempt in range(3):
                try:
                    http("PUT", upload_url, data=body, timeout=600, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                        "Content-Range": f"bytes {start}-{end}/{size}"})
                    break
                except PostError:
                    if attempt == 2:
                        raise
                    time.sleep(5)
            if count > 1:
                say_(f"  TikTok: part {i + 1} of {count} sent")
    say_("  TikTok: upload finished, TikTok is processing the video")
    deadline = time.time() + 15 * 60
    while True:
        s = http("POST", f"{TT_API}/v2/post/publish/status/fetch/", headers=tt_headers(),
                 json_body={"publish_id": publish_id}).get("data") or {}
        st = s.get("status")
        if st in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            ids = s.get("publicaly_available_post_id") or s.get("publicly_available_post_id") or []
            return {"id": publish_id, "status": st, "post_ids": ids}
        if st == "FAILED":
            raise PostError(f"TikTok could not post the clip: {s.get('fail_reason')}")
        if time.time() > deadline:
            raise PostError(f"TikTok was still processing after 15 minutes (publish id {publish_id}).")
        say_(f"  TikTok: {st or 'waiting'}")
        time.sleep(8)


# ------------------------------------------------------------------------------------------------ commands
def log_post(entry):
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def already_posted(clip, platform):
    if not LOG_FILE.is_file():
        return False
    key = str(clip.resolve())
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("clip") == key and e.get("platform") == platform and e.get("ok"):
            return True
    return False


def cmd_post(args):
    clip = Path(args.clip).expanduser()
    if not clip.is_file():
        raise PostError(f"Clip not found: {clip}")
    caption = args.caption or ""
    if args.caption_file:
        caption = Path(args.caption_file).read_text(encoding="utf-8").strip()
    targets = [t.strip().lower() for t in args.to.split(",") if t.strip()]
    bad = [t for t in targets if t not in ("instagram", "tiktok")]
    if bad:
        raise PostError(f"Unknown platform: {', '.join(bad)}. Use instagram, tiktok or both.")
    say(f"Clip: {clip.name}  ({clip.stat().st_size / 2**20:.1f} MB)")
    say(f"Caption: {caption[:120]}{'...' if len(caption) > 120 else ''}")
    if args.dry_run:
        say("Dry run: nothing was posted.")
        return 0
    failures = 0
    for platform in targets:
        if already_posted(clip, platform) and not args.again:
            say(f"  {platform}: already posted before; skipped (use --again to post it again)")
            continue
        try:
            res = post_instagram(clip, caption) if platform == "instagram" else post_tiktok(clip, caption, args.privacy)
            say(f"  {platform}: posted  {res.get('url') or res.get('status') or ''}")
            log_post({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "clip": str(clip.resolve()), "platform": platform,
                      "ok": True, "caption": caption, **res})
        except PostError as e:
            failures += 1
            say(f"  {platform}: FAILED - {e}")
            log_post({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "clip": str(clip.resolve()), "platform": platform,
                      "ok": False, "error": str(e)})
    return 1 if failures else 0


def cmd_check(_args):
    env = load_env()
    tokens = load_tokens()
    say(f"Settings file: {ENV_FILE} ({'found' if ENV_FILE.is_file() else 'missing - copy .env.example to .env'})")
    for k in ("IG_APP_ID", "IG_APP_SECRET", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"):
        say(f"  {k:22} {'set' if env.get(k) else 'MISSING'}")
    say(f"  {'TIKTOK_PRIVACY':22} {env.get('TIKTOK_PRIVACY', 'SELF_ONLY (default)')}")
    try:
        host = public_host()
        say(f"Tailscale name: {host}")
        say(f"  Instagram redirect to register: https://{host}:{FUNNEL_PORT}/instagram/callback")
    except PostError as e:
        say(f"Tailscale: {e}")
    say(f"  TikTok redirect to register:    {tt_redirect_uri()}")
    ig = tokens.get("instagram")
    say(f"Instagram: {'connected as @' + str(ig.get('username')) + ', valid ' + str(int((ig['expires_at'] - time.time()) / 86400)) + ' more days' if ig else 'not connected (python post.py auth instagram)'}")
    tt = tokens.get("tiktok")
    say(f"TikTok:    {'connected (sign-in renews itself for ' + str(int((tt['refresh_expires_at'] - time.time()) / 86400)) + ' days)' if tt else 'not connected (python post.py auth tiktok)'}")
    return 0


def cmd_history(_args):
    if not LOG_FILE.is_file():
        say("Nothing posted yet.")
        return 0
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines()[-50:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        say(f"{e['time']}  {e['platform']:9}  {'ok    ' if e.get('ok') else 'FAILED'}  {Path(e['clip']).name}  "
            f"{e.get('url') or e.get('status') or e.get('error', '')}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="post.py", description="Post CUTWORK clips to Instagram Reels and TikTok.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="show what is set up")
    a = sub.add_parser("auth", help="sign in to a platform once")
    a.add_argument("platform", choices=["instagram", "tiktok"])
    p = sub.add_parser("post", help="post one clip")
    p.add_argument("clip")
    p.add_argument("--caption", default="")
    p.add_argument("--caption-file")
    p.add_argument("--to", default="instagram,tiktok", help="instagram, tiktok or both (comma-separated)")
    p.add_argument("--privacy", help="TikTok privacy, e.g. PUBLIC_TO_EVERYONE or SELF_ONLY")
    p.add_argument("--again", action="store_true", help="post even if this clip was posted there before")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("history", help="list what has been posted")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check":
            return cmd_check(args)
        if args.cmd == "auth":
            env = load_env()
            (auth_instagram if args.platform == "instagram" else auth_tiktok)(env)
            return 0
        if args.cmd == "post":
            return cmd_post(args)
        if args.cmd == "history":
            return cmd_history(args)
    except PostError as e:
        say(f"\n  {e}")
        return 1
    except KeyboardInterrupt:
        say("\n  Stopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
