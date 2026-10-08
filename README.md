# opel-home-assistant

A maintained fork of [homeassistant-stellantis-vehicles-worker-v2](https://github.com/andreadegiovine/homeassistant-stellantis-vehicles-worker-v2):
the login service used by the [Stellantis Vehicles](https://github.com/andreadegiovine/homeassistant-stellantis-vehicles)
Home Assistant integration (Opel, Peugeot, Citroën, DS, Vauxhall).

Stellantis does not offer an official consumer API or Home Assistant integration, so the integration logs in like
the MyOpel app does. This worker opens the official login page in a headless Chromium, signs in, and returns the
OAuth code from the `mymopsdk://…` app redirect.

## What changed vs upstream

- **New Opel login page**: Opel replaced the Gigya login form with the ForgeRock AM page
  (`idpcvs.opel.com/am/XUI/#login/`), so upstream waited for a form that no longer exists. The worker now handles
  the ForgeRock page (`#idToken1` / `#idToken2` / `#loginButton_0`, "allow" consent button) as well as the old form.
- **New consent page**: after login Opel can show `id-dcr.opel.com/index/authorize-consentments`. The worker ticks
  only the checkboxes the page marks as required and clicks the accept/continue button (never decline). If a page
  still blocks it, the error lists that page's headings, buttons and checkboxes.
- **Cookie banner**: upstream clicked "Login" while a cookie overlay could still cover it, failing with
  `Page.click: Timeout 30000ms exceeded` (the error reported in the integration's issues). The banner is now
  dismissed first.
- **Code capture actually waits**: upstream's `asyncio.to_thread(lambda: captured_code)` returned at once instead of
  waiting for the redirect. It now waits on an event, and also reads the code from redirect `Location` headers.
- **Consent screen is optional**: accounts that already consented are redirected straight to the app; the worker no
  longer waits for a consent form that never shows up.
- **Clear errors**: a rejected password returns `Login error: …`, and a stall reports which page the flow stopped on.
- Browser auto-restarts if it crashes, the health check timeout is 10 s (was 10 000 s), Playwright 1.56 / Chromium 141.
- Runs on your own hardware (Home Assistant add-on or Docker), so no shared free-tier Render instance and your
  credentials never leave your network.

## Install as a Home Assistant add-on (HA OS / Supervised)

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add
   `https://github.com/jeffstahlnecker/opel-home-assistant`.
2. Install **Stellantis Login Worker** and start it (first build downloads a large Playwright image; give it a few
   minutes). Supports amd64 and aarch64 (e.g. Raspberry Pi 4/5).
3. Add or reconfigure the **Stellantis Vehicles** integration, choose MyOpel, and on the email/password step set
   **Login service URL** to `http://127.0.0.1:3789`
   (if that does not connect, use `http://<your-HA-IP>:3789`).

The add-on listens on host port **3789**. If another add-on already uses it, the add-on will not start: change the
port on the add-on's **Configuration → Network** section and use that port in the URL instead.

## Run with Docker (HA Container / Core)

```sh
docker compose up -d --build
```

Then use `http://<docker-host-ip>:3789` (change the left side of `3789:3000` in `docker-compose.yml` if that port is taken) as the **Login service URL**.

## Run on Render

`render.yaml` still works: create a Render Blueprint from this repo, then use the service URL as the
**Login service URL**. The free tier sleeps, so the first login after idle may time out; retry once.

## API

`POST /` with JSON `{"url": "<authorize url>", "email": "...", "password": "...", "debug": false}` returns
`{"code": "<oauth code>"}` or HTTP 400 `{"message": "...", "code": 400}`. Optional `timeout_page` (default 60000 ms)
and `timeout_input` (default 30000 ms). `GET /health` returns `{"status": "ok"}`.

## If it still fails

The login page is Stellantis's to change. Send `"debug": true` (or check the add-on log) and look at the last
step logged and the page reported in the error, then adjust `SELECTORS` in `stellantis_worker/main.py`.
As a fallback, the integration's manual OAuth mode always works: open the login link it shows, sign in with the
browser devtools open, and copy the `code=` value from the failed `mymopsdk://` request.
