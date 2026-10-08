import re
import time
import asyncio
import uuid
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

app = FastAPI()

browser_process_id = None
browser_start = None

ok_count = 0
ko_count = 0

playwright = None
browser = None
browser_lock = asyncio.Lock()

# Opel/Stellantis moved from a Gigya login form to the ForgeRock AM "XUI" login page
# (/am/XUI/#login/). Both are listed so either flow keeps working.
SELECTORS = {
    "cookies": "#onetrust-accept-btn-handler, #didomi-notice-agree-button, button#accept-all-cookies",
    "email": "#idToken1, "
             '#gigya-login-form input[name="username"], #gigya-login-form input[type="email"]',
    "password": "#idToken2, "
                '#gigya-login-form input[name="password"], #gigya-login-form input[type="password"]',
    "submit": "#loginButton_0, "
              '#gigya-login-form input[type="submit"], #gigya-login-form button[type="submit"]',
    "login_error": "#loginFailure, .alert-danger, "
                   "#gigya-login-form .gigya-error-msg-active, #gigya-login-form .gigya-form-error-msg.gigya-error-msg-active",
    "authorize": '[name="decision"][value="allow"], '
                 '#cvs_from input[type="submit"], #cvs_form input[type="submit"], #cvs_from button[type="submit"], #cvs_form button[type="submit"]',
}

# Consent pages after login (e.g. id-dcr.opel.com/index/authorize-consentments) are matched by
# button text, in the languages of the MyOpel/MyPeugeot/... markets.
ACCEPT_TEXT = re.compile(
    r"akzeptier|zustimm|einverstanden|erlaub|autorisier|bestätig|weiter|fortfahren|"
    r"accept|agree|allow|authori[sz]|confirm|continue|"
    r"accepter|j'accepte|autoriser|valider|continuer|"
    r"accett|autorizz|conferm|continua|acept|confirmar|"
    r"akkoord|toestaan|doorgaan|bevestig",
    re.IGNORECASE,
)
DECLINE_TEXT = re.compile(
    r"ablehn|nicht|später|abbrech|ohne|decline|deny|reject|refus|cancel|later|without|annul|sans|rifiut|senza|rechaz|sin |weiger",
    re.IGNORECASE,
)
CLICKABLE = 'button, input[type="submit"], input[type="button"], [role="button"], a.btn, a.button'

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--disable-notifications",
    "--disable-default-apps",
    "--disable-blink-features=AutomationControlled",
    "--mute-audio",
    "--no-first-run",
    "--no-zygote",
]


def log_process(message, process_id, debug=True):
    if debug:
        print(f"[{process_id}] {message}", flush=True)


async def start_browser():
    global playwright, browser, browser_process_id, browser_start
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True, args=BROWSER_ARGS)
    browser_process_id = uuid.uuid4().hex[:8]
    browser_start = time.perf_counter()
    log_process(f"Browser start (Chromium {browser.version})", browser_process_id)


async def stop_browser():
    global playwright, browser
    try:
        if browser:
            await browser.close()
    except Exception:
        pass
    try:
        if playwright:
            await playwright.stop()
    except Exception:
        pass
    browser = None
    playwright = None
    if browser_start:
        log_process(f"Browser end: {time.perf_counter() - browser_start:.2f}s", browser_process_id)


async def ensure_browser():
    if browser is None or not browser.is_connected():
        await stop_browser()
        await start_browser()


@app.on_event("startup")
async def startup():
    async with browser_lock:
        await start_browser()


@app.on_event("shutdown")
async def shutdown():
    async with browser_lock:
        await stop_browser()


def http_response(message, process_id, process_start, status=400):
    global ok_count, ko_count

    if status == 200:
        ok_count += 1
        body = {"code": message}
    else:
        ko_count += 1
        body = {"message": f"{message} [{process_id}]", "code": status}
        log_process(f"Response: {message}", process_id)

    log_process(f"Process end: {time.perf_counter() - process_start:.2f}s (OK: {ok_count}, KO: {ko_count})", process_id)

    return JSONResponse(
        status_code=status,
        content=body,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    )


def extract_code(url):
    # The final redirect goes to the mobile app scheme (mymopsdk://, mymap://, ...),
    # which the browser cannot open; the authorization code is in its query string.
    if not url or not urlparse(url).scheme.startswith("mym"):
        return None
    return parse_qs(urlparse(url).query).get("code", [None])[0]


def page_location(page):
    # Current page without query string, to help diagnose where the flow stopped.
    try:
        parsed = urlparse(page.url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return "unknown"


async def click_if_visible(page, selector):
    locator = page.locator(selector).first
    try:
        if await locator.is_visible():
            await locator.click(timeout=5000)
            return True
    except Exception:
        pass
    return False


async def accept_consent(page):
    # Known consent forms first, then any "accept"-like button that is not a "decline" one.
    if await click_if_visible(page, SELECTORS["authorize"]):
        return "authorize form"

    candidates = page.locator(CLICKABLE)
    for i in range(min(await candidates.count(), 30)):
        button = candidates.nth(i)
        try:
            if not await button.is_visible():
                continue
            text = ((await button.inner_text()) or (await button.get_attribute("value")) or "").strip()
            if not ACCEPT_TEXT.search(text) or DECLINE_TEXT.search(text):
                continue
            # Tick only the checkboxes the page requires before it can be submitted.
            required = page.locator('input[type="checkbox"][required]:not(:checked)')
            for j in range(await required.count()):
                await required.nth(j).check(force=True, timeout=5000)
            await button.click(timeout=5000)
            return text[:40]
        except Exception:
            continue
    return None


async def describe_page(page):
    # Visible headings, buttons and checkboxes (never field values), to show what a stalled page asks for.
    try:
        return await page.evaluate("""() => {
            const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
            const text = s => (s || "").replace(/\\s+/g, " ").trim().slice(0, 60);
            const out = [];
            document.querySelectorAll("h1, h2, h3").forEach(e => visible(e) && out.push("heading: " + text(e.innerText)));
            document.querySelectorAll('%s').forEach(e => visible(e) &&
                out.push("button: " + text(e.innerText || e.value) + (e.id ? " #" + e.id : "")));
            document.querySelectorAll('input[type="checkbox"]').forEach(e => {
                const label = e.labels && e.labels[0] ? e.labels[0].innerText : "";
                out.push("checkbox" + (e.required ? " (required)" : "") + (e.checked ? " (checked)" : "") +
                    ": " + (e.name || e.id) + " " + text(label));
            });
            return out.slice(0, 25).join(" | ");
        }""" % CLICKABLE)
    except Exception:
        return ""


@app.post("/")
async def fetch(request: Request):
    process_id = uuid.uuid4().hex[:8]
    process_start = time.perf_counter()
    log_process("Process start", process_id)

    try:
        payload = await request.json()
    except Exception:
        return http_response("Invalid JSON body", process_id, process_start)

    url = payload.get("url")
    email = payload.get("email")
    password = payload.get("password")
    timeout_page = payload.get("timeout_page", 60000)
    timeout_input = payload.get("timeout_input", 30000)
    debug = bool(payload.get("debug", False))

    if not url or not email or not password:
        return http_response("Missing required params", process_id, process_start)

    code_captured = asyncio.Event()
    captured = {"code": None}

    def capture(candidate_url):
        code = extract_code(candidate_url)
        if code and not captured["code"]:
            captured["code"] = code
            code_captured.set()
            log_process("Code captured!", process_id, debug)

    def on_response(response):
        if 300 <= response.status < 400:
            capture(response.headers.get("location"))

    async with browser_lock:
        context = None
        try:
            await ensure_browser()

            context = await browser.new_context(
                user_agent=f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36",
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
                bypass_csp=True,
                ignore_https_errors=True,
            )
            context.set_default_timeout(timeout_input)
            context.set_default_navigation_timeout(timeout_page)

            page = await context.new_page()
            page.on("request", lambda req: capture(req.url))
            page.on("requestfailed", lambda req: capture(req.url))
            page.on("response", on_response)
            page.on("framenavigated", lambda frame: capture(frame.url))

            log_process(f"Navigating to login: {urlparse(url).netloc}", process_id, debug)
            await page.goto(url, wait_until="domcontentloaded")

            log_process("Waiting for login form...", process_id, debug)
            email_input = page.locator(SELECTORS["email"]).first
            password_input = page.locator(SELECTORS["password"]).first
            await email_input.wait_for(state="visible")
            await click_if_visible(page, SELECTORS["cookies"])

            log_process("Filling credentials...", process_id, debug)
            await email_input.click()
            await email_input.fill("")
            await email_input.press_sequentially(email, delay=30)
            await password_input.click()
            await password_input.fill("")
            await password_input.press_sequentially(password, delay=30)

            log_process("Submitting login form...", process_id, debug)
            await click_if_visible(page, SELECTORS["cookies"])
            await page.locator(SELECTORS["submit"]).first.click()

            # After login the flow shows zero or more consent pages, then redirects to the app scheme
            # with the code. Accounts that already consented go straight to the redirect.
            log_process("Waiting for consent form or code...", process_id, debug)
            deadline = time.perf_counter() + timeout_page / 1000
            consents = []
            last_click = ("", 0.0)
            while not code_captured.is_set() and time.perf_counter() < deadline:
                error = page.locator(SELECTORS["login_error"]).first
                try:
                    if await error.is_visible():
                        message = (await error.inner_text()).strip() or "Login rejected"
                        return http_response(f"Login error: {message}", process_id, process_start)
                except Exception:
                    pass

                # Click again only on a new page, or if the same page has not moved on for a while.
                now = time.perf_counter()
                if len(consents) < 5 and (page.url != last_click[0] or now - last_click[1] > 5):
                    clicked = await accept_consent(page)
                    if clicked:
                        consents.append(clicked)
                        last_click = (page.url, now)
                        log_process(f"Consent submitted ({clicked}) at {page_location(page)}", process_id, debug)

                try:
                    await asyncio.wait_for(code_captured.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

            if captured["code"]:
                return http_response(captured["code"], process_id, process_start, 200)

            step = f"after consent ({', '.join(consents)})" if consents else "after login"
            details = await describe_page(page)
            return http_response(
                f"Code not found {step} (stopped at {page_location(page)})" + (f" Page: {details}" if details else ""),
                process_id, process_start)

        except Exception as e:
            log_process(f"Error: {e}", process_id)
            if captured["code"]:
                return http_response(captured["code"], process_id, process_start, 200)
            return http_response(str(e).splitlines()[0], process_id, process_start)

        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass


@app.get("/")
@app.get("/health")
async def healthcheck():
    process_id = uuid.uuid4().hex[:8]

    async with browser_lock:
        try:
            await ensure_browser()
            context = await asyncio.wait_for(browser.new_context(), timeout=10)
            page = await context.new_page()
            await page.goto("about:blank", timeout=10000)
            await context.close()
        except Exception as e:
            log_process(f"Restarting browser: {e}", process_id)
            await stop_browser()
            await start_browser()

    return {"status": "ok"}
