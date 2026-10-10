"""One-tap re-login for the Stellantis Vehicles integration, driven through Home Assistant's
config-flow API from inside this add-on (see README "Re-login from Home Assistant")."""
import asyncio
import ipaddress
import json
import os
import re
from datetime import datetime, timezone

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

DOMAIN = "stellantis_vehicles"
API = os.environ.get("HA_API_URL", "http://supervisor/core/api")
WS = os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
OPTIONS_PATH = os.environ.get("OPTIONS_PATH", "/data/options.json")
STATE_PATH = os.environ.get("STATE_PATH", "/data/relogin_state.json")
DEFAULT_CODE_URL = "http://127.0.0.1:3789/"
STATUS_ENTITY = "sensor.opel_login_status"
LOGIN_TIMEOUT_S = 330
SMS_TIMEOUT_S = int(os.environ.get("SMS_TIMEOUT_S", 600))
# Callers allowed on the new endpoints: Home Assistant itself (host loopback, reached
# through the Docker port mapping) and the Supervisor network. Option allow_lan lifts it.
LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "172.30.32.0/23")]
REMOTE_COMMANDS_OFF = "Remote commands are now OFF until the SMS step succeeds; run it again."

router = APIRouter()


class FlowError(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_options():
    try:
        with open(OPTIONS_PATH) as f:
            options = json.load(f)
    except Exception:
        options = {}
    options.setdefault("min_minutes_between_attempts", 15)
    return options


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


class Job:
    def __init__(self):
        saved = load_state()
        self.stage = "idle"
        self.message = ""
        self.name = None
        self.task = None
        self.flow_id = None
        # Only flows this add-on started are deleted on failure; the integration's own reauth
        # flow is left in place so its repair prompt survives.
        self.own_flow = False
        self.sms_code = None
        self.sms_received = asyncio.Event()
        self.secrets = []
        self.last_attempt = saved.get("last_attempt")
        self.last_success = saved.get("last_success")
        # Monotonic start time per job kind, for the rate limit.
        self.attempt_started = {}

    @property
    def running(self):
        return self.stage in ("running", "waiting_for_sms")

    def mask(self, text):
        text = str(text)
        for secret in self.secrets:
            if secret:
                text = text.replace(str(secret), "***")
        return text

    def save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"last_attempt": self.last_attempt, "last_success": self.last_success}, f)
        except Exception:
            pass

    def status(self):
        return {"stage": self.stage, "message": self.message, "job": self.name,
                "last_attempt": self.last_attempt, "last_success": self.last_success}


job = Job()


def log(message):
    print(f"[relogin] {job.mask(message)}", flush=True)


class HomeAssistant:
    def __init__(self, session):
        self.session = session
        self.headers = {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}

    async def call(self, method, path, body=None, timeout=30):
        async with self.session.request(method, API + path, json=body, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {"message": text[:200]}
            if resp.status >= 400:
                raise FlowError(f"Home Assistant API {method} {path.split('?')[0]} returned {resp.status}: "
                                f"{data.get('message', data) if isinstance(data, dict) else data}")
            return data

    async def entry(self):
        entries = await self.call("GET", f"/config/config_entries/entry?domain={DOMAIN}")
        if not entries:
            raise FlowError("Stellantis integration not set up")
        return entries[0]

    async def flow_start(self, entry_id):
        return await self.call("POST", "/config/config_entries/flow",
                               {"handler": DOMAIN, "entry_id": entry_id, "show_advanced_options": False})

    async def flow_submit(self, flow_id, data, timeout=60):
        return await self.call("POST", f"/config/config_entries/flow/{flow_id}", data, timeout)

    async def flow_delete(self, flow_id):
        try:
            await self.call("DELETE", f"/config/config_entries/flow/{flow_id}")
        except Exception:
            pass

    async def flows_in_progress(self):
        # The REST listing answers 405, so use the WebSocket API.
        async with self.session.ws_connect(WS, timeout=15) as ws:
            await ws.receive_json(timeout=15)  # auth_required
            await ws.send_json({"type": "auth", "access_token": os.environ.get("SUPERVISOR_TOKEN", "")})
            auth = await ws.receive_json(timeout=15)
            if auth.get("type") != "auth_ok":
                raise FlowError("WebSocket authentication failed")
            await ws.send_json({"id": 1, "type": "config_entries/flow/progress"})
            while True:
                msg = await ws.receive_json(timeout=15)
                if msg.get("id") == 1:
                    return msg.get("result") or []

    async def reauth_flows(self, entry_id=None):
        return [f for f in await self.flows_in_progress()
                if f.get("handler") == DOMAIN and (f.get("context") or {}).get("source") == "reauth"
                and (entry_id is None or (f.get("context") or {}).get("entry_id") in (None, entry_id))]

    async def set_status(self):
        attributes = dict(job.status(), friendly_name="Opel login", icon="mdi:login")
        attributes.pop("stage")
        try:
            await self.call("POST", f"/states/{STATUS_ENTITY}", {"state": job.stage, "attributes": attributes})
        except Exception as e:
            log(f"Could not update {STATUS_ENTITY}: {e}")

    async def notify(self, title, message):
        try:
            await self.call("POST", "/services/persistent_notification/create",
                            {"notification_id": "opel_login", "title": title, "message": message})
        except Exception as e:
            log(f"Persistent notification failed: {e}")
        service = (read_options().get("notify_service") or "").strip()
        if service.startswith("notify."):
            service = service[len("notify."):]
        if service:
            try:
                await self.call("POST", f"/services/notify/{service}", {
                    "title": title, "message": message, "data": {"tag": "opel_login", "group": "system_health"}})
            except Exception as e:
                log(f"notify.{service} failed: {e}")


def describe(result):
    parts = [f"type={result.get('type')}"]
    for key in ("step_id", "reason"):
        if result.get(key):
            parts.append(f"{key}={result[key]}")
    if result.get("errors"):
        parts.append(f"errors={result['errors']}")
    return ", ".join(parts)


def expect(result, type_, step_id=None, reason=None):
    if result.get("type") != type_ or (step_id and result.get("step_id") != step_id) \
            or (reason and result.get("reason") != reason):
        if result.get("type") == "abort":
            raise FlowError(f"Integration stopped: {result.get('reason')}")
        raise FlowError(f"Unexpected flow result ({describe(result)})")
    return result


def schema_default(result, name):
    for field in result.get("data_schema") or []:
        if field.get("name") == name and "default" in field:
            return field["default"]
    return None


def schema_defaults(result):
    return {f["name"]: f["default"] for f in result.get("data_schema") or [] if "default" in f}


async def set_stage(ha, stage, message):
    job.stage = stage
    job.message = job.mask(message)
    log(f"{stage}: {message}")
    await ha.set_status()


async def finish_after_otp(ha, result):
    # After the SMS step a reauth flow still shows the options form; reconfigure ends directly.
    if result.get("type") == "form" and result.get("step_id") == "options":
        result = await ha.flow_submit(job.flow_id, schema_defaults(result))
    if result.get("type") == "abort" and result.get("reason") in ("reconfigure_successful", "reauth_successful"):
        return
    raise FlowError(f"Integration stopped: {result.get('reason')}" if result.get("type") == "abort"
                    else f"Unexpected flow result ({describe(result)})")


def sms_window():
    return f"{SMS_TIMEOUT_S // 60} min" if SMS_TIMEOUT_S >= 60 else f"{SMS_TIMEOUT_S} s"


async def wait_for_sms(ha, intro):
    job.sms_code = None
    job.sms_received.clear()
    await set_stage(ha, "waiting_for_sms", f"{intro} Enter the SMS code within {sms_window()}.")
    await ha.notify("📱 Opel SMS sent — enter the code", job.message)
    try:
        await asyncio.wait_for(job.sms_received.wait(), timeout=SMS_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise FlowError(f"No SMS code received within {sms_window()}. {REMOTE_COMMANDS_OFF}")
    await set_stage(ha, "running", "Submitting SMS code and PIN...")
    job.secrets.append(job.sms_code)
    result = await ha.flow_submit(job.flow_id, {"sms_code": job.sms_code, "pin_code": read_options().get("pin", "")},
                                  timeout=120)
    try:
        await finish_after_otp(ha, result)
    except FlowError as e:
        raise FlowError(f"{e}. {REMOTE_COMMANDS_OFF}")


async def oauth_steps(ha, result, options):
    """From the oauth_mode form to the end of the account login."""
    expect(result, "form", "oauth_mode")
    result = expect(await ha.flow_submit(job.flow_id, {"oauth_manual_mode": False}), "form", "oauth_remote")
    code_url = schema_default(result, "oauth_code_url") or DEFAULT_CODE_URL
    await set_stage(ha, "running", "Logging in to MyOpel (this can take a few minutes)...")
    result = await ha.flow_submit(job.flow_id, {"email": options["email"], "password": options["password"],
                                                "oauth_code_url": code_url}, timeout=LOGIN_TIMEOUT_S)
    expect(result, "form", "get_access_token")
    remote_commands = schema_default(result, "remote_commands")
    if not isinstance(remote_commands, bool):
        raise FlowError("Could not read the current remote_commands setting from the flow")
    return await ha.flow_submit(job.flow_id, {"remote_commands": remote_commands})


async def run_relogin(ha, dry_run):
    options = read_options()
    entry = await ha.entry()
    if entry.get("state") == "loaded":
        job.flow_id = (expect(await ha.flow_start(entry["entry_id"]), "form", "reconfigure"))["flow_id"]
        job.own_flow = True
        result = await ha.flow_submit(job.flow_id, {"reconfigure": "oauth"})
        if dry_run:
            expect(result, "form", "oauth_mode")
            expect(await ha.flow_submit(job.flow_id, {"oauth_manual_mode": False}), "form", "oauth_remote")
            await ha.flow_delete(job.flow_id)
            job.flow_id = None
            return "Dry run ok"
        result = await oauth_steps(ha, result, options)
        expect(result, "abort", reason="reconfigure_successful")
        return "Opel login renewed"

    # Not loaded (e.g. the refresh token died): Reconfigure would abort with "not_loaded", so
    # continue the integration's own reauth flow, starting one by reloading the entry if needed.
    flows = await ha.reauth_flows(entry["entry_id"])
    if not flows and not dry_run:
        await ha.call("POST", f"/config/config_entries/entry/{entry['entry_id']}/reload", timeout=120)
        await asyncio.sleep(5)
        flows = await ha.reauth_flows(entry["entry_id"])
    if not flows:
        raise FlowError(f"Integration is not loaded (state: {entry.get('state')}) and has no reauth flow")
    if dry_run:
        return f"Dry run ok (integration not loaded, reauth flow at step {flows[0].get('step_id')})"
    job.flow_id = flows[0]["flow_id"]
    job.own_flow = False
    result = flows[0]
    if result.get("step_id") == "reauth_confirm":
        result = await ha.flow_submit(job.flow_id, {})
    elif result.get("step_id") != "oauth_mode":
        raise FlowError(f"Reauth flow is at an unexpected step: {result.get('step_id')}")
    result = await oauth_steps(ha, result, options)
    if result.get("type") == "form" and result.get("step_id") == "otp":
        await wait_for_sms(ha, "Account login renewed; the integration also needs the remote-commands SMS.")
    else:
        await finish_after_otp(ha, result)
    return "Opel login renewed"


async def run_remote_commands(ha):
    entry = await ha.entry()
    if entry.get("state") != "loaded":
        raise FlowError(f"Integration is not loaded (state: {entry.get('state')}); run the account re-login first")
    job.flow_id = (expect(await ha.flow_start(entry["entry_id"]), "form", "reconfigure"))["flow_id"]
    job.own_flow = True
    # From here on the integration has switched remote commands off until the SMS step succeeds.
    result = await ha.flow_submit(job.flow_id, {"reconfigure": "remote_commands"})
    try:
        expect(result, "form", "otp")
    except FlowError as e:
        raise FlowError(f"{e}. {REMOTE_COMMANDS_OFF}")
    await wait_for_sms(ha, "SMS sent. Remote commands are OFF until you enter the code.")
    return "Remote commands renewed"


async def cleanup_reauth(ha):
    try:
        for flow in await ha.reauth_flows():
            await ha.flow_delete(flow["flow_id"])
    except Exception as e:
        log(f"Reauth cleanup skipped: {e}")


async def run_job(name, dry_run=False):
    async with aiohttp.ClientSession() as session:
        ha = HomeAssistant(session)
        await ha.set_status()
        try:
            if name == "relogin":
                message = await run_relogin(ha, dry_run)
            else:
                message = await run_remote_commands(ha)
            job.flow_id = None
            if not dry_run:
                job.last_success = now_iso()
                job.save()
                await cleanup_reauth(ha)
            await set_stage(ha, "success", message)
            if not dry_run:
                await ha.notify("✅ Opel login renewed", job.message)
        except asyncio.CancelledError:
            await abort_flow(ha)
            await set_stage(ha, "failed", "Cancelled." + (f" {REMOTE_COMMANDS_OFF}" if name == "remote_commands" else ""))
            raise
        except Exception as e:
            await abort_flow(ha)
            await set_stage(ha, "failed", str(e) or e.__class__.__name__)
            await ha.notify("⚠️ Opel login failed", job.message)
        finally:
            job.secrets = []


async def abort_flow(ha):
    if job.flow_id and job.own_flow:
        await ha.flow_delete(job.flow_id)
    job.flow_id = None


async def publish_initial_status():
    # So sensor.opel_login_status exists (as "idle") for dashboards before the first job.
    if os.environ.get("SUPERVISOR_TOKEN"):
        async with aiohttp.ClientSession() as session:
            await HomeAssistant(session).set_status()


@router.on_event("startup")
async def startup():
    asyncio.create_task(publish_initial_status())


def reply(status, body):
    return JSONResponse(status_code=status, content=body)


def guard(request: Request, options):
    if not options.get("allow_lan"):
        try:
            if not any(ipaddress.ip_address(request.client.host) in n for n in LOCAL_NETWORKS):
                return reply(403, {"message": "Only Home Assistant may call this endpoint (option allow_lan)"})
        except Exception:
            return reply(403, {"message": "Unknown caller"})
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return reply(400, {"message": "Not running as a Home Assistant add-on (no SUPERVISOR_TOKEN)"})
    return None


async def start_job(request, name, required, dry_run=False):
    options = read_options()
    denied = guard(request, options)
    if denied:
        return denied
    if job.running:
        return reply(409, {"message": f"A {job.name} job is already {job.stage}"})
    missing = [key for key in required if not str(options.get(key) or "").strip()]
    if missing and not dry_run:
        return reply(400, {"message": f"Missing add-on options: {', '.join(missing)}"})
    if not dry_run:
        wait = int(options.get("min_minutes_between_attempts") or 15) * 60
        elapsed = asyncio.get_running_loop().time() - job.attempt_started.get(name, -1e9)
        if elapsed < wait:
            return reply(429, {"message": "Rate limited", "retry_after_s": int(wait - elapsed)})
        job.attempt_started[name] = asyncio.get_running_loop().time()
        job.last_attempt = now_iso()
        job.save()

    job.name = name
    job.secrets = [options.get("email"), options.get("password"), options.get("pin")]
    job.stage = "running"
    job.message = "Dry run started" if dry_run else "Started"
    job.task = asyncio.create_task(run_job(name, dry_run))
    return reply(202, {"status": "started"})


@router.post("/relogin")
async def relogin(request: Request, dry_run: int = 0):
    return await start_job(request, "relogin", ["email", "password"], dry_run=bool(dry_run))


@router.post("/remote-commands/start")
async def remote_commands_start(request: Request):
    return await start_job(request, "remote_commands", ["pin"])


@router.post("/remote-commands/sms")
async def remote_commands_sms(request: Request):
    denied = guard(request, read_options())
    if denied:
        return denied
    if job.stage != "waiting_for_sms":
        return reply(409, {"message": "Not waiting for an SMS code"})
    try:
        code = re.sub(r"\s+", "", str((await request.json()).get("code", "")))
    except Exception:
        code = ""
    if not re.fullmatch(r"\d{4,8}", code):
        return reply(400, {"message": "The SMS code must be 4-8 digits"})
    job.sms_code = code
    job.sms_received.set()
    return reply(202, {"status": "submitted"})


@router.post("/cancel")
async def cancel(request: Request):
    denied = guard(request, read_options())
    if denied:
        return denied
    if job.task and not job.task.done():
        job.task.cancel()
        try:
            await job.task
        except (asyncio.CancelledError, Exception):
            pass
    return reply(200, job.status())


@router.get("/status")
async def status():
    return job.status()
