"""End-to-end test of CUTWORK Clipper. Starts the real app on a spare port and drives it over HTTP.

    .venv\\Scripts\\python.exe tests\\smoke_test.py            (everything, about 3 to 6 minutes)
    .venv\\Scripts\\python.exe tests\\smoke_test.py --quick    (skips real speech recognition)

Needs tests/media/talk.mp4 (make it with tests/make_test_video.ps1). Nothing here touches your real projects:
the app is pointed at a throw-away folder under tests/_work.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from clipper.media import probe, run, tool  # noqa: E402
from clipper import moments as M  # noqa: E402
from clipper.render import build_ass, _t  # noqa: E402

WORK = HERE / "_work"
MEDIA = HERE / "media"
TALK = MEDIA / "talk.mp4"
PORT = 5199
BASE = f"http://127.0.0.1:{PORT}"
QUICK = "--quick" in sys.argv
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)
    return bool(ok)


def call(path, form=None, js=None, method=None, files=None):
    url = BASE + path
    headers, data = {}, None
    if files:
        b = uuid.uuid4().hex
        parts = []
        for k, v in (form or {}).items():
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fname, content) in files.items():
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                         f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
        data = b"".join(parts) + f"--{b}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={b}"
    elif js is not None:
        data = json.dumps(js).encode() if not isinstance(js, bytes) else js
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = r.read()
            return r.status, (json.loads(body) if r.headers.get("Content-Type", "").startswith("application/json") else body)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"_raw": body[:200].decode("utf-8", "replace")}


class Server:
    def __init__(self, fake=None, strip_path=True):
        env = dict(os.environ, PORT=str(PORT), CUTWORK_PROJECTS=str(WORK / "projects"), CUTWORK_LIBRARY=str(WORK / "library"), CUTWORK_CLOUD=str(WORK / "cloud"), CUTWORK_NO_BROWSER="1",
                   PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        env.pop("CUTWORK_FAKE_TRANSCRIPT", None)
        if fake:
            env["CUTWORK_FAKE_TRANSCRIPT"] = str(fake)
        if strip_path:   # the app must find ffmpeg by itself, as it has to right after a winget install
            env["PATH"] = os.pathsep.join(d for d in env.get("PATH", "").split(os.pathsep)
                                          if "ffmpeg" not in d.lower() and "winget" not in d.lower())
        self.log = open(WORK / "server.log", "ab")
        self.p = subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=str(ROOT), env=env,
                                  stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                if call("/api/health")[0] == 200:
                    return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("the app did not start; see tests/_work/server.log")

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.log.close()
        time.sleep(0.5)


def wait_ready(pid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, p = call(f"/api/projects/{pid}")
        if code == 200 and p["status"]["stage"] in ("ready", "error"):
            return p
        time.sleep(0.7)
    return {"status": {"stage": "timeout", "error": "timed out", "pct": 0}, "moments": [], "export": {}}


def wait_export(pid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, p = call(f"/api/projects/{pid}")
        if code == 200 and not p["export"].get("running"):
            return p
        time.sleep(0.7)
    return p


def new(**form):
    f = {"model": "base", "min_len": 6, "max_len": 25, "count": 6, "language": "en", "keywords": "", "url": "", "path": ""}
    f.update(form)
    return call("/api/projects", form=f)


def make(name, args):
    out = MEDIA / name
    if not out.exists():
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args + [str(out)])
    return out


def clip_ok(pid, clip, want_len):
    f = WORK / "projects" / pid / "clips" / clip["file"]
    if not f.is_file():
        return False, "file missing"
    i = probe(f)
    ok = (i["width"], i["height"]) == (1080, 1920) and i["has_audio"] and abs(i["duration"] - want_len) < 1.0
    return ok, f"{i['width']}x{i['height']} audio={i['has_audio']} dur={i['duration']:.2f} want={want_len:.2f}"


def export_and_check(label, pid, p, framing, n=1, hook="Why GPUs are the new oil"):
    picks = [dict(id=m["id"], start=m["start"], end=m["end"], text=m["text"], hook=hook if i == 0 else "")
             for i, m in enumerate(p["moments"][:n])]
    check(f"{label}: every moment lies inside the video", all(m["end"] <= p["duration"] + 0.01 for m in p["moments"]),
          f"duration {p['duration']}")
    code, r = call(f"/api/projects/{pid}/export", js={"moments": picks, "framing": framing, "captions": True, "watermark": True})
    if not check(f"{label}: export accepted", code == 200, str(r)):
        return None
    p = wait_export(pid)
    ex = p["export"]
    check(f"{label}: export finished without error", not ex.get("error") and len(ex["clips"]) >= len(picks), str(ex.get("error")))
    for pk in picks:
        c = next((c for c in ex["clips"] if abs(c["start"] - pk["start"]) < 0.01 and framing in c["file"]), None)
        ok, det = clip_ok(pid, c, pk["end"] - pk["start"]) if c else (False, "clip not listed")
        check(f"{label}: clip is 1080x1920 with sound and the right length", ok, det)
    return p


def unit_tests():
    print("\nUnit checks")
    check("time format never prints 60 seconds", _t(59.999) == "0:01:00.00" and _t(3599.996) == "1:00:00.00" and _t(-1) == "0:00:00.00",
          f"{_t(59.999)} {_t(3599.996)}")
    words = [{"s": i * 0.4, "e": i * 0.4 + 0.3, "w": w} for i, w in enumerate("Hello {there} this\\is a test. Next line here now".split())]
    ass = build_ass(words, 0, 5, hook="line one\nline {two}")
    check("captions escape braces, backslashes and line breaks", "{there}" not in ass and "\\is" not in ass and "LINE ONE LINE (TWO)" in ass)
    ev = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0")]
    times = []
    for ln in ev:
        a, b = ln.split(",")[1:3]
        sec = lambda t: int(t.split(":")[0]) * 3600 + int(t.split(":")[1]) * 60 + float(t.split(":")[2])  # noqa: E731
        times.append((sec(a), sec(b)))
    check("caption lines never overlap in time", all(times[i][1] <= times[i + 1][0] + 1e-6 for i in range(len(times) - 1)) and all(b > a for a, b in times))
    got = M.import_moments([{"start": "x", "end": 5}, {"start": 10, "end": 2}, {"start": 1, "end": 9, "score": "high", "hook": None},
                            {"start": float("nan"), "end": 4}, {"end": 3}], words)
    check("pasted moments: bad entries skipped, good ones kept", len(got) == 2 and all(g["end"] - g["start"] >= 3 for g in got), str(got))
    check("no moments from an empty transcript", M.find_moments([]) == [] and M.find_moments([{"s": 0, "e": 1, "w": "hi"}]) == [])


def api_validation():
    print("\nBad input is refused with a clear message")
    for label, form in (("nothing chosen", {}), ("letters in a number box", {"path": str(TALK), "min_len": "abc"}),
                        ("shortest longer than longest", {"path": str(TALK), "min_len": 50, "max_len": 20}),
                        ("file that does not exist", {"path": "C:\\nope\\missing video.mp4"}),
                        ("link that is not a link", {"url": "not a link"})):
        code, r = new(**form)
        check(f"{label} -> 400 with a message", code == 400 and r.get("error"), f"{code} {r}")
    code, r = call("/api/projects/doesnotexist")
    check("unknown video -> 404 as JSON", code == 404 and r.get("error"), f"{code} {r}")
    code, r = call("/api/projects/doesnotexist/export", js={"moments": []})
    check("export on unknown video -> 404 as JSON", code == 404 and r.get("error"))
    code, r = call("/nope")
    check("unknown page -> 404 as JSON", code == 404 and isinstance(r, dict) and r.get("error"))
    code, r = call("/")
    check("the page itself loads", code == 200 and b"CUTWORK" in r)
    check("no stray project folders left by refused requests",
          not any(d.is_dir() for d in (WORK / "projects").glob("*")), str(list((WORK / "projects").glob("*"))))


def real_run():
    print("\nReal run: speech recognition, moments, export (face and fit)")
    spaced = MEDIA / "my tést video – ½ (final).mp4"      # spaces, accents and symbols in the path
    if not spaced.exists():
        shutil.copy(TALK, spaced)
    code, r = new(path=str(spaced))
    if not check("project created from a path with spaces and accents", code == 200, str(r)):
        return None
    pid = r["id"]
    code2, r2 = new(path=str(TALK), model="base")       # a second video queued behind the first
    p = wait_ready(pid)
    if not check("video processed to ready", p["status"]["stage"] == "ready", str(p["status"])):
        return None
    words = json.loads((WORK / "projects" / pid / "transcript.json").read_text(encoding="utf-8"))["words"]
    text = " ".join(w["w"] for w in words).lower()
    check("transcript has the spoken words", len(words) > 150 and "nvidia" in text and "robots" in text, f"{len(words)} words")
    check("moments were found and scored", len(p["moments"]) >= 3 and all(0 <= m["score"] <= 100 for m in p["moments"]), str(len(p["moments"])))
    check("moments stay inside the video and inside the length limits",
          all(0 <= m["start"] < m["end"] <= p["duration"] + 1 and 5 <= m["end"] - m["start"] <= 26.5 for m in p["moments"]))
    check("preview file exists and plays in a browser (H.264 360p)", probe(WORK / "projects" / pid / "proxy.mp4")["height"] == 360)
    check("temporary audio file was cleaned up", not (WORK / "projects" / pid / "audio.wav").exists())
    code, body = call(f"/media/{pid}/proxy")
    check("preview is served", code == 200 and len(body) > 10000)
    export_and_check("face framing", pid, p, "face", n=2)
    lib = WORK / "library" / "my tést video – ½ (final)"
    made = sorted(x.name for x in lib.glob("*.mp4")) if lib.is_dir() else []
    check("clips are filed in a folder named after the video", len(made) == 2 and not list(lib.glob("*.part")), f"{lib} -> {made}")
    cloud = WORK / "cloud" / lib.name
    check("a copy of each clip lands in the cloud folder", sorted(x.name for x in cloud.glob("*.mp4")) == made and made, str(cloud))
    export_and_check("fit framing", pid, p, "fit", n=1)
    code, r = call(f"/api/projects/{pid}/claude_prompt")
    check("Claude prompt contains the transcript", code == 200 and "TRANSCRIPT:" in r["prompt"] and "[0." in r["prompt"])
    reply = 'Sure! Here you go:\n```json\n[{"start": 12.0, "end": 30.5, "hook": "The data is the product", "reason": "strong"}, ' \
            '{"start": "bad", "end": 3}, {"start": 40, "end": 41}]\n```'
    code, r = call(f"/api/projects/{pid}/import", js={"text": reply})
    check("Claude reply with extra text and bad rows is imported", code == 200 and r.get("added") == 1, str(r))
    for junk in ("", "hello", "[1,2,3]", "[{]", '{"start":1}'):
        code, r = call(f"/api/projects/{pid}/import", js={"text": junk})
        if not (code == 400 and r.get("error")):
            check(f"junk import refused: {junk!r}", False, f"{code} {r}")
            break
    else:
        check("junk pasted into the import box is refused politely", True)
    code, r = call(f"/api/projects/{pid}/import", js=b"this is not json")
    check("malformed request body does not crash the app", code == 400, f"{code} {r}")
    # export edge cases
    m0 = p["moments"][0]
    for label, picks, want in (
            ("clip under 3 seconds", [dict(id=1, start=5, end=6)], 400), ("end before start", [dict(id=1, start=20, end=5)], 400),
            ("text instead of a time", [dict(id=1, start="a", end=9)], 400), ("no clips ticked", [], 400),
            ("start beyond the end of the video", [dict(id=1, start=p["duration"] + 50, end=p["duration"] + 60)], 400)):
        code, r = call(f"/api/projects/{pid}/export", js={"moments": picks})
        check(f"export refused: {label}", code == want and r.get("error"), f"{code} {r}")
    tail = [dict(id=999, start=max(0, p["duration"] - 6), end=p["duration"] + 30, text="", hook="")]
    code, r = call(f"/api/projects/{pid}/export", js={"moments": tail, "framing": "weird", "captions": True, "watermark": False})
    px = wait_export(pid)
    c = next((c for c in px["export"]["clips"] if abs(c["start"] - tail[0]["start"]) < 0.01), None)
    f = WORK / "projects" / pid / "clips" / c["file"] if c else None
    check("clip running past the end of the video is trimmed, not broken",
          code == 200 and c and not px["export"].get("error") and 4 < probe(f)["duration"] < 7.5, str(px["export"].get("error")))
    code, _ = call(f"/api/projects/{pid}/export", js={"moments": [dict(id=m0["id"], start=m0["start"], end=m0["end"])]})
    code_b, r_b = call(f"/api/projects/{pid}/export", js={"moments": [dict(id=m0["id"], start=m0["start"], end=m0["end"])]})
    check("a second export while one is running is refused", code == 200 and code_b == 400, f"{code} {code_b} {r_b}")
    wait_export(pid)
    leftovers = [x.name for x in (WORK / "projects" / pid / "clips").iterdir() if x.suffix in (".ass",) or ".part" in x.name]
    check("no temporary files left in the clips folder", not leftovers, str(leftovers))
    if code2 == 200:
        p2 = wait_ready(r2["id"])
        check("a second video queued at the same time also finishes", p2["status"]["stage"] == "ready", str(p2["status"]))
    return pid, words


def variants(words):
    print("\nAwkward videos")
    cut = ["-t", "24", "-i", str(TALK)]
    vs = {
        "vertical phone video (1080x1920)": make("v_vertical.mp4", cut + ["-vf", "crop=608:1080,scale=1080:1920", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]),
        "square video": make("v_square.mp4", cut + ["-vf", "crop=1080:1080", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]),
        "odd size, 10-bit colour, 60 fps, mkv": make("v_odd.mkv", cut + ["-vf", "scale=853:481,fps=60", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p10le", "-c:a", "libmp3lame"]),
        "phone video stored sideways (rotation flag)": make("v_rotated.mp4", ["-display_rotation", "90"] + cut + ["-c", "copy"]),
        "tiny low-resolution video": make("v_tiny.mp4", cut + ["-vf", "scale=320:180", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "1", "-ar", "8000"]),
        "webm (VP9 + Opus)": make("v_web.webm", ["-t", "12", "-i", str(TALK), "-vf", "scale=640:360", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus"]),
        "two audio tracks and a subtitle track": make("v_multi.mkv", cut + ["-f", "lavfi", "-t", "24", "-i", "sine=frequency=300", "-map", "0:v", "-map", "0:a", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-vf", "scale=1280:720", "-c:a", "aac"]),
    }
    for label, src in vs.items():
        code, r = new(path=str(src))
        if not check(f"{label}: accepted", code == 200, str(r)):
            continue
        p = wait_ready(r["id"])
        if not check(f"{label}: processed", p["status"]["stage"] == "ready" and p["moments"], str(p["status"])):
            continue
        for fr in ("face", "fit"):
            export_and_check(f"{label} / {fr}", r["id"], p, fr, n=1, hook="")
    print("\nFiles that cannot work give a clear reason")
    bad = {
        "video with no sound": (make("b_silent.mp4", ["-t", "6", "-i", str(TALK), "-an", "-c:v", "copy"]), "no audio"),
        "sound with no picture": (make("b_audio.mp3", ["-t", "6", "-i", str(TALK), "-vn", "-c:a", "libmp3lame"]), "no picture"),
    }
    junk = MEDIA / "b_junk.mp4"
    junk.write_bytes(b"this is not a video at all " * 400)
    bad["a file that is not a video"] = (junk, "not a video")
    half = MEDIA / "b_cut.mp4"
    half.write_bytes(TALK.read_bytes()[:300_000])
    bad["a download that was cut short"] = (half, "")
    silent = make("b_nospeech.mp4", ["-f", "lavfi", "-t", "8", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-t", "8", "-i", "anullsrc=r=44100:cl=stereo", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest"])
    for label, (src, needle) in bad.items():
        code, r = new(path=str(src))
        p = wait_ready(r["id"]) if code == 200 else {"status": {"stage": "?", "error": str(r)}}
        err = p["status"].get("error") or ""
        check(f"{label}: stops with a plain message", p["status"]["stage"] == "error" and needle in err.lower() and "Traceback" not in err, f"{p['status']}")
    return silent


def upload_retry_delete_restart(server, fake):
    print("\nUpload, retry, delete, restart")
    small = make("v_small.mp4", ["-t", "20", "-i", str(TALK), "-vf", "scale=640:360", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"])
    code, r = call("/api/projects", form={"model": "base", "min_len": 6, "max_len": 18, "count": 3}, files={"file": ("my upload (1).mp4", small.read_bytes())})
    ok = check("upload accepted", code == 200, str(r))
    up = wait_ready(r["id"]) if ok else None
    if ok and check("uploaded video processed", up["status"]["stage"] == "ready", str(up["status"])):
        export_and_check("uploaded video", r["id"], up, "face", n=1)
    code, r0 = call("/api/projects", form={}, files={"file": ("empty.mp4", b"")})
    check("empty upload refused", code == 400 and r0.get("error"), f"{code} {r0}")
    # retry: source disappears, export refuses, the file comes back, retry works
    moved = MEDIA / "v_moving.mp4"
    shutil.copy(small, moved)
    code, r2 = new(path=str(moved), max_len=18)
    p2 = wait_ready(r2["id"])
    hidden = moved.with_suffix(".hidden")
    moved.replace(hidden)
    code, e = call(f"/api/projects/{r2['id']}/export", js={"moments": [dict(id=1, start=1, end=8)]})
    check("export refused clearly when the original file was moved", code == 400 and "moved or deleted" in e.get("error", ""), f"{code} {e}")
    code, e = call(f"/api/projects/{r2['id']}/retry", method="POST")
    check("retry refused on a video that did not fail", code == 400, f"{code} {e}")
    hidden.replace(moved)
    code, rb = new(path=str(MEDIA / "b_junk.mp4"))
    pb = wait_ready(rb["id"])
    code, e = call(f"/api/projects/{rb['id']}/retry", method="POST")
    pb2 = wait_ready(rb["id"])
    check("retry runs again and reports the same clear error", code == 200 and pb2["status"]["stage"] == "error" and pb2["status"]["error"])
    code, e = call(f"/api/projects/{rb['id']}", method="DELETE")
    check("delete removes the project and its folder", code == 200 and not (WORK / "projects" / rb["id"]).exists() and call(f"/api/projects/{rb['id']}")[0] == 404)
    check("delete never touches a video that was added by path", (MEDIA / "b_junk.mp4").exists())
    # restart: everything is still there, and a project that was mid-way is recoverable
    code, r3 = new(path=str(small), max_len=18)
    time.sleep(0.4)                   # stop the app while this one is still being prepared
    before = {p["id"]: p["status"]["stage"] for p in call("/api/projects")[1]}
    server.stop()
    server2 = Server(fake=fake)
    after = {p["id"]: p for p in call("/api/projects")[1]}
    check("all videos are still listed after a restart", set(before) == set(after), f"{len(before)} vs {len(after)}")
    if ok:
        pu = call(f"/api/projects/{r['id']}")[1]
        clip = pu["export"]["clips"][0]["file"] if pu["export"]["clips"] else None
        code, body = call(f"/media/{r['id']}/clips/{urllib.parse.quote(clip)}") if clip else (0, b"")
        check("finished clips are still downloadable after a restart", code == 200 and len(body) > 50000)
    st = after[r3["id"]]["status"]
    if st["stage"] == "error":
        check("a video interrupted by closing the app says so", "closed" in (st["error"] or ""), str(st))
        code, e = call(f"/api/projects/{r3['id']}/retry", method="POST")
        p3 = wait_ready(r3["id"])
        check("...and Try again finishes it", code == 200 and p3["status"]["stage"] == "ready", str(p3["status"]))
    else:
        check("a video that finished just before the restart is intact", st["stage"] == "ready", str(st))
    code, r = call(f"/media/{r3['id']}/clips/..%2F..%2Fproject.json")
    check("clip download cannot reach files outside the clips folder", code == 404, str(code))
    return server2


def main():
    if not TALK.exists():
        sys.exit("tests/media/talk.mp4 is missing. Run tests/make_test_video.ps1 first.")
    if not tool("ffmpeg"):
        sys.exit("ffmpeg was not found.")
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "projects").mkdir(parents=True)
    t0 = time.time()
    unit_tests()
    fake = HERE / "media" / "talk_transcript.json"
    server = Server()
    try:
        check("the app found ffmpeg without it being on PATH", call("/api/health")[1].get("ffmpeg") is True)
        api_validation()
        if not QUICK or not fake.exists():
            got = real_run()
            if got:
                pid, words = got
                shutil.copy(WORK / "projects" / pid / "transcript.json", fake)
        server.stop()
        if not fake.exists():
            sys.exit("no transcript to continue with")
        words = json.loads(fake.read_text(encoding="utf-8"))["words"]
        server = Server(fake=fake)       # from here the saved transcript is reused, so only video handling is tested
        variants(words)
        server = upload_retry_delete_restart(server, fake)
    finally:
        server.stop()
    log = (WORK / "server.log").read_text(encoding="utf-8", errors="replace")
    check("no Python traceback reached the console", "Traceback (most recent call last)" not in log)
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)} of {len(results)} checks passed in {time.time() - t0:.0f}s")
    for name, _, detail in bad:
        print("  FAILED:", name, "|", detail)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
