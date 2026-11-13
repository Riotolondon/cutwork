# CUTWORK link helper

The web app at `/app` runs in the browser, and a browser cannot download from
YouTube and similar sites by itself. This small server does the download and
hands the finished video to the web app.

## Use it on your own computer

1. Start the clipper once (`cutwork-clipper/run.bat`) so its Python packages are installed.
2. Double-click `cutwork-link/run.bat` and keep the window open.
3. Open the web app in Chrome or Edge on the same computer and paste a link.
   The browser may ask to allow access to devices on your local network: allow it.

## Publish it for everyone

Run the helper on a machine that is always on, give it an `https://` address,
and put that address first in `docs/app/config.js`.

- `HOST=0.0.0.0` makes it listen on all addresses (the default is this computer only).
- `CUTWORK_LINK_ORIGINS` lists the sites allowed to call it (default: the CUTWORK site).
- YouTube blocks many cloud-server addresses. A home or office connection works;
  a rented server may not.

## Limits

- Only well-known video sites are accepted, so the helper cannot be used to reach other addresses.
- Videos up to 3 hours and 1.8 GB, at up to 1080p. Live streams are refused.
- One download at a time; downloads are deleted after an hour.
- Only clip videos you have permission to use.
