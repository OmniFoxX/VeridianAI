import asyncio
import json
import os
import random
import re
import sys
import time
from urllib.parse import urlencode, urlparse, quote_plus
from pathlib import Path
from typing import Any, Dict, List, Optional

# v2.1.6 unified time source — single audit point for all timestamps
sys.path.insert(0, str(Path(__file__).resolve().parent))
from time_manager import TimeManager

import aiohttp
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)
from playwright_stealth import Stealth  # pip install playwright-stealth


def _resolve_profile_dir(ns: Optional[str] = None) -> Path:
    """Per-user browser profile dir, kept OUT of the project tree (sage_data).
    owner/None -> <DATA_DIR>/browser_profile ; user -> <DATA_DIR>/users/<ns>/browser_profile.
    Falls back to the COMPUTED sibling sage_data (never a hardcoded drive/version)
    only if config is unavailable, e.g. a standalone run."""
    try:
        from config import DATA_DIR as _DD
        base = Path(_DD)
    except Exception:
        # backend/ -> project root -> sibling sage_data (matches the real layout)
        base = Path(__file__).resolve().parent.parent.parent / "sage_data"
    return (base / "users" / ns / "browser_profile") if ns else (base / "browser_profile")


def _detect_brave_path() -> Optional[str]:
    """Locate a Brave Browser binary across OS-standard install locations.

    Brave is NOT one of Playwright's official `channel` values (only chrome/
    msedge and their beta/dev/canary variants are), so it can only be launched
    by pointing `executable_path` at the Brave binary. Returns the first path
    that exists, or None if Brave isn't installed (many users won't have it).
    """
    candidates: List[str] = []
    if sys.platform.startswith("win"):
        pf = os.getenv("PROGRAMFILES", r"C:\Program Files")
        pfx86 = os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.getenv("LOCALAPPDATA", "")
        sub = r"BraveSoftware\Brave-Browser\Application\brave.exe"
        candidates += [os.path.join(pf, sub), os.path.join(pfx86, sub)]
        if local:
            candidates.append(os.path.join(local, sub))
    elif sys.platform == "darwin":
        candidates.append(
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        )
    else:  # linux / other unix
        candidates += [
            "/usr/bin/brave-browser", "/usr/bin/brave",
            "/opt/brave.com/brave/brave-browser", "/snap/bin/brave",
        ]
    for p in candidates:
        try:
            if p and Path(p).exists():
                return p
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------- #
# SECURITY (CRITICAL #1): browser-content sanitization
# ---------------------------------------------------------------------- #
# VeridianAI's tool-call grammar is square-bracket based: sage_engine.py
# parses [BROWSE:...], [REMEMBER:...], [SAVE_FILE:...], [GENERATE_IMAGE],
# and ~20 other [WORD:...] / [WORD] tags out of model-visible text, plus an
# ORPHAN/PARTIAL detector that reacts to an UNCLOSED "[WORD" with no closing
# bracket. Any of those shapes appearing verbatim in fetched page content
# would be injected straight into Toga's context and executed as a live tool
# call — a textbook prompt-injection / indirect-tool-call vector.
#
# Defense: before ANY page-derived text crosses the return boundary back to
# the engine, neutralize tag-like bracket tokens by swapping the ASCII
# brackets for Unicode mathematical white square brackets:
#     [  ->  U+27E6  ⟦        ]  ->  U+27E7  ⟧
# The text stays fully human-readable (important — our users are blind/low
# vision and this is read aloud to them) but is completely INERT to the
# parser, which only ever matches ASCII "[" / "]".
#
# We deliberately do NOT touch purely numeric/symbolic brackets like "[1]"
# citation markers or "[...]" ellipses: those never match a tool tag
# (tags require a leading letter/underscore) so they read more naturally
# left alone. We DO neutralize:
#   * closed tag-like tokens   [WORD]  /  [WORD: anything]  /  [edit]
#   * bare openings            [WORD   (defuses the orphan/partial detector,
#                              which keys on an unclosed "[WORD")
#
# One private helper so this logic lives in exactly one place and every
# return path is provably covered.

# A tag-like token: "[", optional space, a leading letter/underscore, then any
# run of non-bracket chars, closed by "]". The [^\[\]] body prevents runaway
# backtracking and stops the match at the first bracket of either kind.
_TAGISH_TOKEN_RE = re.compile(r"\[\s*[A-Za-z_][^\[\]]*\]")
# A bare (unclosed) tag opening: "[" immediately followed by a word start.
# This is what defuses sage_engine's orphan-tag / partial-tag detectors,
# which act on "[WORD" even when the attacker never supplies a closing "]".
_TAGISH_OPEN_RE = re.compile(r"\[(?=\s*[A-Za-z_])")


def _sanitize_browser_content(text: Optional[str]) -> str:
    """Neutralize square-bracket tool-call patterns in any content returned
    from the live web so a malicious page cannot inject a tool call into
    Toga's context window (CRITICAL #1).

    Returns readable text with tag-like "[" / "]" swapped for the inert
    lookalikes "⟦" / "⟧". Safe on non-str / None input.
    """
    if not text:
        return "" if text is None else text
    if not isinstance(text, str):
        # Defensive: callers should pass str, but never let a non-str slip
        # past the boundary unsanitized (it could still stringify to a tag).
        text = str(text)

    # Pass 1: fully-closed tag-like tokens -> swap BOTH brackets. This covers
    # [BROWSE:...], [REMEMBER:slug|payload], [SAVE_FILE:name|body], [edit], etc.
    def _swap(m: "re.Match") -> str:
        return "⟦" + m.group(0)[1:-1] + "⟧"
    text = _TAGISH_TOKEN_RE.sub(_swap, text)

    # Pass 2: any remaining bare "[WORD" opening (no matching "]") -> neutralize
    # the opening bracket so the orphan/partial-tag detector can't latch onto it
    # and pair it with a legitimate "]" later in the page.
    text = _TAGISH_OPEN_RE.sub("⟦", text)
    return text


class BrowserTool:
    """
    A persistent, stealth-enabled Playwright wrapper that behaves like a
    human user and can be reused across calls.
    """

    def __init__(
        self,
        profile_dir: str = None,
        headless: bool = False,
        timeout: int = 10_000,
        user_agents: Optional[List[str]] = None,
        proxy: Optional[Dict[str, str]] = None,
        ns: Optional[str] = None,
        persist_cookies: bool = False,
        channel: Optional[str] = None,
        ignore_https_errors: bool = False,
    ):
        # Per-user profile, OUT of the project tree (sage_data). Each user's
        # Sage gets her own browser profile, so bookmarks/history/cookies never
        # bleed across accounts. profile_dir overrides; else resolved from ns.
        self.ns = ns
        self.profile_dir = Path(profile_dir) if profile_dir else _resolve_profile_dir(ns)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.timeout = timeout
        self.proxy = proxy
        # Cookies are OPT-IN (off by default): bookmarks/history persist, but
        # cookies are cleared each session unless the user enables them.
        self.persist_cookies = persist_cookies
        # Prefer a real Chrome install (proper identity, no "Chromium"/test
        # build); start() falls back to bundled Chromium if it isn't present.
        # channel may also be "brave" (resolved to an executable_path in start).
        self.channel = channel
        self.executable_path: Optional[str] = None  # set if launched via a binary
        # SECURITY (SIGNIFICANT #3): TLS certificate validation is now ON by
        # default. Silently accepting bad certs made a MITM'd page invisible to
        # the tool — doubly dangerous given the content boundary above. This is
        # now an explicit, per-instance opt-in and is logged loudly when set.
        self.ignore_https_errors = ignore_https_errors
        if self.ignore_https_errors:
            print(
                "[SECURITY][WARN] BrowserTool started with "
                "ignore_https_errors=True — TLS certificate validation is "
                "DISABLED for this profile. A man-in-the-middle can serve "
                "forged pages undetected. Use only for a known-bad-cert host "
                "you explicitly trust."
            )
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # Default UA pool
        self.user_agents = user_agents or [
            # Chrome on Windows
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            # Chrome on macOS
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            # Firefox on Linux
            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) "
            "Gecko/20100101 Firefox/124.0",
        ]

        self._last_action_ts = 0.0
        self._init_rate_tracker()

    def _init_rate_tracker(self) -> None:
        """Initialize domain hit counter."""
        self.domain_hits: Dict[str, List[float]] = {}
        self.rate_limit_window: int = 60   # seconds
        self.rate_limit_max: int = 10      # max hits per domain per window

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Launch a REAL, persistent browser profile (bookmarks/history survive
        across sessions, stored in user_data_dir). Tries a real Chrome install
        first for a proper taskbar identity, then falls back to bundled Chromium."""
        self.playwright = await async_playwright().start()

        # ---- Sandbox decision (CRITICAL #2 — ROOT CAUSE) ------------------
        # Playwright's `chromium_sandbox` launch option defaults to False/None,
        # so launch_persistent_context() appends `--no-sandbox` ITSELF on every
        # attempt — bundled Chromium, Chrome, Edge, and Brave alike. That is why
        # the banner appeared "regardless of channel": it is a Playwright-level
        # default (confirmed on Playwright 1.58 / Win11 / py3.14), NOT a Windows
        # or per-browser quirk. Forcing chromium_sandbox=True stops Playwright
        # adding the flag and keeps Chromium's OS process sandbox. We NEVER put
        # "--no-sandbox" in args ourselves. The env var is the ONLY way to turn
        # the sandbox off, for genuinely locked-down hosts, and it warns loudly.
        allow_no_sandbox = os.getenv("VERIDIAN_ALLOW_NO_SANDBOX") == "1"
        chromium_sandbox = not allow_no_sandbox  # True in normal operation
        if allow_no_sandbox:
            print(
                "[SECURITY][WARN] VERIDIAN_ALLOW_NO_SANDBOX=1 — launching with "
                "chromium_sandbox=False; Playwright will add --no-sandbox and "
                "Chromium process isolation is DISABLED. Use only on hosts that "
                "genuinely cannot sandbox (and never run the app elevated)."
            )

        base_kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            proxy=self.proxy,
            user_agent=random.choice(self.user_agents),
            viewport={"width": 1366, "height": 768},
            # SECURITY (SIGNIFICANT #3): validate TLS by default; only bypass
            # when the operator explicitly opted in on this instance.
            ignore_https_errors=self.ignore_https_errors,
            # Drop the "Chrome is being controlled by automated test software"
            # banner + the automation fingerprint so it presents as a normal
            # browser (no "test chrome" identity).
            ignore_default_args=["--enable-automation"],
            # THE FIX (CRITICAL #2): keep the process sandbox. Without this,
            # Playwright silently injects --no-sandbox on Win11 for ANY channel.
            chromium_sandbox=chromium_sandbox,
            args=[
                "--disable-infobars",
                "--start-maximized",
                "--no-default-browser-check",
                "--no-first-run",
            ],
        )

        # Build the ordered list of launch attempts. Each entry is
        # (label, overrides) where overrides carry EITHER channel= OR
        # executable_path= (never both). Brave is not an official Playwright
        # channel, so it is launched via executable_path to its binary.
        attempts: List = []
        if self.channel:
            if self.channel.lower() == "brave":
                brave = _detect_brave_path()
                if not brave:
                    raise RuntimeError(
                        "channel='brave' requested but no Brave binary was "
                        "found in the standard install locations."
                    )
                attempts.append(("brave", {"executable_path": brave}))
            else:
                attempts.append((self.channel, {"channel": self.channel}))
        else:
            # Default preference order. Brave FIRST when present (the preferred
            # browser), then real Chrome, then Edge, then bundled Chromium so a
            # fresh machine with none of them installed still works. Every step
            # keeps the sandbox on — the fallback is about identity, not safety.
            brave = _detect_brave_path()
            if brave:
                attempts.append(("brave", {"executable_path": brave}))
            attempts.append(("chrome", {"channel": "chrome"}))
            attempts.append(("msedge", {"channel": "msedge"}))
            attempts.append(("bundled-chromium", {}))  # channel=None

        # CRITICAL #2: log every attempt + the sandbox state so an operator can
        # confirm from the console exactly what served the session.
        last_err = None
        self.context = None
        for label, overrides in attempts:
            print(
                f"[BROWSER] Attempting launch: {label} "
                f"(chromium_sandbox={chromium_sandbox})"
            )
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    **{**base_kwargs, **overrides}
                )
                # Record what actually served the session (channel name, or the
                # binary path for Brave / other executable_path launches).
                self.channel = overrides.get("channel", label)
                self.executable_path = overrides.get("executable_path")
                print(
                    f"[BROWSER] Launch SUCCEEDED: {label} — process sandbox "
                    f"{'ON' if chromium_sandbox else 'OFF (--no-sandbox)'}"
                )
                break
            except Exception as e:
                last_err = e
                print(f"[BROWSER] Launch failed on {label}: {e}")
                continue
        if self.context is None:
            raise RuntimeError(f"browser launch failed (no usable channel): {last_err}")

        # Persistent contexts have no separate Browser object; closing the
        # context closes the browser. Keep the handle if Playwright exposes one.
        self.browser = self.context.browser

        # Cookies are opt-in: when disabled, start each session with none so no
        # personal/session data accrues (bookmarks/history still persist).
        if not self.persist_cookies:
            try:
                await self.context.clear_cookies()
            except Exception:
                pass

        await Stealth().apply_stealth_async(self.context)

        # Reuse the page the persistent context opens with instead of spawning
        # a second blank tab.
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(self.timeout)

    async def close(self) -> None:
        """Shut down cleanly. The persistent profile (bookmarks/history) is
        saved by Chrome itself in user_data_dir — no storage_state file. If
        cookies are opt-OUT, clear them first so nothing personal persists."""
        if self.context:
            if not self.persist_cookies:
                try:
                    await self.context.clear_cookies()
                except Exception:
                    pass
            try:
                await self.context.close()
            except Exception:
                pass
        # For a persistent context, closing the context closes the browser;
        # guard the legacy browser handle just in case it's a real object.
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            await self.playwright.stop()
        self.page = self.context = self.browser = self.playwright = None

    # ------------------------------------------------------------------ #
    # Human-like timing helpers
    # ------------------------------------------------------------------ #
    async def _human_delay(self, min_ms: int = 120, max_ms: int = 350) -> None:
        """Pause for a random interval mimicking human reaction time."""
        delay = random.uniform(min_ms / 1000, max_ms / 1000)
        await asyncio.sleep(delay)
        self._last_action_ts = time.time()

    async def _random_mouse_move(self) -> None:
        """Move the mouse to a random point inside the viewport."""
        if not self.page:
            return
        width = self.page.viewport_size["width"]
        height = self.page.viewport_size["height"]
        x = random.randint(0, width)
        y = random.randint(0, height)
        await self.page.mouse.move(x, y)
        await self._human_delay(50, 150)

    # ------------------------------------------------------------------ #
    # Rate limiting
    # ------------------------------------------------------------------ #
    async def _check_rate_limit(self, domain: str) -> bool:
        """
        Returns True if safe to proceed, False if rate limit reached.
        Call before every goto() or search() targeting external domains.
        """
        now = time.time()
        hits = self.domain_hits.get(domain, [])
        hits = [t for t in hits if now - t < self.rate_limit_window]
        if len(hits) >= self.rate_limit_max:
            return False
        hits.append(now)
        self.domain_hits[domain] = hits
        return True

    # ------------------------------------------------------------------ #
    # Exponential backoff & manual CAPTCHA fallback
    # ------------------------------------------------------------------ #
    async def _with_backoff(
        self,
        coro_func,
        *args,
        retries: int = 5,
        base_delay: float = 2.0,
        **kwargs,
    ):
        """Retries an async function with exponential backoff + jitter."""
        for attempt in range(retries):
            try:
                return await coro_func(*args, **kwargs)
            except (RuntimeError, aiohttp.ClientError) as e:
                if attempt == retries - 1:
                    raise
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                print(
                    f"[BACKOFF] Attempt {attempt + 1} failed: {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)

    async def _solve_captcha_with_fallback(
        self, sitekey: str, page_url: str
    ) -> str:
        """Tries automated CAPTCHA solve first, falls back to manual input."""
        try:
            return await self._with_backoff(
                self._solve_recaptcha_v2, sitekey, page_url
            )
        except (TimeoutError, RuntimeError) as e:
            print(f"[CAPTCHA] Automated solve failed: {e}")
            print("[CAPTCHA] Falling back to manual solve.")
            token = input(
                "Solve the CAPTCHA manually, then paste the token here: "
            ).strip()
            return token

    # ------------------------------------------------------------------ #
    # CAPTCHA solving (2Captcha)
    # ------------------------------------------------------------------ #
    async def _solve_recaptcha_v2(self, sitekey: str, page_url: str) -> str:
        """
        Solves a reCAPTCHA v2 challenge using 2Captcha.
        Returns the token that can be injected into the page.
        """
        api_key = os.getenv("CAPTCHA_API_KEY")
        if not api_key:
            raise RuntimeError("CAPTCHA_API_KEY environment variable not set")

        # 1. Submit the CAPTCHA to the solving service
        payload = {
            "key": api_key,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://2captcha.com/in.php", data=payload
            ) as resp:
                resp_json = await resp.json()
                if resp_json.get("status") != 1:
                    raise RuntimeError(f"CAPTCHA submit error: {resp_json}")
                captcha_id = resp_json["request"]

        # 2. Poll for the solution
        for _ in range(20):  # max ~100 seconds
            await asyncio.sleep(5)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://2captcha.com/res.php"
                    f"?key={api_key}&action=get&id={captcha_id}&json=1"
                ) as resp:
                    res_json = await resp.json()
                    if res_json.get("status") == 1:
                        token = res_json["request"]
                        break
                    elif res_json.get("request") != "CAPCHA_NOT_READY":
                        raise RuntimeError(f"CAPTCHA error: {res_json}")
        else:
            raise TimeoutError("CAPTCHA solving timed out")

        return token

    async def _detect_and_solve_captcha(self) -> None:
        if not self.page:
            return
        try:
            sitekey = await self.page.get_attribute(
                'div.g-recaptcha',
                'data-sitekey',
                timeout=22000  # 22 seconds max, not 30!
            )
        except Exception:
            return  # No CAPTCHA found, just move on silently

        if sitekey:
            page_url = self.page.url
            await self._notify_ipc("captcha_detected", {
                "url": page_url, "sitekey": sitekey,
            })
            token = await self._solve_captcha_with_fallback(sitekey, page_url)
            # SECURITY: the token comes from our own solver/operator, but it is
            # still injected into the page via string interpolation. Guard the
            # JS string so a hostile token value can't break out of the quotes.
            safe_token = json.dumps(token)  # yields a properly-escaped JS string
            await self.page.evaluate(
                "(t) => { const el = document.getElementById('g-recaptcha-response');"
                " if (el) el.innerHTML = t; }",
                token,
            )
            await self.page.evaluate(
                "if (window.__recaptchaCallback) window.__recaptchaCallback();"
            )
            self._log_captcha_success(page_url)
            await self._notify_ipc("captcha_solved", {"url": page_url})

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    def _log_captcha_success(self, url: str, notes: str = "") -> None:
        """Emits a structured memory log entry after a successful CAPTCHA solve.

        This is a TRUSTED, tool-originated memory tag (not page content), so it
        is intentionally emitted verbatim. Only untrusted, page-derived text is
        passed through _sanitize_browser_content(); our own log lines are not.
        """
        description = f"Successfully browsed {url} with CAPTCHA solve."
        if notes:
            description += f" Notes: {notes}"
        print(f"[REMEMBER:browse_captcha_success|{description}]")

    # ------------------------------------------------------------------ #
    # Navigation & interaction primitives
    # ------------------------------------------------------------------ #
    async def goto(self, url: str, wait_until: str = "networkidle") -> None:
        """Navigate to a URL with human-like jitter and CAPTCHA detection."""
        domain = urlparse(url).netloc
        await self._notify_ipc("navigate", {"url": url, "domain": domain})
        if not await self._check_rate_limit(domain):
            await asyncio.sleep(2)
        await self._human_delay()
        await self._random_mouse_move()
        try:
            await self.page.goto(url, wait_until=wait_until)
        except Exception as e:
            await self._notify_ipc("error", {
                "where": "goto", "url": url, "message": str(e),
            })
            raise
        await self._human_delay(300, 800)
        await self._detect_and_solve_captcha()
        await self._notify_ipc("navigate_done", {
            "url": url,
            "title": (await self.page.title()) if self.page else "",
        })

    async def click(self, selector: str, force: bool = False) -> None:
        await self._notify_ipc("click", {"selector": selector})
        await self._human_delay()
        await self._random_mouse_move()
        await self.page.click(selector, force=force)
        await self._human_delay(200, 600)

    async def fill(
        self, selector: str, text: str, clear: bool = True
    ) -> None:
        # Don't mirror the actual text — could contain PII (passwords,
        # tokens, personal info during signup flows). Only the selector
        # and a length so the operator can see *that* a fill happened.
        await self._notify_ipc("fill", {
            "selector": selector, "len": len(text), "clear": clear,
        })
        await self._human_delay()
        await self._random_mouse_move()
        if clear:
            await self.page.fill(selector, "")
        await self.page.type(selector, text, delay=random.randint(10, 30))
        await self._human_delay(200, 500)

    async def wait_for(
        self, selector: str, state: str = "visible"
    ) -> None:
        await self.page.wait_for_selector(
            selector, state=state, timeout=self.timeout
        )
        await self._human_delay()

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        return await self.page.evaluate(expression, arg)

    # ------------------------------------------------------------------ #
    # Content & page utilities
    # ------------------------------------------------------------------ #
    async def extract_text(self) -> str:
        """Extract clean readable text from current page, strips HTML.

        SECURITY (CRITICAL #1): the return value is UNTRUSTED web content and
        is sanitized before it can reach Toga's context window / the tool
        parser. Never return raw page text from this method.
        """
        raw = await self.evaluate("() => document.body.innerText")
        return _sanitize_browser_content(raw)

    async def scroll_to_bottom(self, steps: int = 5) -> None:
        """Scroll page gradually like a human reading."""
        for _ in range(steps):
            await self.evaluate(
                "() => window.scrollBy(0, window.innerHeight)"
            )
            await self._human_delay(300, 700)

    async def wait_for_content(self, timeout: int = 56000) -> None:
        """Wait for dynamic content to settle before extracting."""
        await self.page.wait_for_load_state("networkidle", timeout=timeout)
        await self._human_delay(200, 400)

    async def extract_links(
        self, filter_domain: Optional[str] = None
    ) -> List[str]:
        """Extract all href links, optionally filtered by domain.

        SECURITY (CRITICAL #1): href values are attacker-controlled and can
        carry tool-tag payloads (e.g. a link whose text or URL fragment spells
        out [REMEMBER:...]). Each returned link is sanitized before crossing
        the boundary.
        """
        links = await self.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href)"
        )
        if filter_domain:
            links = [l for l in links if filter_domain in l]
        # Sanitize every link individually so no tag pattern survives.
        return [_sanitize_browser_content(l) for l in links]

    async def check_page_health(self) -> Dict[str, Any]:
        """Detect 404s, empty pages, or bot-detection walls.

        SECURITY (CRITICAL #1): document.title is page-controlled text, so the
        title is sanitized. bodyLength/status are numeric/URL and low-risk, but
        we sanitize the URL string too for defense in depth.
        """
        health = await self.evaluate("""
            () => ({
                title: document.title,
                bodyLength: document.body.innerText.length,
                status: window.location.href
            })
        """)
        try:
            if isinstance(health, dict):
                if "title" in health:
                    health["title"] = _sanitize_browser_content(health.get("title"))
                if "status" in health:
                    health["status"] = _sanitize_browser_content(health.get("status"))
        except Exception:
            pass
        return health

    async def page_content(self) -> str:
        """Return the page's HTML.

        SECURITY (CRITICAL #1): full HTML is the richest injection surface of
        all — sanitized before return, same as extract_text().
        """
        raw = await self.page.content()
        return _sanitize_browser_content(raw)

    async def screenshot(self, path: str) -> None:
        await self.page.screenshot(path=path, full_page=True)

    # ------------------------------------------------------------------ #
    # IPC bridge stub (port 9999 - wire in when ipc_bridge.py is ready)
    # ------------------------------------------------------------------ #
    async def _notify_ipc(self, event: str, data: dict) -> None:
        """Send a browser event to the IPC bridge so an external monitor
        (ipc_monitor.py on port 9999) can display Sage's activity in
        real time. Silent-fail by design — IPC must NEVER break a
        browse, click, or search just because the monitor isn't running.

        v2.1.5: replaces the previous no-op stub. Uses ipc_bridge's
        send_ipc_message() which has its own 0.5s connection timeout
        and silently drops if no listener is attached. We still wrap
        in try/except + run_in_executor so even if the bridge raises,
        the browser stays alive.
        """
        try:
            from ipc_bridge import send_ipc_message
            payload = {
                "event": event,
                "ts": TimeManager.epoch(),  # v2.1.6 unified
                **(data or {}),
            }
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, send_ipc_message, event, payload,
            )
        except Exception:
            # IPC failures must never propagate. The monitor is an
            # operator convenience, not a hard dependency.
            pass

    # ------------------------------------------------------------------ #
    # Search engine wrapper (DuckDuckGo default, SearXNG opt-in)
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        engine: str = "duckduckgo",
        base_url: Optional[str] = None,
        max_results: int = 10,
    ) -> List[Dict[str, str]]:
        """
        Perform a search and return a list of dicts:
        {"title": str, "url": str, "snippet": str}

        SIGNIFICANT #4: the default engine is now DuckDuckGo, driven through
        the real browser via goto() to https://duckduckgo.com/?q=<query> and
        parsed from the rendered page. The previous default (SearXNG at
        https://searx.be) was dead, which is why [WEB_SEARCH:] silently failed.

        Backward compatibility: the method signature is unchanged in shape
        (query, engine, base_url, max_results). SearXNG is still fully
        supported as an EXPLICIT opt-in — call search(q, engine="searxng",
        base_url="https://your-searx-instance"). All result dicts are passed
        through _sanitize_browser_content() (titles/snippets/urls are untrusted
        web content — CRITICAL #1).
        """
        eng = (engine or "duckduckgo").lower()

        await self._notify_ipc("search", {
            "query": query, "engine": eng, "max_results": max_results,
        })

        if eng in ("duckduckgo", "ddg", "duck"):
            results = await self._search_duckduckgo(query, max_results)
        elif eng == "searxng":
            # Opt-in only. base_url must be supplied (no dead default baked in).
            results = await self._search_searxng(
                query, base_url or "https://searx.be", max_results
            )
        else:
            raise NotImplementedError(
                f"Unknown search engine '{engine}'. Supported: "
                f"'duckduckgo' (default), 'searxng' (opt-in, requires base_url)."
            )

        # Sanitize every field of every result before it leaves the boundary.
        clean: List[Dict[str, str]] = []
        for r in results[:max_results]:
            clean.append({
                "title": _sanitize_browser_content(r.get("title", "")),
                "url": _sanitize_browser_content(r.get("url", "")),
                "snippet": _sanitize_browser_content(r.get("snippet", "")),
            })

        await self._notify_ipc("search_results", {
            "query": query, "count": len(clean),
        })
        return clean

    async def _search_duckduckgo(
        self, query: str, max_results: int = 10
    ) -> List[Dict[str, str]]:
        """DuckDuckGo search via a real browser navigation (SIGNIFICANT #4).

        Navigates to the live results page and extracts results from the DOM.
        Uses several resilient selector strategies because DDG periodically
        renames its result containers; falls back to the no-JS HTML endpoint
        if the primary layout yields nothing.
        """
        search_url = f"https://duckduckgo.com/?q={quote_plus(query)}&ia=web"
        await self.goto(search_url, wait_until="networkidle")

        # Give the SERP a beat to hydrate, then read results from the DOM. The
        # extraction runs entirely in-page and tolerates layout changes by
        # trying modern (data-testid) selectors first, then older class names.
        try:
            await self.page.wait_for_selector(
                "[data-testid='result'], article[data-testid='result'], "
                ".result, .react-results--main",
                timeout=8000,
            )
        except Exception:
            pass  # best-effort; extraction below still tries

        extract_js = """
            (maxN) => {
                const out = [];
                const seen = new Set();
                const push = (title, url, snippet) => {
                    if (!url || seen.has(url)) return;
                    seen.add(url);
                    out.push({ title: title || '', url: url,
                               snippet: snippet || '' });
                };

                // Strategy 1: modern data-testid layout
                let nodes = document.querySelectorAll(
                    "article[data-testid='result'], [data-testid='result']");
                nodes.forEach(n => {
                    if (out.length >= maxN) return;
                    const a = n.querySelector(
                        "a[data-testid='result-title-a'], h2 a, a[href]");
                    const s = n.querySelector(
                        "[data-testid='result-snippet'], .result__snippet");
                    if (a) push(a.innerText.trim(), a.href,
                               s ? s.innerText.trim() : '');
                });

                // Strategy 2: classic .result blocks
                if (out.length === 0) {
                    nodes = document.querySelectorAll('.result, .web-result');
                    nodes.forEach(n => {
                        if (out.length >= maxN) return;
                        const a = n.querySelector('a.result__a, h2 a, a[href]');
                        const s = n.querySelector('.result__snippet');
                        if (a) push(a.innerText.trim(), a.href,
                                   s ? s.innerText.trim() : '');
                    });
                }
                return out.slice(0, maxN);
            }
        """
        try:
            results = await self.evaluate(extract_js, max_results)
        except Exception:
            results = []

        # Fallback: the no-JS HTML endpoint, which is stable and easy to parse.
        if not results:
            html_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            await self.goto(html_url, wait_until="networkidle")
            try:
                results = await self.evaluate("""
                    (maxN) => {
                        const out = [];
                        document.querySelectorAll('.result__body, .result')
                          .forEach(n => {
                            if (out.length >= maxN) return;
                            const a = n.querySelector('.result__a, a[href]');
                            const s = n.querySelector('.result__snippet');
                            if (a) out.push({
                                title: a.innerText.trim(),
                                url: a.href,
                                snippet: s ? s.innerText.trim() : ''
                            });
                          });
                        return out.slice(0, maxN);
                    }
                """, max_results)
            except Exception:
                results = []

        return results or []

    async def _search_searxng(
        self, query: str, base_url: str, max_results: int = 10
    ) -> List[Dict[str, str]]:
        """SearXNG JSON search (opt-in). Kept intact for anyone running their
        own SearXNG instance; no longer a default because public instances
        (e.g. searx.be) are unreliable/disabled for the JSON API."""
        params = {"q": query, "format": "json"}
        search_url = f"{base_url}/search?{urlencode(params)}"

        await self.goto(search_url)
        await self.wait_for("pre")

        json_text = await self.evaluate(
            "() => document.querySelector('pre').innerText"
        )
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            json_text = await self.evaluate(
                "() => document.querySelector('code').innerText"
            )
            data = json.loads(json_text)

        results = []
        for item in data.get("results", [])[:max_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            })
        return results

    async def search_brave(
        self, query: str, max_results: int = 10
    ) -> List[Dict[str, str]]:
        """
        Brave Search fallback.
        TODO: implement when needed.
        """
        pass

    # ------------------------------------------------------------------ #
    # Universal Signup Automation (Auto-detect form fields on ANY site)
    # ------------------------------------------------------------------ #
    async def _detect_form_fields(self) -> Dict[str, str]:
        """
        Auto-detects common signup form fields by inspecting the DOM.
        Returns a dict of best-guess selectors for email, password, and submit.
        No manual selector input needed — figures it out from the live page.
        """
        return await self.evaluate("""
            () => {
                const fields = {};

                // Find email input
                const email = document.querySelector(
                    'input[type="email"], input[name*="email"], input[placeholder*="email" i]'
                );
                if (email) fields.email = email.id
                    ? '#' + email.id
                    : email.name
                    ? '[name="' + email.name + '"]'
                    : 'input[type="email"]';

                // Find password input
                const pwd = document.querySelector('input[type="password"]');
                if (pwd) fields.password = pwd.id
                    ? '#' + pwd.id
                    : pwd.name
                    ? '[name="' + pwd.name + '"]'
                    : 'input[type="password"]';

                // Find submit button
                const submit = document.querySelector(
                    'button[type="submit"], input[type="submit"], button[name*="submit" i]'
                );
                if (submit) fields.submit = submit.id
                    ? '#' + submit.id
                    : submit.name
                    ? '[name="' + submit.name + '"]'
                    : 'button[type="submit"]';

                return fields;
            }
        """)

    async def signup_auto_detect(self, signup_url: str, username: str, password: str) -> str:
        """
        Universal signup flow:
        1. Navigates to signup_url.
        2. Auto-detects email, password, and submit fields.
        3. Fills them with provided credentials.
        4. Submits the form.
        5. Handles verification if a link appears (optional, can be extended).

        This works for Tuta, AtomicMail, or any site with standard forms.
        """
        print(f"[SIGNUP] Starting universal signup at: {signup_url}")
        # Never log the password (clear-text logging of a credential). Username only.
        print(f"[SIGNUP] Username: {username}")

        await self.goto(signup_url)

        # Auto-detect form fields from live page
        fields = await self._detect_form_fields()
        print(f"[SIGNUP] Detected fields: {fields}")

        if not fields.get("email"):
            raise RuntimeError("[SIGNUP] Could not detect email field on page.")
        if not fields.get("submit"):
            raise RuntimeError("[SIGNUP] Could not find submit button on page.")

        # Fill email (using the username provided)
        await self.fill(fields["email"], username)

        # Fill password
        if fields.get("password"):
            await self.fill(fields["password"], password)
            print(f"[SIGNUP] Password filled.")

        # Submit the form
        await self.click(fields["submit"])
        print(f"[SIGNUP] Form submitted.")

        # Wait a moment for navigation or success message
        await asyncio.sleep(3)

        # Optional: Check for success message or redirect. extract_text() is
        # already sanitized (CRITICAL #1), so the returned page content is safe
        # to hand back to the engine.
        page_content = await self.extract_text()
        if "success" in page_content.lower() or "welcome" in page_content.lower():
            print(f"[SIGNUP] Success detected in page content.")

        print(f"[REMEMBER:signup_success|Completed signup at {signup_url} for user {username}]")
        return page_content

    async def signup_with_temp_email(self, signup_url: str) -> str:
        """
        [DEPRECATED] Legacy function for Guerrilla Mail.
        Use signup_auto_detect for Tuta/AtomicMail.
        """
        # If Sage tries to call this, she should get a warning or it should fallback
        # For now, let's log a warning and suggest the new method
        print("[WARNING] signup_with_temp_email is deprecated. Use signup_auto_detect for Tuta/AtomicMail.")
        # Fallback to auto-detect with a temp email if she insists?
        # Or just raise an error to force the correct path.
        # Let's raise a clear error to guide her.
        raise RuntimeError("[SIGNUP] Please use signup_auto_detect for persistent accounts like Tuta/AtomicMail. This function is for legacy temp-mail only.")


# ---------------------------------------------------------------------- #
# Module-level singleton — MUST live after the class so the methods
# above are part of BrowserTool, not nested locals of get_browser()
# ---------------------------------------------------------------------- #
# v2.1.5 fix (Leo audit): the singleton block was previously inserted
# inside the class definition just after close(), which terminated the
# class scope at zero indent. Every method defined below that point
# was being parsed as a nested local of get_browser() instead of as a
# BrowserTool method, so attempts to call goto/click/fill/search/
# _notify_ipc on instances raised AttributeError. Moving these two
# definitions to AFTER all methods restores the class as a single
# contiguous block. End of structural fix.
# Per-user instances: each namespace (user) gets its own BrowserTool +
# persistent profile, so one account's Sage never sees another's bookmarks,
# history, or cookies. Keyed by ns ("" == owner/default).
_browser_instances: Dict[str, "BrowserTool"] = {}


async def get_browser(ns: Optional[str] = None,
                      headless: bool = False,
                      persist_cookies: bool = False) -> "BrowserTool":
    """
    Return the per-user BrowserTool for namespace `ns`, creating + starting it
    on first use and reusing it thereafter. Each ns has its own persistent
    profile under sage_data. This is what sage_engine.py drives for the
    [BROWSE:] / [WEB_SEARCH:] tags.
    """
    key = ns or ""
    inst = _browser_instances.get(key)
    if inst is None or inst.context is None:
        inst = BrowserTool(ns=ns, headless=headless, persist_cookies=persist_cookies)
        await inst.start()
        _browser_instances[key] = inst
    return inst


# ---------------------------------------------------------------------- #
# Example usage (async entry-point)
# ---------------------------------------------------------------------- #
async def _browser_test():
    tool = BrowserTool(headless=False)
    await tool.start()
    try:
        await tool.goto("https://example.com")
        title = await tool.evaluate("() => document.title")
        print(f"Page title: {title}")

        results = await tool.search("latest subsea cable projects 2026")
        for i, r in enumerate(results, 1):
            print(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}\n")
    finally:
        await tool.close()


if __name__ == "__main__":
    asyncio.run(_browser_test())
