"""ffmpeg/ffprobe helpers: find the tools wherever they are installed, run them safely, read video facts."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TOOLS = {}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MediaError(RuntimeError):
    """A problem the user can act on; the message is shown in the page as it is."""


def _candidates(name):
    exe = name + (".exe" if os.name == "nt" else "")
    env = os.environ.get("CUTWORK_FFMPEG_DIR")
    if env:
        yield Path(env) / exe
    yield ROOT / "bin" / exe                       # a copy dropped next to the app
    found = shutil.which(name)
    if found:
        yield Path(found)
    if os.name == "nt":
        # winget installs ffmpeg here, but the PATH change only reaches programs started after a restart
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        yield local / "Microsoft" / "WinGet" / "Links" / exe
        pk = local / "Microsoft" / "WinGet" / "Packages"
        if pk.is_dir():
            yield from sorted(pk.glob(f"*FFmpeg*/**/bin/{exe}"), reverse=True)
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramData"), "C:\\"):
            if base:
                yield Path(base) / "ffmpeg" / "bin" / exe
                yield Path(base) / "chocolatey" / "bin" / exe
        yield Path.home() / "scoop" / "shims" / exe
    else:
        for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
            yield Path(d) / exe


def tool(name):
    """Absolute path of ffmpeg or ffprobe, or None."""
    if name not in _TOOLS:
        hit = None
        for c in _candidates(name):
            try:
                if c.is_file():
                    hit = str(c)
                    break
            except OSError:
                continue
        if hit is None and name == "ffmpeg":
            try:                                    # optional pip package that ships an ffmpeg binary
                import imageio_ffmpeg
                hit = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                hit = None
        _TOOLS[name] = hit
    return _TOOLS[name]


def check_tools():
    if not tool("ffmpeg"):
        how = ("open PowerShell, run  winget install Gyan.FFmpeg  and start the app again" if os.name == "nt"
               else "install it with  brew install ffmpeg  (Mac) or  sudo apt install ffmpeg  (Linux)")
        raise MediaError(f"ffmpeg was not found on this computer. To fix: {how}.")


def run(cmd, cwd=None, timeout=None):
    """Run ffmpeg/ffprobe. cmd[0] is 'ffmpeg' or 'ffprobe'; it is swapped for the real path."""
    exe = tool(cmd[0])
    if not exe:
        check_tools()
        raise MediaError(f"{cmd[0]} was not found.")
    full = [exe] + [str(c) for c in cmd[1:]]
    try:
        p = subprocess.run(full, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise MediaError("ffmpeg took too long and was stopped. The video file may be damaged.")
    except OSError as e:
        raise MediaError(f"Could not start ffmpeg: {e}")
    if p.returncode != 0:
        tail = [ln for ln in (p.stderr or "").strip().splitlines() if ln.strip()][-4:]
        text = " | ".join(tail)
        low = text.lower()
        if "no space left" in low:
            raise MediaError("The disk is full. Free some space and try again.")
        if "invalid data found" in low or "moov atom not found" in low:
            raise MediaError("This file is not a video ffmpeg can read. It may be damaged or still downloading.")
        if "permission denied" in low:
            raise MediaError("The app was not allowed to read or write a file. Close any program that has the video open.")
        raise MediaError("ffmpeg failed: " + (text or f"exit code {p.returncode}"))
    return p


def _probe_ffprobe(path):
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height,duration:stream_tags=rotate:stream_side_data=rotation",
             "-of", "json", str(path)], timeout=120)
    data = json.loads(p.stdout or "{}")
    info = {"duration": float(data.get("format", {}).get("duration", 0) or 0), "width": 0, "height": 0,
            "has_audio": False, "has_video": False, "rotation": 0}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["has_video"] and s.get("width"):
            info["has_video"] = True
            info["width"], info["height"] = int(s["width"]), int(s["height"])
            rot = (s.get("tags") or {}).get("rotate")
            for sd in s.get("side_data_list") or []:
                if "rotation" in sd:
                    rot = sd["rotation"]
            try:
                info["rotation"] = int(round(float(rot))) % 360 if rot is not None else 0
            except (TypeError, ValueError):
                info["rotation"] = 0
        if s.get("codec_type") == "audio":
            info["has_audio"] = True
        if not info["duration"] and s.get("duration"):
            try:
                info["duration"] = float(s["duration"])
            except ValueError:
                pass
    return info


def _probe_ffmpeg(path):
    """Fallback when only ffmpeg exists: read the banner that 'ffmpeg -i' prints."""
    p = subprocess.run([tool("ffmpeg"), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                       timeout=120)
    err = p.stderr or ""
    info = {"duration": 0.0, "width": 0, "height": 0, "has_audio": False, "has_video": False, "rotation": 0}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Stream #.*Video:.*?[ ,](\d{2,5})x(\d{2,5})[ ,\[]", err)
    if m:
        info["has_video"] = True
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    info["has_audio"] = bool(re.search(r"Stream #.*Audio:", err))
    m = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", err)
    if m:
        info["rotation"] = int(round(float(m.group(1)))) % 360
    if not info["has_video"] and not info["has_audio"]:
        raise MediaError("This file is not a video ffmpeg can read. It may be damaged or still downloading.")
    return info


def probe(path):
    """{"duration", "width", "height", "has_audio", "has_video"}; width/height are as displayed (rotation applied)."""
    if not Path(path).is_file():
        raise MediaError(f"The video file is missing: {path}")
    info = _probe_ffprobe(path) if tool("ffprobe") else _probe_ffmpeg(path)
    if info["rotation"] in (90, 270):       # phone videos: ffmpeg rotates them while decoding
        info["width"], info["height"] = info["height"], info["width"]
    return info


def free_gb(path):
    try:
        return shutil.disk_usage(str(path)).free / 2 ** 30
    except OSError:
        return None


if __name__ == "__main__":
    print("ffmpeg :", tool("ffmpeg"))
    print("ffprobe:", tool("ffprobe"))
    if len(sys.argv) > 1:
        print(probe(sys.argv[1]))
