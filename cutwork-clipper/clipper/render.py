"""Render a moment as a 1080x1920 clip: reframe, burned captions, hook, watermark."""
import os
import re
import shutil
import textwrap
from pathlib import Path

from .media import MediaError, probe, run

ROOT = Path(__file__).resolve().parent.parent
FONT_FILE = ROOT / "assets" / "fonts" / "Inter-ExtraBold.otf"
WATERMARK = ROOT / "assets" / "watermark-white.png"
CASCADE = ROOT / "assets" / "haarcascade_frontalface_default.xml"
ORANGE_ASS = "&H1A4FFF&"   # #FF4F1A in ASS (BGR)
WHITE_ASS = "&HFFFFFF&"

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Inter ExtraBold,88,&H00FFFFFF,&H00FFFFFF,&H00111111,&H96000000,0,0,0,0,100,100,0,0,1,8,3,2,70,70,560,1
Style: Hook,Inter ExtraBold,60,&H00111111,&H00111111,&H00EEF2F4,&H00EEF2F4,0,0,0,0,100,100,0,0,3,16,0,8,90,90,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _t(sec):
    cs = int(round(max(0.0, sec) * 100))          # whole centiseconds, so 59.999 never prints as 0:00:60.00
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    return f"{h}:{m:02d}:{cs // 100:02d}.{cs % 100:02d}"


def _esc(text):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text.replace("{", "(").replace("}", ")").replace("\\", "/")


def build_ass(words, start, end, hook="", captions=True, hook_seconds=3.5):
    lines = [ASS_HEAD]
    hook = _esc(hook or "")
    if hook:
        wrapped = "\\N".join(textwrap.wrap(hook.upper(), 22)[:3])
        lines.append(f"Dialogue: 1,{_t(0)},{_t(min(hook_seconds, end - start))},Hook,,0,0,0,,{wrapped}")
    if captions:
        ws = [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05 and _esc(w["w"])]
        groups, cur = [], []
        for i, w in enumerate(ws):
            cur.append(w)
            nxt = ws[i + 1] if i + 1 < len(ws) else None
            chars = sum(len(x["w"]) + 1 for x in cur)
            if (not nxt or len(cur) >= 3 or chars >= 16 or w["w"][-1:] in ".?!,"
                    or nxt["s"] - w["e"] > 0.6):
                groups.append(cur); cur = []
        for gi, g in enumerate(groups):
            # a caption line never stays up into the next line's first word, so two lines never stack
            limit = min(end, groups[gi + 1][0]["s"]) if gi + 1 < len(groups) else end
            for k, w in enumerate(g):
                t0 = max(0.0, w["s"] - start)
                t1 = (g[k + 1]["s"] if k + 1 < len(g) else min(w["e"] + 0.25, limit)) - start
                if t1 <= t0:
                    t1 = t0 + 0.1
                parts = []
                for n, x in enumerate(g):
                    word = _esc(x["w"]).upper()
                    parts.append(f"{{\\c{ORANGE_ASS}}}{word}{{\\c{WHITE_ASS}}}" if n == k else word)
                lines.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Cap,,0,0,0,,{' '.join(parts)}")
    return "\n".join(lines) + "\n"


def shot_starts(source, start, end):
    """Seconds from the clip's start at which the picture cuts to another camera."""
    try:
        p = run(["ffmpeg", "-hide_banner", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(source),
                 "-an", "-vf", "scale=320:-2,select='gt(scene,0.3)',showinfo", "-f", "null", "-"], timeout=600)
    except MediaError:
        return []
    cuts = []
    for t in re.findall(r"pts_time:([0-9.]+)", p.stderr or ""):
        t = float(t)
        if t > 0.3 and (not cuts or t - cuts[-1] > 0.4):
            cuts.append(t)
    return cuts[:40]


def face_center_x(source, start, end, samples=8, need=2):
    """Median horizontal face position (0-1), or None. Needs opencv (optional)."""
    try:
        import cv2
    except Exception:
        return None
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return None
    try:
        det = cv2.CascadeClassifier(str(CASCADE))
        if det.empty():
            return None
        xs = []
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + (end - start) * (i + 0.5) / samples) * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            scale = 640 / w if w > 640 else 1
            small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1 else frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = det.detectMultiScale(gray, 1.15, 6, minSize=(50, 50))
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                xs.append((x + fw / 2) / small.shape[1])
    finally:
        cap.release()
    if len(xs) < need:
        return None
    xs.sort()
    return xs[len(xs) // 2]


def _crop_x(source, W, crop_w, start, end):
    """Where the vertical crop sits: a number, or an ffmpeg expression that moves it at each camera cut."""
    def left(cx):
        return int(min(max((cx if cx is not None else 0.5) * W - crop_w / 2, 0), W - crop_w))
    cuts = shot_starts(source, start, end)
    if not cuts:
        return str(left(face_center_x(source, start, end)))
    edges = [0.0] + cuts + [end - start]
    xs = []
    for a, b in zip(edges, edges[1:]):
        n = min(6, max(2, int((b - a) / 1.5)))
        x = left(face_center_x(source, start + a + 0.1, start + max(b - 0.1, a + 0.2), samples=n, need=1))
        if xs and abs(x - xs[-1]) < 0.04 * W:
            x = xs[-1]          # the same shot, or near enough: hold still
        xs.append(x)
    expr = str(xs[-1])
    for t, x in zip(reversed(cuts), reversed(xs[:-1])):
        expr = f"if(lt(t,{t - 0.001:.3f}),{x},{expr})"
    return f"'{expr}'"


def _base_filter(source, W, H, start, end, framing):
    """Filter that turns the source picture into 1080x1920, labelled [b]."""
    crop_w = int(H * 9 / 16) // 2 * 2
    if framing == "face" and W and H and 2 <= crop_w <= W:
        try:
            x = _crop_x(source, W, crop_w, start, end)
        except Exception:
            x = str((W - crop_w) // 2)   # face detection is a nice-to-have; fall back to a centre crop
        return f"[0:v]crop={crop_w}:{H}:{x}:0,scale=1080:1920,setsar=1[b]"
    if W and H and abs(W / H - 9 / 16) < 0.02:      # already vertical: just resize
        return "[0:v]scale=1080:1920,setsar=1[b]"
    # "fit": whole frame over a blurred background (screen recordings, narrow videos)
    return ("[0:v]split[a][c];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2[bg];"
            "[c]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[b]")


def render_clip(source, out_path, words, start, end, hook="", framing="face",
                captions=True, watermark=True):
    out_path = Path(out_path)
    work = out_path.parent
    work.mkdir(parents=True, exist_ok=True)
    if not FONT_FILE.is_file():
        raise MediaError("The caption font is missing from assets/fonts. Download the app again.")
    fonts = work / "_fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    if not (fonts / FONT_FILE.name).exists():
        shutil.copy(FONT_FILE, fonts / FONT_FILE.name)

    info = probe(source)
    if not info["has_video"]:
        raise MediaError("This file has sound but no picture, so there is nothing to clip.")
    start = max(0.0, float(start))
    if info["duration"]:
        end = min(float(end), info["duration"])
    if end - start < 1:
        raise MediaError("This clip starts after the end of the video.")
    base = _base_filter(source, info["width"], info["height"], start, end, framing)

    ass_name = out_path.stem + ".ass"
    tmp_name = out_path.stem + ".part.mp4"       # only renamed to the final name once ffmpeg has finished
    (work / ass_name).write_text(build_ass(words, start, end, hook, captions), encoding="utf-8")
    chain = base + f";[b]subtitles={ass_name}:fontsdir=_fonts[s]"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(source)]
    if watermark and WATERMARK.exists():
        cmd += ["-i", str(WATERMARK)]
        # a soft dark shadow under the mark keeps it readable on white and bright backgrounds
        chain += (";[1:v]scale=250:-1,format=rgba,pad=iw+40:ih+40:20:20:color=black@0,split[w1][w2];"
                  "[w1]colorchannelmixer=aa=0.9[wm];"
                  "[w2]colorchannelmixer=rr=0:gg=0:bb=0:aa=0.75,gblur=sigma=5[sh];"
                  "[s][sh]overlay=28:112[s2];[s2][wm]overlay=28:110[v]")
        vmap = "[v]"
    else:
        vmap = "[s]"
    cmd += ["-filter_complex", chain, "-map", vmap, "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000",
            "-max_muxing_queue_size", "2048", "-movflags", "+faststart", tmp_name]
    try:
        run(cmd, cwd=str(work), timeout=1800)   # relative paths avoid Windows filter-escaping problems
        if not (work / tmp_name).is_file() or (work / tmp_name).stat().st_size < 2000:
            raise MediaError("ffmpeg produced an empty clip. Check the start and end times.")
        os.replace(work / tmp_name, out_path)
    finally:
        (work / ass_name).unlink(missing_ok=True)
        (work / tmp_name).unlink(missing_ok=True)
    return out_path
