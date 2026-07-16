import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

PROFILE = Path.home() / ".gpt-pro-profile"
STATE = Path.home() / ".gpt-pro"
RUNS = STATE / "runs"
LAUNCH_LOCK = STATE / "launch.lock"
CLIPBOARD_LOCK = STATE / "clipboard.lock"
SLOT_LOCK_DIR = STATE / "slots"
SESSION_COOKIE_PREFIX = "__Secure-next-auth.session-token"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
RUN_ID_MAX_LEN = 100
DEFAULT_GENERATION_TIMEOUT = 60 * 60
DEFAULT_MAX_PARALLEL = 6
MAX_PROMPT_BYTES = 5_000_000
# Initial chatgpt.com navigation. Playwright's implicit 30s default clipped
# slow-but-working loads during transient server/Cloudflare windows (runs
# 5939ab6e/6489a9e7/bf35b1f8 on 2026-06-21 all died on `Page.goto` at 30s while
# identical prompts navigated in ~7s minutes later). 90s rides out the transient;
# one retry covers a first-attempt blip. Tune via the `goto_retry` JSONL signal.
DEFAULT_GOTO_TIMEOUT_MS = 90_000
DEFAULT_GOTO_RETRIES = 1

# Post-send page/tab-close recovery. If the user closes this worker's Chrome tab
# mid-run, the worker reopens the *same* captured conversation URL in a fresh tab
# and resumes monitoring — never re-pasting or re-sending (a resend burns another
# 5-20 min of Pro reasoning). Bounded so a repeatedly-closed window can't loop
# forever; every recovery await is capped by the ORIGINAL generation deadline, so
# recovery never grants a fresh budget. 3 tolerates an accidental repeated close;
# it is safe only because all recovery awaits are deadline-bounded. See
# _monitor_and_finalize / _recover_navigate / classify_recovery.
MAX_PAGE_RECOVERIES = 3
# The latest assistant text must sit unchanged this long before the completion
# gate even *checks* for the no-Stop-button + Copy-button-present signals. Named
# so tests can drive completion without waiting the wall-clock interval.
COMPLETION_STABLE_SECS = 5.0
# The only URL shape recovery will reopen: a canonical ChatGPT conversation route.
# Matched exactly (not a prefix) and stripped of query/fragment so a benign
# ?model=... or #frag can't repoint recovery at a different conversation. A home
# URL, a login redirect, a foreign host, embedded credentials, or a non-default
# port all fail this and are never persisted or reopened.
CONVERSATION_ID_RE = re.compile(r"^/c/([A-Za-z0-9-]+)$")

CHROME_APP = "/Applications/Google Chrome.app"
LAUNCH_DEBUG_PORT = 19222


MAX_PARALLEL_CEILING = 10  # Personal-use ceiling per CLAUDE.md / README.md.


def get_max_parallel() -> int:
    try:
        n = int(os.environ.get("GPT_PRO_MAX_PARALLEL", DEFAULT_MAX_PARALLEL))
    except ValueError:
        n = DEFAULT_MAX_PARALLEL
    clamped = min(MAX_PARALLEL_CEILING, max(1, n))
    if clamped != n:
        log_stage("max_parallel_clamped", requested=n, effective=clamped, ceiling=MAX_PARALLEL_CEILING)
    return clamped

# Chrome flags passed via /usr/bin/open. Curated subset of what Playwright
# would normally pass via launch_persistent_context. Why the LaunchServices
# launch: a process spawned via direct exec from a sshd-detached Popen worker
# bypasses LaunchServices; the resulting Chrome has no app registration,
# isn't in lsappinfo, has no Dock icon, and macOS WindowServer never gives it
# a visible compositor surface. Routing through `open -n -a` puts Chrome in
# the user's Aqua session with a real registered identity. Then connect via
# CDP instead of letting Playwright re-exec Chrome.
#
# Load-bearing flags:
#  - --disable-blink-features=AutomationControlled (anti-detection per CLAUDE.md)
#  - --password-store=basic, --use-mock-keychain, --disable-features=
#    DestroyProfileOnBrowserClose (cookie persistence per CLAUDE.local.md memory)
#  - --window-size pins the OS window (zero-area windows = white-screen)
CHROME_OPEN_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=DestroyProfileOnBrowserClose,DialMediaRouteProvider,MediaRouter,Translate,HttpsUpgrades,PaintHolding",
    "--window-size=1280,800",
]


def _chrome_open_argv(port: int) -> list[str]:
    return [
        *CHROME_OPEN_ARGS,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE}",
    ]


async def pin_viewport_cdp(context, page, *, width: int = 1280, height: int = 800) -> None:
    """Pin renderer viewport via direct CDP, bypassing the racy launch-time path.

    setDeviceMetricsOverride only affects renderer-level emulation — no
    Browser.getWindowForTarget call, no window-bounds dance, no race. Result:
    getBoundingClientRect / window.innerWidth track our pinned viewport, so
    Playwright's "outside of viewport" clickability check stays accurate even
    if the OS window state ever drifts.

    Call this only AFTER a real navigation. The initial about:blank target in a
    persistent context rejects setDeviceMetricsOverride with "Target does not
    support metrics override" (observed on Chrome 147 + persistent profile).
    Best-effort: log and continue on failure rather than killing the run —
    the OS-level --window-size still gives the renderer a sane default, and
    the override is belt-and-suspenders for unstable window states.
    """
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
        })
    except Exception as e:
        log_stage("pin_viewport_skipped", exception=f"{type(e).__name__}: {e}")


def _find_chrome_browser_pid() -> int | None:
    """Return the PID of the gpt-pro Chrome BROWSER process — the parent that
    owns the Cocoa window. Helper/renderer processes carry --type= in argv and
    don't own windows; activating them is a no-op.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-fl", f"user-data-dir={PROFILE}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        try:
            pid_str, cmd = line.split(maxsplit=1)
        except ValueError:
            continue
        if "--type=" not in cmd:
            return int(pid_str)
    return None


def bind_chrome_compositor_surface() -> None:
    """JXA-activate the gpt-pro Chrome process to bind its CoreAnimation surface.

    Chrome on macOS displays web content via a BrowserCompositorCALayerTree
    attached to the Cocoa view. When the worker is launched from a detached
    Popen (sshd → start_new_session=True → no AppKit activation), Chrome can
    skip the LaunchServices/AppKit foreground path and never bind a visible CA
    surface. DOM, CDP, and clicks keep working — but Page.captureScreenshot
    waits forever for a frame, and a human watcher sees a white window.

    PID-targeted activation via NSRunningApplication.activateWithOptions: avoids
    bundle ambiguity (interactive Chrome + gpt-pro Chrome share the bundle) and
    needs no Accessibility permission. Idempotent: if Chrome is already
    foreground the JXA call is a no-op. Does NOT call page.bring_to_front, so
    a concurrent worker mid-paste in another tab is not disturbed. Safe to
    call from anywhere; cheap when not needed.
    """
    if sys.platform != "darwin":
        return
    pid = _find_chrome_browser_pid()
    if pid is None:
        log_stage("chrome_activation_skipped", reason="browser_pid_not_found")
        return
    jxa = (
        'ObjC.import("AppKit");'
        f'$.NSRunningApplication.runningApplicationWithProcessIdentifier({pid})'
        '.activateWithOptions(2);'
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", jxa],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=True,
        )
        log_stage("chrome_activated", pid=pid)
    except Exception as e:
        log_stage("chrome_activation_skipped", exception=f"{type(e).__name__}: {e}")


async def bring_tab_to_front(page) -> None:
    """page.bring_to_front() — switches Chrome's active tab to this worker's page.

    UNSAFE outside UiClipboardLock: another worker mid-paste expects its tab
    to stay frontmost so its `Meta+V` lands in its composer. Only call from
    within the focus+paste / focus+copy critical sections that hold the lock.
    """
    try:
        await page.bring_to_front()
    except Exception as e:
        log_stage("page_bring_to_front_skipped", exception=f"{type(e).__name__}: {e}")


def stderr_jsonl(obj: dict) -> None:
    print(json.dumps(obj, separators=(",", ":")), file=sys.stderr, flush=True)


def log_stage(stage: str, **kwargs) -> None:
    """JSONL progress line to the worker's stderr (which is captured to worker.stderr)."""
    obj = {"ts": round(time.time(), 3), "stage": stage, **kwargs}
    print(json.dumps(obj, separators=(",", ":")), file=sys.stderr, flush=True)


async def safe_screenshot(page, path: Path, *, timeout_ms: int = 10_000) -> None:
    """Best-effort diagnostic screenshot. Never propagate failure.

    Screenshots are artifacts, not part of the critical path. A renderer that's
    busy (e.g. mid-paste reflow on a large prompt) can stall page.screenshot
    long enough to blow Playwright's default 30s timeout — this once killed an
    otherwise-healthy run before Send was even clicked. Bail fast (10s) and let
    the run continue; record the skip in worker.stderr for diagnostics.
    """
    try:
        await page.screenshot(path=str(path), full_page=True, timeout=timeout_ms)
    except Exception as e:
        log_stage("screenshot_skipped", path=path.name, exception=f"{type(e).__name__}: {e}")


def probe_cdp(port: int, *, timeout: float = 1.0) -> bool:
    """True if Chrome's CDP endpoint at the given port responds within `timeout`s.

    The default 1s is for the fast-path (everything's healthy and the request
    returns instantly). Use a longer timeout (e.g. 3s) inside LaunchLock when
    deciding whether to kill processes — under heavy CPU contention from
    multiple in-flight Pro renderers, a healthy Chrome can take >1s
    to respond and we don't want to falsely declare it orphaned.
    """
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout).read()
        return True
    except Exception:
        return False


def _slots_held(skip_slot_id: int | None = None) -> bool:
    """True if any *other* worker currently holds a ParallelSlot lock.

    Probes each slot file with non-blocking LOCK_EX. If a slot is held by
    another process, our LOCK_EX fails with BlockingIOError. We only care
    about *other* workers, not ourselves — when called from inside our own
    ParallelSlot, our own slot file fails this check too (flock conflicts even
    across two fds in the same process). Callers that hold a slot MUST pass
    their own `skip_slot_id` so it isn't counted; otherwise a worker would see
    its own slot as "held" and a wedged-Chrome recovery could never fire —
    even for a lone serial run. The kill-orphans entrypoints that don't hold a
    slot (login/doctor/close-chrome) pass None and count every held slot.
    """
    if not SLOT_LOCK_DIR.exists():
        return False
    skip_name = f"slot-{skip_slot_id}.lock" if skip_slot_id is not None else None
    for path in SLOT_LOCK_DIR.glob("slot-*.lock"):
        if skip_name is not None and path.name == skip_name:
            continue
        try:
            fd = open(path, "w")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()
    return False


def ensure_shared_chrome_running(port: int = LAUNCH_DEBUG_PORT, skip_slot_id: int | None = None) -> bool:
    """Idempotent: launch Chrome bound to PROFILE if its CDP isn't responding.

    Returns True iff this call performed the launch (the "owner" return), False if
    it found Chrome already up. Holds LaunchLock only across the launch path; the
    fast-path (CDP up) does not contend.

    Kill-orphan safety: under heavy load a healthy Chrome can fail a 1s probe.
    Inside LaunchLock we re-probe with a 3s timeout and one retry, AND we refuse
    to kill if any *other* worker is currently holding a ParallelSlot (i.e., has
    a live tab in the same Chrome). The combination prevents the "transient CDP
    stall under load → kill the live Chrome" failure mode. A slot-holding caller
    MUST pass its own `skip_slot_id` so its own slot isn't mistaken for another
    worker's — otherwise a genuinely wedged Chrome could never be recovered.

    On the launch path, also bind the CoreAnimation surface once. Followers
    don't need to bind — Chrome's compositor stays bound for the rest of its
    lifetime once activated.
    """
    if probe_cdp(port):
        return False
    with LaunchLock():
        # Re-probe with a longer timeout — under contention the 1s fast-path
        # probe can falsely fail. Two retries with 0.5s backoff.
        for _ in range(2):
            if probe_cdp(port, timeout=3.0):
                return False
            time.sleep(0.5)
        # CDP is genuinely unresponsive. Refuse to kill if other workers are
        # using the shared Chrome — they'd lose their tabs. Surface a clear
        # error for the operator.
        if _slots_held(skip_slot_id=skip_slot_id):
            raise RuntimeError(
                "Chrome CDP unresponsive but other workers hold ParallelSlots; "
                "refusing to kill shared Chrome. Wait for active runs to finish, "
                "then run `gpt-pro-relay close-chrome --force` if Chrome is wedged."
            )
        _kill_chrome_orphans()
        argv = _chrome_open_argv(port)
        subprocess.Popen(
            ["/usr/bin/open", "-n", "-a", CHROME_APP, "--args", *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if probe_cdp(port):
                log_stage("chrome_cdp_ready", port=port)
                bind_chrome_compositor_surface()
                return True
            time.sleep(0.3)
        raise RuntimeError(f"Chrome CDP not ready on port {port} after 30s")


async def connect_shared_chrome(pw, port: int = LAUNCH_DEBUG_PORT):
    """Connect Playwright to the running Chrome via CDP. Returns the persistent context.

    Caller is responsible for `ctx.new_page()` per worker tab and `page.close()`
    on exit. The returned context's owning browser handle is intentionally NOT
    surfaced — callers must NOT call `browser.close()`, and Playwright's `async
    with` exit drops the connection without terminating Chrome.

    Retries briefly on empty `browser.contexts`: just-launched Chrome's
    persistent default context can lag the CDP `/json/version` ready signal by
    a few hundred ms under contention.
    """
    deadline = time.time() + 5.0
    while True:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        if browser.contexts:
            return browser.contexts[0]
        if time.time() >= deadline:
            raise RuntimeError("connect_over_cdp returned no contexts after 5s")
        await asyncio.sleep(0.25)


def _kill_chrome_orphans() -> None:
    """Kill any stale Chrome procs still bound to our profile.

    Safe to call while holding the file lock — no other gpt-pro worker is
    allowed to be launching Chrome, so anything matching is an orphan from a
    SIGKILL'd or crashed previous worker. Without this, the next Chrome launch
    fails with SingletonLock.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={PROFILE}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return
    pids = [p for p in out.split() if p.strip()]
    if not pids:
        return
    log_stage("orphan_kill_term", pids=pids)
    try:
        subprocess.run(["kill", "-TERM", *pids], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(0.5)
    try:
        stubborn = [p for p in subprocess.run(
            ["pgrep", "-f", f"user-data-dir={PROFILE}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.split() if p.strip()]
    except Exception:
        stubborn = []
    if stubborn:
        log_stage("orphan_kill_kill", pids=stubborn)
        try:
            subprocess.run(["kill", "-KILL", *stubborn], capture_output=True, timeout=5)
        except Exception:
            pass


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


class RunPageClosed(Exception):
    """The worker's page/tab was closed during the post-send phase.

    Distinct from a generic worker_exception so the swallow-to-sentinel helpers
    (`served_assistant_model_slug`, `_copy_button_present`, `read_latest_assistant_text`,
    ...) can re-raise a *close* instead of laundering it into an empty read, a
    "Copy button absent", or a fail-open "unverified" model audit. The recovery
    loop in `_run_with_browser` catches it and reopens the captured conversation
    on a fresh tab. A *live-page* transient (page still open) is NOT this — those
    keep their conservative sentinel so a momentary DOM/eval hiccup doesn't
    trigger a needless tab reopen.
    """


def parse_conversation_url(url: str | None) -> str | None:
    """Return the canonical `https://chatgpt.com/c/<id>` form of `url`, or None.

    The single gate for what recovery is allowed to reopen. Validates scheme,
    host (exact, so embedded credentials or a non-default port are rejected via
    the netloc mismatch), and an exact `/c/<id>` path; query/fragment are
    dropped so recovery reopens the same conversation id regardless of benign
    trailing params. A home URL (`/`), a `/auth/login` redirect, or a foreign
    host all return None → never captured, never reopened.
    """
    if not url:
        return None
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme != "https" or p.netloc != "chatgpt.com":
        return None
    m = CONVERSATION_ID_RE.match(p.path or "")
    if not m:
        return None
    return f"https://chatgpt.com/c/{m.group(1)}"


class _ConversationUrl:
    """Captures the first valid conversation URL seen after Send.

    Immutable once set: a later navigation (e.g. a stray redirect) cannot
    repoint recovery at a different conversation. `capture` is memory-only and
    idempotent so it is safe to call from the *synchronous* `framenavigated`
    observer AND from the monitor poll (the two cover each other's timing gaps).
    `persist` does the one-time log + `conversation.json` write from normal async
    control flow — the disk artifact is a diagnostic breadcrumb for manual
    recovery, NOT load-bearing state (nothing re-spawns the worker from it).
    """

    def __init__(self):
        self._url: str | None = None
        self._persisted = False

    def capture(self, raw_url: str | None) -> None:
        if self._url is None:
            canon = parse_conversation_url(raw_url)
            if canon:
                self._url = canon

    def persist(self, run_dir: Path) -> None:
        if self._url and not self._persisted:
            self._persisted = True
            log_stage("conversation_url_captured")
            try:
                atomic_write(
                    run_dir / "conversation.json",
                    json.dumps({"url": self._url, "captured_at": time.time()}),
                )
            except Exception:
                pass

    def get(self) -> str | None:
        return self._url


def classify_recovery(conv_url: str | None, recoveries: int, max_recoveries: int, remaining: float) -> str:
    """Decide the next action after a detected page close. Pure so the branch
    logic is unit-tested without a browser:

    - "no_url"    — no validated conversation URL was captured → terminate,
                    never guess a conversation. (Message may have been sent; we
                    fail closed rather than resubmit.)
    - "deadline"  — the original generation budget is spent → return a timeout,
                    never a fresh budget.
    - "exhausted" — the bounded recovery count is used up → terminate.
    - "recover"   — reopen the captured conversation on a fresh tab.

    Order matters: no_url and deadline are terminal regardless of remaining
    budget; the exhaustion check only applies once a URL exists and time remains.
    """
    if not conv_url:
        return "no_url"
    if remaining <= 0:
        return "deadline"
    if recoveries >= max_recoveries:
        return "exhausted"
    return "recover"


def gen_run_id() -> str:
    # 4 hex chars from os.urandom prevent collision when two `ask` calls fire
    # in the same wall-clock second without --run-id.
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex() + "-ask"


def validate_run_id(s: str) -> None:
    if not s or len(s) > RUN_ID_MAX_LEN or not RUN_ID_RE.match(s):
        raise SystemExit(f"invalid run_id: {s!r}")


def new_run_dir(label: str) -> Path:
    d = RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def is_logged_in(ctx) -> bool:
    cookies = await ctx.cookies("https://chatgpt.com/")
    return any(c["name"].startswith(SESSION_COOKIE_PREFIX) for c in cookies)


# Composer chip shows the reasoning-EFFORT tier only. Since the 2026-07 GPT-5.6
# redesign the model and effort are separate axes: the chip renders the effort
# tier (Instant / Medium / High / Extra High / Pro) and the model lives on its
# own submenu ("GPT-5.6 Sol"). The desired effort is the top "Pro" tier, which
# the chip renders as the bare label "Pro". The chip exposes NO model-axis signal
# (no aria-label, no dataset, no hidden mirror that differs from innerText), so
# the model is invisible here — the pre-send chip verifies the *effort* and the
# post-send served-slug audit verifies the *model*. See `is_pro_label`.
COMPOSER_CHIP = 'button.__composer-pill[aria-haspopup="menu"]'
PRO_TOKEN = "Pro"
# Ground-truth model slug stamped on the served assistant turn
# (data-message-model-slug). This is the only *authoritative* model signal, but
# it only exists after Send, so it backstops the pre-send chip gate rather than
# replacing it. See served_assistant_model_slug / the post-completion audit.
# An explicit allowlist (not a prefix test): a prefix like "gpt-5-6" would
# wrongly accept a hypothetical non-Pro "gpt-5-6-mini". If OpenAI ships a new
# Pro-family slug, add it here — a one-line, deliberate edit. NOTE the UI display
# name and the slug diverge: "GPT-5.6 Sol" + Pro effort serves as `gpt-5-6-pro`
# (verified 2026-07-09 via a live send). The served slug encodes the effort tier
# too, not just the model: Sol at Pro effort → `gpt-5-6-pro`, but Sol at High
# effort → `gpt-5-6-thinking` (measured 2026-07-09). So on a *present* slug the
# audit catches an effort downgrade as well as a model swap — only Pro-on-Sol
# maps into this allowlist. The pre-send chip ("Pro") is a fast fail; the slug is
# authoritative. The one thing neither can see is a *missing* slug (fail-open).
PRO_MODEL_SLUGS = frozenset({"gpt-5-6-pro"})


def is_pro_label(text: str | None) -> bool:
    """Predicate: chip text indicates the top "Pro" reasoning-effort tier.

    The composer chip shows the effort tier only (Instant / Medium / High /
    Extra High / Pro). "Pro" is the highest tier and the only one containing the
    "Pro" token, so a substring test uniquely identifies it — model names never
    appear in the chip. This verifies *effort*, not model: the model
    ("GPT-5.6 Sol", served slug gpt-5-6-pro) is verified post-send by the
    served-slug audit. Substring (not exact) matching per the redesign-resilience
    convention — ChatGPT relabels this chip across redesigns.
    """
    return bool(text) and PRO_TOKEN in text


SSR_CHIP_PLACEHOLDER = "Model"  # Server-rendered text before React hydrates the user's actual selection.


async def read_composer_chip_text(page, *, timeout: float = 30.0, stable_polls: int = 3) -> str:
    """Read the composer chip's text after React hydration *and* settle.

    The chip's SSR text is 'Model'; hydration replaces it with the user's
    selected effort tier ('Pro', 'High', 'Auto', etc.). We poll until the
    placeholder is gone — reading too early would cause a self-correction click
    on an unhydrated chip, which doesn't open the menu.

    Beyond the SSR→hydrated transition, the chip passes through a *second*
    transition the old "return first non-placeholder value" logic could not see:
    React hydrates the pill optimistically from the persisted/last-used
    selection (e.g. "Extended Pro"), then an async resolution overwrites it with
    the new conversation's actual default (e.g. "Thinking"). Returning the first
    value caught that transient and silently sent to the wrong model (run
    ask-20260531T065451Z: read "Extended Pro", served gpt-5-5-thinking 2.6s
    later). We now require the same non-placeholder text to repeat for
    `stable_polls` consecutive reads (~`stable_polls * 0.2`s) before trusting it.

    Pass `stable_polls=1` only to confirm a deliberate menu click took effect
    (no hydration race there). On timeout, returns "" — never an unstable value
    — so the caller's predicate fails closed. A re-render back through the
    "Model" placeholder breaks the streak entirely (resets the candidate).
    """
    chip = page.locator(COMPOSER_CHIP).first
    await chip.wait_for(state="visible", timeout=timeout * 1000)
    deadline = time.time() + timeout
    last = ""
    stable_count = 0
    while time.time() < deadline:
        cur = (await chip.inner_text()).strip()
        if cur and cur != SSR_CHIP_PLACEHOLDER:
            stable_count = stable_count + 1 if cur == last else 1
            last = cur
            if stable_count >= stable_polls:
                return cur
        else:
            stable_count = 0
            last = ""
        await asyncio.sleep(0.2)
    # Timed out without `stable_polls` consecutive identical reads: the chip
    # never settled. Return "" (not the last transient) so is_pro_label
    # fails closed — an oscillating chip must not be accepted as verified.
    return ""


# The 2026-07 GPT-5.6 redesign split the chip menu into two axes. The flat
# "Intelligence" list (data-testid="composer-intelligence-picker-content") holds
# the effort tiers — Instant / Medium / High / Extra High / Pro — each a
# role='menuitemradio' with a plain-text label; "Pro" is the top tier and maps
# to the gpt-5-6-pro served slug. Selecting it flips the chip to the stable label
# "Pro". A separate model submenu (a role='menuitem' with aria-haspopup='menu'
# labeled "GPT-5.6 Sol") sets the model, but the model is a persistent account
# preference — a freshly loaded chatgpt.com (every worker's starting point)
# defaults to Sol — and it is verified fail-closed post-send by the served-slug
# audit. So the slow path corrects only the *effort* and never navigates the
# fragile, hover-driven model submenu. The "Pro" radio carries no data-testid, so
# we match on its exact accessible name; that also excludes the icon-only "Pro
# effort options" config button (a role='menuitem', not menuitemradio, whose
# accessible name is "Pro effort options", that appears when the Pro row is
# focused).
PRO_LABEL = "Pro"


async def ensure_pro_chip(page, *, run_dir: Path) -> tuple[bool, str | None]:
    """Make the composer chip read the "Pro" effort tier. Returns (ok, observed_text).

    Idempotent fast path: if the chip already reads `is_pro_label` we no-op
    without taking any lock — the typical case, since a fresh page defaults to
    Sol+Pro.

    Slow path (chip in a wrong effort): held under `UiClipboardLock` plus a
    `bring_tab_to_front` because the chip menu is a focus-sensitive Radix portal,
    and `keyboard.press("Escape")` on cleanup paths can close the wrong menu if a
    concurrent worker brings its tab to front. It opens the chip menu and clicks
    the "Pro" effort leaf (role='menuitemradio') directly. It does NOT touch the
    model submenu — the model comes from the account default and is verified
    fail-closed post-send by the served-slug audit; self-correcting it here would
    add fragile submenu navigation for a rare drift the audit already catches.
    """
    chip = page.locator(COMPOSER_CHIP).first
    text = await read_composer_chip_text(page, timeout=30.0)
    if is_pro_label(text):
        return True, text

    with UiClipboardLock():
        bind_chrome_compositor_surface()
        await bring_tab_to_front(page)

        await chip.click()
        try:
            await page.wait_for_selector('[role="menu"]', timeout=5000)
        except Exception as e:
            await safe_screenshot(page, run_dir / "error-chip_menu_open.png")
            (run_dir / "error.html").write_text(await page.content())
            log_stage("error", reason="chip_menu_open_failed", exception=f"{type(e).__name__}: {e}")
            return False, text

        # The chip menu is the newest-mounted [role=menu]. The "Pro" effort tier
        # is a direct menuitemradio leaf — anchor the regex so a future relabel
        # doesn't silently match (an intentional product change worth reviewing
        # rather than auto-accepting).
        menu = page.locator('[role="menu"]').last
        item = menu.get_by_role("menuitemradio", name=re.compile(rf"^{re.escape(PRO_LABEL)}$"))
        try:
            await item.first.click(timeout=5000)
        except Exception as e:
            await safe_screenshot(page, run_dir / "error-chip_menuitem.png")
            (run_dir / "error.html").write_text(await page.content())
            log_stage("error", reason="chip_menuitem_missing", exception=f"{type(e).__name__}: {e}")
            await page.keyboard.press("Escape")
            return False, text

        # Poll up to 5s for the chip text to settle on the "Pro" effort label.
        deadline = time.time() + 5.0
        final_text = text
        while time.time() < deadline:
            final_text = (await chip.inner_text()).strip()
            if is_pro_label(final_text):
                return True, final_text
            await asyncio.sleep(0.2)
        return False, final_text


# The selected MODEL is invisible in the chip — it's the checked radio inside the
# chip menu's model submenu. `doctor` reads it to confirm the account default is
# GPT-5.6 Sol; the worker send path does NOT (its authoritative model gate is the
# served-slug audit). This closes the diagnostic gap the effort-only chip opened:
# without it, `doctor` reports green on a wrong model whose default has drifted.
SOL_MODEL_TOKEN = "Sol"  # distinctive substring: no other model row contains it


def classify_model_status(model_text: str | None) -> str:
    """Classify a read model label for `doctor`. `None` (unreadable menu) →
    "unknown"; a label containing "Sol" → "ok"; anything else →
    "unexpected: <label>" (a confirmed wrong model)."""
    if model_text is None:
        return "unknown"
    if SOL_MODEL_TOKEN in model_text:
        return "ok"
    return f"unexpected: {model_text!r}"


def doctor_exit_ok(logged_in: bool, chip_status: str, model_status: str) -> bool:
    """`doctor` succeeds only when login, the Pro-effort chip, AND the Sol model
    are all POSITIVELY confirmed ("ok"). Anything else — a wrong effort/model
    ("unexpected: ..."), an unreadable chip/menu ("unknown"/"failed"), or the
    not-logged-in "skipped" — is NOT a confirmation, so doctor goes red. doctor
    is a diagnostic, not the hot path: a read failure surfacing as non-green (the
    operator re-runs) is correct, whereas a false green would defeat doctor's
    whole purpose of catching a setup drifted off GPT-5.6 Sol + Pro."""
    return logged_in and chip_status == "ok" and model_status == "ok"


def classify_served_audit(served_slug: str | None, menu_model: str | None) -> str:
    """Post-send model audit verdict. The served `data-message-model-slug` is
    authoritative (it encodes model *and* effort tier), but only exists on a
    stamped turn. When it's absent, `menu_model` (a read-only chip-menu model
    read, the fallback) is the independent backstop. Verdicts:

    - "verified"            — slug present and in `PRO_MODEL_SLUGS` (Sol+Pro).
    - "slug_mismatch"       — slug present but not allowlisted → FATAL.
    - "menu_mismatch"       — slug absent, menu confirms a non-Sol model → FATAL
                              (closes the missing-slug wrong-model hole).
    - "model_ok_slug_missing" — slug absent, menu confirms Sol → fail-OPEN, but
                              model is confirmed (effort stays unverified).
    - "unverified_missing_slug" — slug absent AND menu unreadable → fail-OPEN
                              (a double selector break must not brick the tool).
    """
    if served_slug:
        return "verified" if served_slug in PRO_MODEL_SLUGS else "slug_mismatch"
    if menu_model is None:
        return "unverified_missing_slug"
    return "model_ok_slug_missing" if SOL_MODEL_TOKEN in menu_model else "menu_mismatch"


async def read_selected_model(page, *, timeout: float = 10.0) -> str | None:
    """Return the currently-selected model's label from the chip menu, or None.

    Read-only diagnostic (no selection, no send). Opens the Intelligence menu,
    hovers the model submenu trigger — the `aria-haspopup='menu'` menuitem WITH
    visible text, which excludes the icon-only "Pro effort options" config button
    (empty text) — waits for the submenu, and reads its checked model radio. The
    submenu-mounted guard (`count() >= 2`) is load-bearing: the *main* menu's
    checked radio is the "Pro" EFFORT tier, so reading `.last` before the submenu
    mounts would wrongly return the effort as the model. Returns None on any
    failure so `doctor` degrades to "unknown" rather than erroring. Always closes
    the menu with Escape.
    """
    chip = page.locator(COMPOSER_CHIP).first
    try:
        await chip.click()
        await page.wait_for_selector('[role="menu"]', timeout=timeout * 1000)
        trigger = page.locator(
            '[role="menu"] [role="menuitem"][aria-haspopup="menu"]'
        ).filter(has_text=re.compile(r"\S")).first
        await trigger.hover()
        await page.locator('[role="menu"]').nth(1).wait_for(state="visible", timeout=timeout * 1000)
        deadline = time.time() + 3.0
        model = None
        while time.time() < deadline:
            if await page.locator('[role="menu"]').count() >= 2:
                submenu = page.locator('[role="menu"]').last
                model = await submenu.evaluate(
                    """m => { const r = m.querySelector('[role="menuitemradio"][aria-checked="true"]'); return r ? (r.innerText || '').trim() : null; }"""
                )
                if model:
                    break
            await asyncio.sleep(0.2)
        return model or None
    except Exception:
        # In the served-slug audit fallback, a close must recover rather than
        # read as "menu unreadable" (which fail-opens). `doctor` catches this as
        # a red "failed" status, which is also correct.
        if page.is_closed():
            raise RunPageClosed()
        return None
    finally:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.1)
            await page.keyboard.press("Escape")
        except Exception:
            pass


async def wait_for_login(ctx, *, timeout: float = 600.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            if await is_logged_in(ctx):
                return True
        except Exception:
            return False
        await asyncio.sleep(1.0)
    return False


# ---- doctor ----

async def cmd_doctor() -> int:
    run_dir = new_run_dir("doctor")
    ensure_shared_chrome_running()
    async with async_playwright() as pw:
        ctx = await connect_shared_chrome(pw)
        page = await ctx.new_page()
        try:
            # bind only, NOT bring_to_front: a worker may be mid-paste in another
            # tab. Screenshots work on background tabs in a windowed Chrome.
            bind_chrome_compositor_surface()
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            await pin_viewport_cdp(ctx, page)
            ok = await wait_for_login(ctx, timeout=30.0)
            await page.screenshot(path=str(run_dir / "page.png"), full_page=True)
            (run_dir / "page.html").write_text(await page.content())
            chip_status = "skipped"
            chip_text = None
            model_status = "skipped"
            model_text = None
            if ok:
                try:
                    chip_text = await read_composer_chip_text(page, timeout=10.0)
                    chip_status = "ok" if is_pro_label(chip_text) else f"unexpected: {chip_text!r}"
                except Exception as e:
                    chip_status = f"failed: {type(e).__name__}: {e}"
                # Read the selected model (read-only) so a default drifted off Sol
                # is visible — the effort chip alone can't reveal it.
                try:
                    model_text = await read_selected_model(page, timeout=10.0)
                    model_status = classify_model_status(model_text)
                except Exception as e:
                    model_status = f"failed: {type(e).__name__}: {e}"
            # doctor is green ONLY when login + Pro effort + Sol model are all
            # positively confirmed; a wrong OR unconfirmable chip/model is red.
            checks_ok = doctor_exit_ok(ok, chip_status, model_status)
            result = {
                "status": "ok" if checks_ok else ("needs_reauth" if not ok else "misconfigured"),
                "url": page.url,
                "chip": chip_status,
                "chip_text": chip_text,
                "model": model_status,
                "model_text": model_text,
                "run_dir": str(run_dir),
            }
        finally:
            try:
                await page.close()
            except Exception:
                pass
    print(json.dumps(result, indent=2))
    return 0 if checks_ok else 1


# ---- login ----

async def cmd_login() -> int:
    ensure_shared_chrome_running()
    async with async_playwright() as pw:
        ctx = await connect_shared_chrome(pw)
        page = await ctx.new_page()
        try:
            # login is interactive — user needs the tab frontmost. If a worker
            # is mid-paste, login will hijack its focus; documented as
            # "don't run login while workers are active."
            bind_chrome_compositor_surface()
            await bring_tab_to_front(page)
            await page.goto("https://chatgpt.com/")
            await pin_viewport_cdp(ctx, page)
            print(f"Chrome bound to {PROFILE}", file=sys.stderr)
            print("Sign in to ChatGPT in the window. Login auto-detects.", file=sys.stderr)
            ok = await wait_for_login(ctx)
            print("Login detected." if ok else "Timed out without detecting login.", file=sys.stderr)
        finally:
            try:
                await page.close()
            except Exception:
                pass
    return 0 if ok else 1


# ---- ask: parent-side submit + wait ----

def _spawn_worker(run_id: str, run_dir: Path) -> None:
    worker_stdout = (run_dir / "worker.stdout").open("ab")
    worker_stderr = (run_dir / "worker.stderr").open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "gpt_pro.cli", "_run", run_id],
        stdin=subprocess.DEVNULL,
        stdout=worker_stdout,
        stderr=worker_stderr,
        start_new_session=True,
        close_fds=True,
    )


async def _wait_for_result(run_dir: Path, *, poll_interval: float = 0.5, timeout: float | None = None) -> dict | None:
    """Polls run_dir/result.json until it appears or timeout. Returns parsed dict, or None on timeout."""
    result_path = run_dir / "result.json"
    deadline = (time.time() + timeout) if timeout is not None else None
    while True:
        if result_path.exists():
            try:
                return json.loads(result_path.read_text())
            except json.JSONDecodeError:
                await asyncio.sleep(0.1)
                continue
        if deadline is not None and time.time() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


def _emit_terminal(result: dict, run_dir: Path, output_path: Path | None = None) -> int:
    status = result.get("status", "error")
    if status == "ok":
        response_path = run_dir / "response.md"
        if response_path.exists():
            content = response_path.read_text()
            if output_path is not None:
                resolved = output_path.expanduser()
                resolved.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(resolved, content)
                result = {**result, "output": str(resolved)}
            else:
                sys.stdout.write(content)
                sys.stdout.flush()
    stderr_jsonl(result)
    if status == "ok":
        return 0
    if status == "timeout":
        return 3
    return 1


async def cmd_ask(args) -> int:
    prompt_text = sys.stdin.read()
    if not prompt_text.strip():
        stderr_jsonl({"status": "error", "reason": "empty_prompt"})
        return 2

    prompt_bytes = len(prompt_text.encode())
    if prompt_bytes > MAX_PROMPT_BYTES:
        stderr_jsonl({
            "status": "error",
            "reason": "prompt_too_large",
            "bytes": prompt_bytes,
            "limit": MAX_PROMPT_BYTES,
        })
        return 2

    run_id = args.run_id or gen_run_id()
    validate_run_id(run_id)
    run_dir = RUNS / run_id
    prompt_sha = hashlib.sha256(prompt_text.encode()).hexdigest()

    spawn_worker = True
    if run_dir.exists():
        meta_path = run_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                meta = {}
            existing_sha = meta.get("prompt_sha256")
            if existing_sha == prompt_sha:
                stderr_jsonl({
                    "status": "submitted",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "prompt_sha256": prompt_sha,
                    "attached": True,
                })
                spawn_worker = False
            elif existing_sha is not None:
                stderr_jsonl({
                    "status": "error",
                    "reason": "run_id_conflict",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                })
                return 2
            else:
                # meta.json exists but is missing/corrupt prompt_sha256.
                # Most likely cause: a prior `ask` was killed between mkdir
                # and the atomic_write of meta.json. Fail closed rather than
                # spawn a duplicate worker that would race on result.json.
                stderr_jsonl({
                    "status": "error",
                    "reason": "run_id_conflict_no_sha",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "hint": "Delete the run_dir and retry, or use a fresh --run-id.",
                })
                return 2

    if spawn_worker:
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(run_dir / "prompt.md", prompt_text)
        meta = {
            "run_id": run_id,
            "created_at": time.time(),
            "prompt_sha256": prompt_sha,
        }
        atomic_write(run_dir / "meta.json", json.dumps(meta))
        _spawn_worker(run_id, run_dir)
        stderr_jsonl({
            "status": "submitted",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "prompt_sha256": prompt_sha,
        })

    if args.no_wait:
        return 0

    result = await _wait_for_result(run_dir, timeout=args.generation_timeout)
    if result is None:
        stderr_jsonl({
            "status": "pending",
            "reason": "wait_timeout",
            "run_id": run_id,
            "run_dir": str(run_dir),
        })
        return 124
    return _emit_terminal(result, run_dir, output_path=args.output)


# ---- fetch ----

async def cmd_fetch(args) -> int:
    validate_run_id(args.run_id)
    run_dir = RUNS / args.run_id
    if not run_dir.exists():
        stderr_jsonl({"status": "error", "reason": "not_found", "run_id": args.run_id})
        return 4
    result = await _wait_for_result(run_dir, poll_interval=args.poll_interval, timeout=args.timeout)
    if result is None:
        stderr_jsonl({
            "status": "pending",
            "reason": "fetch_timeout",
            "run_id": args.run_id,
            "run_dir": str(run_dir),
        })
        return 124
    return _emit_terminal(result, run_dir, output_path=args.output)


# ---- _run: detached worker driving Chrome ----

async def _log_response(resp, log: list) -> None:
    try:
        if "/backend-api/" in resp.url or "/conversation" in resp.url:
            log.append({"ts": time.time(), "url": resp.url, "status": resp.status})
    except Exception:
        pass


async def _focus_and_paste(page, composer, prompt_text: str) -> None:
    """Hold UiClipboardLock; activate Chrome; focus composer; pbcopy + Cmd+V; wait for paste to settle; restore clipboard.

    The lock spans focus + paste, not just pbcopy/pbpaste, because `Meta+V` is
    dispatched by Chrome to the OS-active window's active tab — a concurrent
    worker that calls `bring_to_front()` mid-keystroke would redirect this
    paste to its own composer. We also wait for ProseMirror to actually ingest
    the paste (composer text length reaches a sentinel) before releasing the
    lock — `keyboard.press("Meta+V")` returns when the CDP event is dispatched,
    not when the paste handler has finished. Without the wait, the next worker
    can pbcopy something else while ProseMirror is still consuming our paste.

    Why pbcopy + Cmd+V instead of `keyboard.insert_text`: ProseMirror re-renders
    the whole document on synthetic input events and chokes on multi-hundred-KB
    inputs; Cmd+V hits the contenteditable's optimized paste handler. Saves and
    restores the user's clipboard since the Mac mini may be in interactive use.
    """
    with UiClipboardLock():
        bind_chrome_compositor_surface()
        await bring_tab_to_front(page)
        try:
            before = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            before = None
        try:
            await composer.click()
            subprocess.run(["pbcopy"], input=prompt_text, text=True, check=True, timeout=10)
            await page.keyboard.press("Meta+V")
            # Wait until the send button mounts before releasing the lock. The
            # send button is only mounted when the composer has non-empty
            # content — its presence proves ProseMirror's paste handler ran
            # to completion. Without this gate, the next worker can pbcopy
            # over our prompt while our paste handler is still reading the
            # OS clipboard. Same selector used by the actual send-click below.
            try:
                await page.wait_for_selector(
                    '[data-testid="send-button"], button[aria-label="Send prompt"], button[aria-label="Send message"]',
                    timeout=10000, state="visible",
                )
            except Exception as e:
                log_stage("paste_settle_skipped", exception=f"{type(e).__name__}: {e}")
        finally:
            if before is not None:
                try:
                    subprocess.run(["pbcopy"], input=before, text=True, timeout=5)
                except Exception:
                    pass


async def served_assistant_model_slug(page) -> str | None:
    """Read the latest assistant turn's `data-message-model-slug`.

    This attribute is the ground truth of which model actually served the turn
    (unlike the composer chip, a pre-send projection of client state). Returns
    the slug string, or None if no slugged assistant turn is present. A missing
    attribute (selector drift / not-yet-rendered) yields None — the caller
    treats that as "unverified" and logs it, degrading fail-open on the audit
    only (the pre-send chip gate remains the primary defense).
    """
    try:
        return await page.evaluate("""() => {
            const msgs = Array.from(document.querySelectorAll(
                '[data-message-author-role="assistant"][data-message-model-slug]'
            ));
            const last = msgs[msgs.length - 1];
            return last ? last.getAttribute('data-message-model-slug') : null;
        }""")
    except Exception:
        # A CLOSED page here would otherwise degrade to a fail-open "unverified"
        # audit (slug None + menu None) and return a wrong-model turn as ok. Raise
        # so the recovery loop reopens and re-audits. A live-page miss keeps None.
        if page.is_closed():
            raise RunPageClosed()
        return None


async def _copy_button_present(page) -> bool:
    """True if the latest assistant turn's post-completion Copy button is mounted.

    The turn-action toolbar (copy/regenerate/share) only renders after the turn
    is finalized — Pro's mid-run "thinking summary" panel does not have
    it. Used as the affirmative completion gate alongside text-stable + no Stop
    button: the text-only heuristic false-positives because Pro can sit on a
    summary string for tens of seconds while reasoning continues silently with
    no Stop button visible.
    """
    try:
        return await page.evaluate("""() => {
            const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
            const last = msgs[msgs.length - 1];
            if (!last) return false;
            const container = last.closest('[data-testid^="conversation-turn"]') || last.parentElement;
            if (!container) return false;
            return !!container.querySelector('[data-testid="copy-turn-action-button"]');
        }""")
    except Exception:
        # A close must not read as "not yet complete" — that would spin the
        # monitor to the deadline. Raise so the loop recovers; a live-page miss
        # stays False (conservatively "not complete yet").
        if page.is_closed():
            raise RunPageClosed()
        return False


async def _copy_button_extract(page) -> str | None:
    """Hold UiClipboardLock; activate Chrome + bring tab to front; click Copy; read pbpaste; ALWAYS restore.

    Preserves markdown fidelity (math, code fences, tables) where innerText mangles them.
    Returns None if the copy didn't change the clipboard (button missing, permission denied,
    not on macOS, etc.) — caller should fall back to innerText.

    The lock spans baseline pbpaste + click + post-click pbpaste + restore so a
    concurrent worker's clipboard write cannot race into our `after` read. The
    `try/finally` ensures `before` is restored whenever a Copy click was attempted,
    even on early-return paths — otherwise we'd leak the assistant's response into
    the user's clipboard if pbpaste(after) raises.
    """
    with UiClipboardLock():
        bind_chrome_compositor_surface()
        await bring_tab_to_front(page)
        try:
            before = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return None

        try:
            clicked = await page.evaluate("""() => {
                const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
                const last = msgs[msgs.length - 1];
                if (!last) return false;
                const container = last.closest('[data-testid^="conversation-turn"]') || last.parentElement;
                if (!container) return false;
                const btn = container.querySelector('[data-testid="copy-turn-action-button"]');
                if (!btn) return false;
                btn.click();
                return true;
            }""")
        except Exception:
            # A close mid-extract must recover, not silently downgrade a clean
            # markdown answer to stale innerText. A live-page miss keeps None.
            if page.is_closed():
                raise RunPageClosed()
            return None
        if not clicked:
            return None

        # Click was attempted — from here, always restore `before` no matter how we exit.
        try:
            await asyncio.sleep(0.6)
            try:
                after = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
            except Exception:
                return None
            if after and after != before and after.strip():
                return after
            return None
        finally:
            # Restore in finally so an exception in pbpaste-after, or early return,
            # cannot leave the assistant's just-copied response on the user's clipboard.
            try:
                subprocess.run(["pbcopy"], input=before, text=True, timeout=5)
            except Exception:
                pass


class _FlockGuard:
    """Plain mutual-exclusion fcntl flock context manager. Held briefly only."""
    def __init__(self, path: Path):
        self.path = path
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(self.path, "w")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        except BaseException:
            fd.close()
            raise
        self._fd = fd
        return self

    def __exit__(self, *_):
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        finally:
            self._fd.close()
            self._fd = None


class LaunchLock(_FlockGuard):
    """Held only across the CDP-probe-and-conditional-launch path. Never held during
    a run's per-tab work — that would re-introduce the old whole-section serialization."""
    def __init__(self):
        super().__init__(LAUNCH_LOCK)


class UiClipboardLock(_FlockGuard):
    """Held across the foreground+focus+pbcopy+Meta+V transaction (paste path) and
    across baseline pbpaste + click-Copy + post-click pbpaste + restore (extract path).

    Wider than just `pbpaste` because `Meta+V` follows OS focus and ChatGPT's
    Copy-button onClick uses `navigator.clipboard.writeText` which requires
    document focus. Two parallel workers must not interleave these phases or
    they will silently swap each other's prompts/responses through the global
    macOS pasteboard."""
    def __init__(self):
        super().__init__(CLIPBOARD_LOCK)


class ParallelSlot:
    """File-lock semaphore admitting at most max_parallel concurrent _run workers.

    On enter, tries non-blocking LOCK_EX on slot files 0..N-1 in order; if all
    are taken, polls every 2s. Emits one `slot_queued` JSONL line when waiting
    starts and a `slot_acquired` line on success with the wait duration.

    `slot_id` is public: the worker passes it to `ensure_shared_chrome_running`
    so a wedged-Chrome recovery can skip its own slot (see `_slots_held`). It is
    None before acquisition and after release.
    """
    def __init__(self, max_parallel: int):
        self.max_parallel = max_parallel
        self._fd = None
        self.slot_id = None

    def __enter__(self):
        SLOT_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        wait_start = time.time()
        queued_logged = False
        while True:
            for slot_id in range(self.max_parallel):
                fd = open(SLOT_LOCK_DIR / f"slot-{slot_id}.lock", "w")
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fd.close()
                    continue
                except BaseException:
                    fd.close()
                    raise
                self._fd = fd
                self.slot_id = slot_id
                log_stage(
                    "slot_acquired",
                    slot_id=slot_id,
                    max_parallel=self.max_parallel,
                    waited_secs=round(time.time() - wait_start, 2),
                )
                return self
            if not queued_logged:
                log_stage("slot_queued", max_parallel=self.max_parallel)
                queued_logged = True
            time.sleep(2.0)

    def __exit__(self, *_):
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        finally:
            self._fd.close()
            self._fd = None
            self.slot_id = None


async def _browser_run(run_id: str, run_dir: Path, prompt_text: str) -> dict:
    network_log: list = []

    def err(reason: str, extra: dict | None = None) -> dict:
        d = {
            "status": "error",
            "reason": reason,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "exit_code": 1,
        }
        if extra:
            d.update(extra)
        return d

    log_stage("start", run_id=run_id)
    with ParallelSlot(get_max_parallel()) as slot:
        # Pass our own slot id so a wedged-Chrome recovery skips it — otherwise
        # the worker counts its own held slot and never recovers (see _slots_held).
        return await _run_with_browser(run_id, run_dir, prompt_text, network_log, err, slot.slot_id)


async def _goto_with_retry(
    page,
    url: str,
    *,
    timeout_ms: int = DEFAULT_GOTO_TIMEOUT_MS,
    retries: int = DEFAULT_GOTO_RETRIES,
) -> None:
    """Navigate to `url`, retrying only on Playwright TimeoutError.

    This is a PRE-SEND navigation retry: no prompt is typed and no Pro reasoning
    is consumed, so it sits outside the "no auto-retry on submitted prompts"
    invariant (which exists to avoid re-burning 5-20 min of reasoning on a sent
    prompt). The catch is scoped to TimeoutError on purpose — a fast connection
    error, CDP disconnect, or auth-redirect navigation error is a different
    failure class that must surface immediately, not be masked behind retries.
    Exhausting the retry budget re-raises, so the worker still fails closed
    (-> worker_exception, run_dir surfaced).
    """
    for attempt in range(retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except PlaywrightTimeoutError:
            if attempt >= retries:
                raise
            log_stage("goto_retry", url=url, attempt=attempt + 1, timeout_ms=timeout_ms)


def _attach_response_logger(page, network_log: list) -> None:
    """Wire the network-response logger onto a page. Reused for the initial tab
    and every replacement tab so recovered pages keep populating network.json."""
    page.on("response", lambda r: asyncio.create_task(_log_response(r, network_log)))


async def read_latest_assistant_text(page) -> str:
    """Latest assistant turn's innerText, or "" on a *live-page* transient read
    failure. Raises RunPageClosed if the tab was closed — so the monitor loop
    reopens the conversation instead of silently treating the close as empty
    text (the old bug: every poll returned "" and the run spun to the deadline).
    """
    try:
        return await page.evaluate(
            """() => {
                const e = document.querySelectorAll('[data-message-author-role="assistant"]');
                return e.length ? e[e.length - 1].innerText : '';
            }"""
        )
    except Exception:
        if page.is_closed():
            raise RunPageClosed()
        return ""


async def _stop_button_count(page) -> int:
    """Count of visible Stop buttons (0 == generation not actively streaming).
    Raises RunPageClosed on a closed tab; a live-page failure returns 1 so an
    ambiguous read is treated conservatively as "still running" and never
    false-completes a turn."""
    try:
        return await page.locator('button[aria-label*="Stop"], [data-testid*="stop"]').count()
    except Exception:
        if page.is_closed():
            raise RunPageClosed()
        return 1


async def _recover_navigate(ctx, page, conv_url: str, *, deadline: float) -> str | None:
    """Reopen the captured conversation on a replacement `page`. Returns None on
    success, or a failure-reason string:

    - "closed"       — the replacement tab was itself closed mid-recovery.
    - "deadline"     — the original generation budget ran out (→ caller returns a
                       timeout, never a fresh budget).
    - "nav_timeout" / "nav_error" — navigation failed on a live tab.
    - "redirect"     — landed somewhere that is not the SAME conversation.
    - "auth_lost"    — session dropped (a login redirect can briefly keep /c/<id>).
    - "shell_missing"— the conversation DOM never rendered a message turn; feeding
                       that empty page into the monitor would just spin to the
                       deadline, so fail closed instead.

    Safe to retry: this is a GET of an existing conversation, never a submission.
    Every await is capped by the remaining deadline so recovery cannot extend the
    generation budget.
    """
    loop = asyncio.get_running_loop()
    remaining = deadline - loop.time()
    if remaining <= 0:
        return "deadline"
    nav_timeout_ms = int(min(DEFAULT_GOTO_TIMEOUT_MS / 1000.0, remaining) * 1000)
    try:
        await page.goto(conv_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
    except PlaywrightTimeoutError:
        return "closed" if page.is_closed() else "nav_timeout"
    except Exception:
        return "closed" if page.is_closed() else "nav_error"

    # pin_viewport_cdp and is_logged_in are the two awaits between the bounded
    # goto and the bounded shell-wait; cap BOTH by the remaining deadline so the
    # "every recovery await is deadline-bounded" invariant is literally true and
    # a wedged CDP session can't overrun the generation budget (belt-and-braces:
    # the deadline is never reset, so an overrun would still self-correct to a
    # timeout — but the invariant should hold in code, not just in effect). pin
    # is best-effort, so a timeout there is swallowed; an auth check that can't
    # complete in budget fails closed as auth_lost.
    remaining = deadline - loop.time()
    if remaining <= 0:
        return "deadline"
    try:
        await asyncio.wait_for(pin_viewport_cdp(ctx, page), timeout=max(0.5, min(10.0, remaining)))
    except Exception:
        pass  # best-effort viewport pin; the OS --window-size is a sane fallback

    # URL equality is necessary but NOT sufficient — validate the canonical id
    # (ignoring query/fragment) so a redirect to home/login/another conversation
    # is caught even if the address bar still momentarily reads /c/<id>.
    if parse_conversation_url(page.url) != conv_url:
        return "redirect"
    remaining = deadline - loop.time()
    if remaining <= 0:
        return "deadline"
    try:
        if not await asyncio.wait_for(is_logged_in(ctx), timeout=max(0.5, min(10.0, remaining))):
            return "auth_lost"
    except Exception:
        return "closed" if page.is_closed() else "auth_lost"

    remaining = deadline - loop.time()
    if remaining <= 0:
        return "deadline"
    try:
        await page.wait_for_selector(
            "[data-message-author-role]",
            timeout=int(min(30.0, remaining) * 1000),
            state="attached",
        )
    except PlaywrightTimeoutError:
        return "closed" if page.is_closed() else "shell_missing"
    except Exception:
        return "closed" if page.is_closed() else "shell_missing"
    return None


async def _monitor_and_finalize(page, *, run_dir, run_id, deadline, send_ts, conv, err) -> dict:
    """Post-send monitor + finalize on `page`. Returns a terminal result dict, or
    raises RunPageClosed if the tab closes (the recovery loop reopens + retries).

    Receives NO prompt text, composer, or Send handle: it is *structurally*
    incapable of re-pasting or re-sending. The absolute `deadline` is passed in
    and never reset, so re-running this after a recovery keeps the original
    generation budget. Re-running after completion is idempotent — the completed
    turn re-detects immediately (Copy button present) and response.md is
    overwritten atomically.
    """
    loop = asyncio.get_running_loop()
    last_text = ""
    last_change = loop.time()
    snapshot_idx = 0
    next_snap = loop.time() + 5.0
    completed = False
    while loop.time() < deadline:
        now = loop.time()
        # Backup URL capture (covers a missed framenavigated event) + one-time
        # persist. Idempotent and immutable once set.
        conv.capture(page.url)
        conv.persist(run_dir)
        if now >= next_snap:
            await safe_screenshot(page, run_dir / f"streaming-{snapshot_idx:03d}.png")
            snapshot_idx += 1
            next_snap = now + 30.0
        cur = await read_latest_assistant_text(page)
        if cur != last_text:
            last_change = now
            last_text = cur
        if cur and (now - last_change) >= COMPLETION_STABLE_SECS:
            if await _stop_button_count(page) == 0 and await _copy_button_present(page):
                completed = True
                break
        await asyncio.sleep(1.5)

    log_stage(
        "completion_detected" if completed else "completion_timeout",
        chars=len(last_text),
        elapsed_secs=round(loop.time() - send_ts, 1),
    )
    await safe_screenshot(page, run_dir / "final.png")
    try:
        final_html = await page.content()
    except Exception:
        if page.is_closed():
            raise RunPageClosed()
        final_html = ""
    (run_dir / "final.html").write_text(final_html)

    # Defense-in-depth against post-recovery drift: _recover_navigate validates
    # the conversation once, but if the reopened background tab were later
    # navigated to a DIFFERENT /c/<id> (e.g. a human grabbed it), extracting and
    # auditing here would return another conversation's answer as ok — the worst
    # failure class. page.url is a cached property (no channel call, never
    # raises); both sides are canonicalized. Fail closed on a mismatch. On the
    # first, non-recovered pass this is a no-op (page.url == the captured URL).
    captured = conv.get()
    if captured and parse_conversation_url(page.url) != captured:
        await safe_screenshot(page, run_dir / "error-conversation_drift.png")
        log_stage("error", reason="conversation_drift", expected=captured, actual=page.url)
        return err("conversation_drift", {"expected": captured, "actual": page.url})

    extraction = "innertext"
    response = last_text
    if completed:
        copied = await _copy_button_extract(page)
        if copied is not None:
            response = copied
            extraction = "copy_button"
    log_stage("extracted", method=extraction, chars=len(response))
    atomic_write(run_dir / "response.md", response)

    # Authoritative post-hoc audit: the served assistant turn stamps the model
    # that actually answered. A close during the audit raises RunPageClosed (not
    # a fail-open "unverified"), so we reopen and re-audit rather than return a
    # wrong-model answer as ok.
    served_slug = await served_assistant_model_slug(page)
    log_stage("served_model", slug=served_slug)

    menu_model = None
    if not served_slug:
        menu_model = await read_selected_model(page, timeout=10.0)
    model_audit = classify_served_audit(served_slug, menu_model)

    if model_audit in ("slug_mismatch", "menu_mismatch"):
        reason = "served_model_mismatch" if model_audit == "slug_mismatch" else "model_menu_mismatch"
        # Quarantine the artifact, not just the run: a rejected turn's text is
        # complete and plausible, differing from a verified answer only by
        # provenance, so leaving it at `response.md` — the name every consumer
        # treats as "the answer" — invites a caller to return it anyway.
        # Renaming makes that require deliberately naming a rejected file. Only
        # the two FATAL verdicts quarantine; the fail-open ones must keep
        # response.md or a selector rename bricks the tool.
        os.replace(run_dir / "response.md", run_dir / "response.rejected.md")
        await safe_screenshot(page, run_dir / f"error-{reason}.png")
        log_stage("error", reason=reason, slug=served_slug, menu_model=menu_model)
        return err(reason,
                   {"served_slug": served_slug, "menu_model": menu_model,
                    "completed": completed, "response_chars": len(response),
                    "rejected_response": str(run_dir / "response.rejected.md")})

    if completed and model_audit != "verified":
        log_stage("served_model_unverified", model_audit=model_audit)

    result = {
        "status": "ok" if completed else "timeout",
        "run_id": run_id,
        "url": page.url,
        "run_dir": str(run_dir),
        "response_chars": len(response),
        "extraction": extraction,
        "model_audit": model_audit,
        "exit_code": 0 if completed else 3,
    }
    log_stage("finished", status=result["status"])
    return result


async def _run_postsend(ctx, page, *, run_dir, run_id, deadline, send_ts, conv, network_log, err) -> tuple[dict, object]:
    """Drive `_monitor_and_finalize` with bounded page-close recovery.

    Returns `(result, latest_owned_page)`. The caller MUST close the returned
    page (not its original handle): recovery rebinds to fresh tabs, so the
    returned page is the only one still owned — closing it keeps the existing
    bounded `finally` leak-safe. Receives NO prompt/composer/send handle, so it
    is structurally incapable of re-pasting or re-sending; the original absolute
    `deadline` is threaded through unchanged (recovery never grants a fresh
    budget). Extracted from `_run_with_browser` so the recovery control flow —
    budget accounting, rebind, terminal reasons — is unit-testable with fakes.
    """
    loop = asyncio.get_running_loop()
    recoveries = 0

    def _timeout(conv_url):
        log_stage("completion_timeout", reason="deadline_during_recovery", conversation_url=conv_url)
        return {"status": "timeout", "run_id": run_id, "url": conv_url,
                "run_dir": str(run_dir), "reason": "deadline_during_recovery", "exit_code": 3}

    while True:
        try:
            result = await _monitor_and_finalize(
                page, run_dir=run_dir, run_id=run_id,
                deadline=deadline, send_ts=send_ts, conv=conv, err=err,
            )
            return result, page
        except RunPageClosed:
            # Reopen loop: re-classify after EACH reopen attempt without
            # re-entering the monitor on a tab already known dead (a
            # closed-during-nav reopen). The old `continue`-to-monitor path
            # spent an extra recovery slot on that no-op round-trip.
            while True:
                conv_url = conv.get()
                remaining = deadline - loop.time()
                decision = classify_recovery(conv_url, recoveries, MAX_PAGE_RECOVERIES, remaining)
                if decision == "no_url":
                    log_stage("error", reason="page_closed_before_conversation_url")
                    return err("page_closed_before_conversation_url"), page
                if decision == "deadline":
                    return _timeout(conv_url), page
                if decision == "exhausted":
                    log_stage("error", reason="page_recovery_exhausted", attempts=recoveries)
                    return err("page_recovery_exhausted",
                               {"conversation_url": conv_url, "attempts": recoveries}), page
                recoveries += 1
                log_stage("page_recovery_attempt", attempt=recoveries, conversation_url=conv_url)
                # A fresh tab in the SAME context proves the browser/CDP survived
                # (vs a full disconnect). Rebind `page` to the new tab BEFORE
                # navigating so the returned page is always the latest owned one.
                try:
                    page = await asyncio.wait_for(ctx.new_page(), timeout=min(30.0, remaining))
                except Exception as e:
                    log_stage("error", reason="browser_disconnected_after_send",
                              exception=f"{type(e).__name__}: {e}")
                    return err("browser_disconnected_after_send", {"conversation_url": conv_url}), page
                _attach_response_logger(page, network_log)
                nav_reason = await _recover_navigate(ctx, page, conv_url, deadline=deadline)
                if nav_reason == "closed":
                    # Reopened tab closed during nav — re-classify (inner loop),
                    # consuming exactly one slot, no dead-monitor round-trip.
                    log_stage("page_recovery_failed", attempt=recoveries, reason="closed_during_nav")
                    continue
                if nav_reason == "deadline":
                    return _timeout(conv_url), page
                if nav_reason is not None:
                    log_stage("page_recovery_failed", attempt=recoveries, reason=nav_reason)
                    return err("page_recovery_failed",
                               {"conversation_url": conv_url, "recovery_reason": nav_reason}), page
                log_stage("page_recovery_succeeded", attempt=recoveries)
                break  # reopened OK → outer loop re-runs the monitor on the new page


async def _run_with_browser(run_id, run_dir, prompt_text, network_log, err, slot_id) -> dict:
    def exc(e: Exception) -> dict:
        return err("worker_exception", {"exception": f"{type(e).__name__}: {e}"})

    try:
        ensure_shared_chrome_running(skip_slot_id=slot_id)
        async with async_playwright() as pw:
            ctx = await connect_shared_chrome(pw)
            page = await ctx.new_page()
            # Worker owns this Page only. Closing it on exit removes our tab from
            # the shared Chrome without affecting other workers' tabs. We do NOT
            # call browser.close() — that would CDP-disconnect the shared process
            # (and historically that has terminated Chrome). The Playwright
            # `async with` exit drops our connection without killing Chrome.
            #
            # We do NOT call bring_tab_to_front or bind_chrome_compositor_surface
            # here. Those run only inside UiClipboardLock (in _focus_and_paste
            # and _copy_button_extract). An early bring_to_front would hijack a
            # concurrent worker's mid-paste keystroke. Screenshots work on
            # background tabs in a windowed Chrome.
            try:
                _attach_response_logger(page, network_log)
                log_stage("chrome_connected")

                await _goto_with_retry(page, "https://chatgpt.com/")
                await pin_viewport_cdp(ctx, page)
                if not await wait_for_login(ctx, timeout=30.0):
                    await safe_screenshot(page, run_dir / "error-needs_reauth.png")
                    (run_dir / "error.html").write_text(await page.content())
                    log_stage("error", reason="needs_reauth")
                    return err("needs_reauth")
                log_stage("logged_in")

                ok, chip_text = await ensure_pro_chip(page, run_dir=run_dir)
                if not ok:
                    await safe_screenshot(page, run_dir / "error-model_select_failed.png")
                    (run_dir / "error.html").write_text(await page.content())
                    log_stage("error", reason="model_select_failed", chip_text=chip_text)
                    return err("model_select_failed", {"chip_text": chip_text})
                log_stage("model_verified", chip_text=chip_text)

                composer = page.get_by_role("textbox").first
                await _focus_and_paste(page, composer, prompt_text)
                log_stage("prompt_typed", chars=len(prompt_text))

                # Wait for any pasted-text-attachment upload to finish before
                # clicking Send. Prompts past ChatGPT's paste threshold get
                # auto-converted to a "Pasted text" attachment that uploads
                # asynchronously; the send button stays `disabled` until the
                # upload completes. Playwright's default 30s click-wait is
                # shorter than realistic uploads on a flaky link (observed
                # ~60s on 442KB prompts). Gate explicitly with a wider hard
                # ceiling so a stuck upload fails closed at a bounded deadline
                # instead of masquerading as a 30s click timeout. Outside
                # UiClipboardLock — sibling workers must stay free to paste.
                send_ready_selector = (
                    '[data-testid="send-button"]:not([disabled]):not([aria-disabled="true"]), '
                    'button[aria-label="Send prompt"]:not([disabled]):not([aria-disabled="true"]), '
                    'button[aria-label="Send message"]:not([disabled]):not([aria-disabled="true"])'
                )
                upload_wait_start = time.time()
                try:
                    await page.wait_for_selector(send_ready_selector, timeout=300_000, state="visible")
                finally:
                    upload_wait_elapsed = time.time() - upload_wait_start
                    if upload_wait_elapsed >= 2.0:
                        log_stage(
                            "paste_upload_wait",
                            chars=len(prompt_text),
                            elapsed_secs=round(upload_wait_elapsed, 1),
                            timeout_secs=300,
                        )

                await safe_screenshot(page, run_dir / "pre-send.png")

                # Re-verify the effort at the point of use — closes the
                # time-of-check/time-of-use gap. ensure_pro_chip ran ~2.6s ago,
                # right after page load; the chip can hydrate optimistically to
                # "Pro" and then re-resolve to the new conversation's default (a
                # lower effort tier) during the paste/upload window, sending at
                # the wrong effort while model_verified logged "Pro". This re-read
                # is a passive inner_text() (no UiClipboardLock, no menu, no
                # bring_to_front) so it can't hijack a sibling's paste. Fail
                # closed: never send at an effort we haven't verified. We do NOT
                # re-run the chip menu here — that needs the clipboard lock and a
                # fragile Radix dance with a loaded composer; surface the run_dir
                # instead. Nothing slow runs between this read and the click. The
                # model axis (invisible in the chip) is backstopped only by the
                # served-slug audit after completion.
                #
                # Uses the default stable read (not stable_polls=1): a chip
                # actively oscillating at Send time must fail closed, not be
                # accepted on a single lucky sample. The irreducible read→click
                # window is backstopped by the served-slug audit after completion.
                presend_chip = await read_composer_chip_text(page, timeout=10.0)
                if not is_pro_label(presend_chip):
                    await safe_screenshot(page, run_dir / "error-model_drift.png")
                    (run_dir / "error.html").write_text(await page.content())
                    log_stage("error", reason="model_drift_before_send",
                              verified=chip_text, presend=presend_chip)
                    return err("model_drift_before_send",
                               {"verified": chip_text, "presend": presend_chip})
                log_stage("model_reverified", chip_text=presend_chip)

                send_btn = page.locator(
                    '[data-testid="send-button"], button[aria-label="Send prompt"], button[aria-label="Send message"]'
                ).first

                # Register the conversation-URL observer BEFORE the click so a fast
                # /  ->  /c/<id> pushState transition cannot be missed. The handler
                # is synchronous and memory-only (idempotent); disk persistence
                # happens from the monitor loop's async flow. Bound to THIS page via
                # a default arg so a later `page` rebind (recovery) can't misroute it.
                conv = _ConversationUrl()

                def _on_frame_navigated(frame, _observed=page):
                    try:
                        if frame is _observed.main_frame:
                            conv.capture(frame.url)
                    except Exception:
                        pass

                page.on("framenavigated", _on_frame_navigated)

                # ---- Send: the single irreversible step. The absolute deadline is
                # created BEFORE the click (monotonic clock) so an ambiguous close —
                # the click can dispatch the request and THEN raise on tab-close —
                # still carries a budget. Nothing past this point pastes or sends
                # again: the post-send helper receives no prompt/composer/send handle.
                loop = asyncio.get_running_loop()
                send_ts = loop.time()
                deadline = send_ts + DEFAULT_GENERATION_TIMEOUT
                try:
                    await send_btn.click()
                except Exception as e:
                    # "The click raised" is NOT proof the send didn't happen. If a
                    # valid conversation URL was already captured, the message is in
                    # flight — recover it (never resubmit). Otherwise fail closed as
                    # send_outcome_unknown; never guess a conversation or resend.
                    conv.capture(page.url)
                    if page.is_closed() and conv.get():
                        conv.persist(run_dir)
                        log_stage("send_click_raised_after_capture",
                                  exception=f"{type(e).__name__}: {e}")
                    elif page.is_closed():
                        log_stage("error", reason="send_outcome_unknown",
                                  exception=f"{type(e).__name__}: {e}")
                        return err("send_outcome_unknown", {"exception": f"{type(e).__name__}: {e}"})
                    else:
                        raise
                else:
                    log_stage("sent")
                    conv.capture(page.url)
                    conv.persist(run_dir)

                # ---- Post-send monitor + finalize, with bounded page-close
                # recovery. On a detected tab close, reopen the SAME captured
                # conversation on a fresh tab and re-run finalization; the original
                # deadline is preserved across every attempt (recovery never grants
                # a fresh budget). `_run_postsend` returns the LATEST owned page so
                # the `finally` below closes the current tab, not a stale handle.
                result, page = await _run_postsend(
                    ctx, page, run_dir=run_dir, run_id=run_id,
                    deadline=deadline, send_ts=send_ts, conv=conv,
                    network_log=network_log, err=err,
                )
                return result
            except Exception as e:
                log_stage("error", reason="worker_exception", exception=f"{type(e).__name__}: {e}")
                try:
                    await page.screenshot(path=str(run_dir / "error-worker_exception.png"), full_page=True)
                    (run_dir / "error.html").write_text(await page.content())
                except Exception:
                    pass
                return exc(e)
            finally:
                # Bounded close: a hung CDP session must not hold the
                # ParallelSlot indefinitely. Playwright's `async with` exit
                # drops the connection regardless.
                try:
                    await asyncio.wait_for(page.close(), timeout=5.0)
                except asyncio.TimeoutError:
                    log_stage("page_close_timeout")
                except Exception as e:
                    log_stage("page_close_skipped", exception=f"{type(e).__name__}: {e}")
    except Exception as e:
        log_stage("error", reason="worker_exception", exception=f"{type(e).__name__}: {e}")
        return exc(e)
    finally:
        try:
            (run_dir / "network.json").write_text(json.dumps(network_log, indent=2))
        except Exception:
            pass


async def cmd_run(args) -> int:
    validate_run_id(args.run_id)
    run_dir = RUNS / args.run_id
    prompt_path = run_dir / "prompt.md"
    if not run_dir.exists() or not prompt_path.exists():
        result = {
            "status": "error",
            "reason": "missing_prompt",
            "run_id": args.run_id,
            "run_dir": str(run_dir),
            "exit_code": 1,
        }
        if run_dir.exists():
            atomic_write(run_dir / "result.json", json.dumps(result))
        return 1

    prompt_text = prompt_path.read_text()
    result = await _browser_run(args.run_id, run_dir, prompt_text)
    atomic_write(run_dir / "result.json", json.dumps(result))
    return result.get("exit_code", 1)


# ---- close-chrome ----

def cmd_close_chrome(force: bool = False) -> int:
    """Tear down the shared gpt-pro Chrome process. Held under LaunchLock.

    Refuses by default when any worker holds a ParallelSlot — killing Chrome
    out from under live tabs costs in-flight Pro runs (5–20 min each).
    Pass --force to bypass.
    """
    with LaunchLock():
        if not force and _slots_held():
            stderr_jsonl({
                "status": "error",
                "reason": "workers_in_flight",
                "hint": "Wait for active runs to finish, or pass --force.",
            })
            return 1
        _kill_chrome_orphans()
    return 0


# ---- main ----

def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Open Chrome on chatgpt.com to sign in. Cookies persist for `ask`.")
    sub.add_parser("doctor", help="Verify the profile is logged in. Prints JSON; saves screenshot + HTML.")
    close_p = sub.add_parser("close-chrome", help="Tear down the shared gpt-pro Chrome. Refuses if workers are in flight.")
    close_p.add_argument("--force", action="store_true",
                         help="Kill Chrome even if workers hold ParallelSlots. In-flight runs will lose their CDP connection.")

    ask_p = sub.add_parser("ask", help="Send a prompt from stdin to ChatGPT GPT-5.6 Sol Pro. Prints response on stdout when ready.")
    ask_p.add_argument("--run-id", default=None,
                      help="Caller-supplied run id. Same id + same prompt attaches to an in-progress run.")
    ask_p.add_argument("--generation-timeout", type=float, default=DEFAULT_GENERATION_TIMEOUT,
                      help="Max seconds the parent will wait for completion (default 3600).")
    ask_p.add_argument("--output", type=Path, default=None,
                      help="Write response to this file (on macmini) instead of stdout. Stderr JSONL is unchanged. Ignored with --no-wait.")
    ask_p.add_argument("--no-wait", action="store_true",
                      help="Submit (or attach to) the run and exit 0 immediately after `submitted`. Use `fetch` to retrieve the response. Designed for short-session SSH polling — see SKILL.md.")

    fetch_p = sub.add_parser("fetch", help="Fetch the response of an existing run by id. Waits if still running.")
    fetch_p.add_argument("run_id")
    fetch_p.add_argument("--timeout", type=float, default=None,
                        help="Max seconds to wait. Default infinite. 0 = non-blocking check.")
    fetch_p.add_argument("--poll-interval", type=float, default=0.5)
    fetch_p.add_argument("--output", type=Path, default=None,
                        help="Write response to this file (on macmini) instead of stdout. Stderr JSONL is unchanged.")

    run_p = sub.add_parser("_run", help=argparse.SUPPRESS)
    run_p.add_argument("run_id")

    args = p.parse_args()
    if args.cmd == "login":
        return asyncio.run(cmd_login())
    if args.cmd == "doctor":
        return asyncio.run(cmd_doctor())
    if args.cmd == "ask":
        return asyncio.run(cmd_ask(args))
    if args.cmd == "fetch":
        return asyncio.run(cmd_fetch(args))
    if args.cmd == "_run":
        return asyncio.run(cmd_run(args))
    if args.cmd == "close-chrome":
        return cmd_close_chrome(force=args.force)
    return 1


if __name__ == "__main__":
    sys.exit(main())
