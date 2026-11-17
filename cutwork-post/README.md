# cutwork-post

Posts finished CUTWORK clips to Instagram Reels and TikTok, straight through the official APIs. No paid service.

```
python post.py check                                   what is set up, and the exact addresses to register
python post.py auth instagram                          sign in once
python post.py auth tiktok                             sign in once
python post.py post "C:\path\clip.mp4" --caption "..." post to both
python post.py post clip.mp4 --to tiktok --caption "..."
python post.py history                                 what has been posted
```

A clip already posted to a platform is skipped next time unless you add `--again`.
Every post (and every failure) is written to `posted.jsonl`.

## Set up once

Run `python post.py check` first. It prints the two redirect addresses you need below.

### 1. Instagram (about 15 minutes)

1. In the Instagram app: Settings > Account type and tools > **Switch to professional account** (Business or Creator).
2. Go to https://developers.facebook.com/apps, sign in, **Create app**, choose the **Business** type (or the
   "Other" path that lets you add Instagram), and add the **Instagram** product.
3. Open **Instagram > API setup with Instagram login**.
   - Under *Generate access tokens*, **Add account** and add your Instagram account (it becomes a tester, so no
     App Review is needed for your own account).
   - Under *Set up Instagram business login* > *Business login settings*, add the **OAuth redirect URI** that
     `check` printed, e.g. `https://desktop-n7gaq4o.tail9d72f3.ts.net:8443/instagram/callback`.
   - Copy the **Instagram app ID** and **Instagram app secret** (not the Facebook app ID) into `.env`.
4. Run `python post.py auth instagram` and approve in the browser.

Instagram fetches the video from a public address, so while a clip posts it is served from this computer
through Tailscale Funnel on port 8443, at a random one-off path, and the port is closed again afterwards.
The laptop has to be on and connected while posting.

The Instagram sign-in lasts 60 days and renews itself whenever you post in its last month.

### 2. TikTok (about 15 minutes, plus TikTok's review)

1. Go to https://developers.tiktok.com, sign in, **Manage apps > Connect an app**.
2. Platform: **Desktop**. Add the products **Login Kit** and **Content Posting API**, and turn on **Direct Post**.
3. Scopes: `user.info.basic` and `video.publish`.
4. Login Kit redirect URI: `http://localhost:3455/callback/` (exactly, with the final slash).
5. Copy the **Client key** and **Client secret** into `.env`.
6. Run `python post.py auth tiktok` and approve in the browser.

Until TikTok audits the app, posts are private: TikTok accepts only `SELF_ONLY`, and it may require the TikTok
account itself to be private while testing. Ask for the audit in the developer portal (Content Posting API page).
Once approved, set `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE` in `.env`.

The TikTok sign-in renews itself (access every 2 hours, renewal valid 180 days).

## Video rules

CUTWORK clips (1080x1920, H.264, AAC, 30 fps, under 10 minutes) meet both platforms' rules.
Instagram Reels: MP4/MOV, 3 s to 15 min, up to 300 MB. TikTok: up to 4 GB; the longest clip allowed is shown by
TikTok per account. Instagram allows 100 API posts per 24 hours.

## Files

- `.env`: your keys (never committed)
- `tokens.json`: sign-in tokens (never committed)
- `posted.jsonl`: post history
