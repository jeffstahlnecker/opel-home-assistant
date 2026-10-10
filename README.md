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

## Re-login from Home Assistant (add-on 2.3.0+)

When the MyOpel login dies (`invalid_grant`) or remote commands need a new SMS, the add-on can drive the
integration's own Reconfigure flow for you, so a dashboard button replaces the manual dialogs.

1. In the add-on's **Configuration**, fill in `email`, `password` and `pin` (your Opel app PIN). Optionally set
   `notify_service` (e.g. `mobile_app_my_phone`) for push notifications. Changes apply without a restart.
2. Call the endpoints from Home Assistant at `http://127.0.0.1:3789`:

| Endpoint | What it does |
|---|---|
| `POST /relogin` | Renews the account login (≈1–5 min). `?dry_run=1` checks the flow without logging in. |
| `POST /remote-commands/start` | Sends the SMS for remote commands. **Remote commands stay off until the SMS step succeeds.** |
| `POST /remote-commands/sms` `{"code": "123456"}` | Submits the SMS code with the configured PIN (10 min window). |
| `POST /cancel` | Stops the running job. |
| `GET /status` | `{"stage", "message", "job", "last_attempt", "last_success"}` |

Progress is published as `sensor.opel_login_status` (`idle`, `running`, `waiting_for_sms`, `success`, `failed`)
plus a persistent notification (and your notify service, if set).

```yaml
rest_command:
  opel_relogin:
    url: http://127.0.0.1:3789/relogin
    method: post
  opel_remote_commands_start:
    url: http://127.0.0.1:3789/remote-commands/start
    method: post
  opel_remote_commands_sms:
    url: http://127.0.0.1:3789/remote-commands/sms
    method: post
    content_type: application/json
    payload: '{"code": "{{ code }}"}'
```

Notes:
- One job at a time (`409` otherwise). Each of `/relogin` and `/remote-commands/start` can run at most once per
  `min_minutes_between_attempts` (default 15, `429` with `retry_after_s`), so a hammered button can't lock the account.
- If the integration is not loaded (its login died), `/relogin` continues the integration's reauth flow. If remote
  commands are enabled, that flow also asks for the SMS: the status goes to `waiting_for_sms`.
- Credentials, PIN, SMS code and OAuth codes are never logged or returned.
- **LAN access:** host port 3789 is reachable from your network. `POST /` (the login service) stays open as before,
  but the re-login endpoints only accept calls from Home Assistant itself (loopback and the Supervisor network
  `172.30.32.0/23`). Set `allow_lan: true` to call them from other machines. `GET /status` is open and contains no
  secrets.

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
