# Changelog

## 2.3.0
- One-tap re-login from Home Assistant: `POST /relogin` renews the MyOpel account login through the
  integration's Reconfigure flow (or its own reauth flow when the integration failed to load).
- Remote commands renewal: `POST /remote-commands/start` sends the SMS, `POST /remote-commands/sms` submits it
  with the PIN from the add-on options.
- `GET /status`, `POST /cancel`, `sensor.opel_login_status`, persistent and mobile notifications.
- New options: email, password, PIN, notify service, minimum minutes between attempts, allow LAN access.
- `POST /` and `GET /health` are unchanged.

## 2.2.1
- Return the OAuth code exactly as received (fixes `invalid_grant` on codes with encoded characters).

## 2.2.0
- Handle the `id-dcr.opel.com` consent page after login.

## 2.1.0
- Support Opel's ForgeRock login page.

## 2.0.0
- Fix cookie banner, code capture and optional consent; package as a Home Assistant add-on.
