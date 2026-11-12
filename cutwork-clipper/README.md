# CUTWORK Clipper

A free clipping tool that runs on your own computer. It transcribes a long video,
marks and scores the best moments, lets you adjust them, and exports vertical
1080x1920 clips with word-by-word captions and the CUTWORK watermark.
No accounts, no API keys, no monthly fee.

## Set up (once)

1. Install **Python 3.10 or newer** (python.org). On Windows, tick "Add Python to PATH".
2. Install **ffmpeg**:
   - Windows: open PowerShell and run `winget install Gyan.FFmpeg` (no restart needed; the app finds it)
   - Mac: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
3. Start the app:
   - Windows: double-click `run.bat`
   - Mac / Linux: run `./run.sh`
4. The page opens by itself. If it does not, go to http://127.0.0.1:5177 in your browser.

The first start installs the Python packages (a few minutes). The first video
also downloads the speech model once (about 500 MB for "Balanced").

## Use it

1. **Add a video**: upload a file, paste its path, or paste a link.
2. **Review the marked moments**: they are ranked by score. Press Preview,
   tick the ones you want, fix start and end, and type an on-screen hook.
3. **Export**: choose the framing, then "Export ticked clips". Finished clips
   are in `projects/<id>/clips/` and can be downloaded from the page.

### Sharper picks with Claude

The built-in score is a set of simple rules (strong opening line, numbers,
on-niche keywords, clean ending). It is a first pass. For better picks, press
**Copy transcript for Claude**, paste it into Claude, and paste the reply back
into the box. Claude's picks appear at the top with hooks filled in.

## Good to know

- Speed: on a normal laptop, expect transcription to take roughly a quarter of
  the video's length on "Balanced". "Fast" is quicker but less accurate.
- "Fill the screen" centres on the speaker's face. Use "Whole frame on a
  blurred background" for screen recordings and demos.
- To use it from another device (for example over Tailscale), start it with
  `HOST=0.0.0.0 ./run.sh`. Only do this on a private network: there is no login.
- Every exported clip carries the CUTWORK watermark. A paid plan that removes it is planned.
- Finished clips are filed per video in the `Clips` folder next to the app. If OneDrive
  is set up on the computer, a copy also goes to `OneDrive/CUTWORK Clips/<video>` so it
  uploads by itself (a personal account is used first, then a work or school one). Set
  `CUTWORK_CLOUD` to another synced folder to use that instead, or to `off` to stop it.
- If a video fails, the page says why and offers **Try again**. Full details are
  written to `projects/clipper.log`.
- The × next to an earlier video removes it and its clips from the app. A video
  you added by path is never deleted; an uploaded copy is.
- Only one video is transcribed at a time; others wait in a queue.
- To check the whole app after changing anything, run
  `.venv\Scripts\python.exe tests\smoke_test.py` (make the test video first with
  `tests\make_test_video.ps1`).
- Only clip videos you have permission to use, such as campaign source files.
- Captions use the Inter typeface (SIL Open Font License).
