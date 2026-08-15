# gpt-pro-relay

Relay prompts to your logged-in ChatGPT Pro session from anywhere with SSH. Your always-on Mac drives a real Chrome via Playwright; remote agents and other machines invoke it as a CLI:

```bash
RUN_ID=$(uuidgen)
echo "your prompt" | ssh mac gpt-pro-relay ask --run-id "$RUN_ID"
```

`uv sync` installs `gpt-pro-relay` into the project's venv.

> Browser automation against ChatGPT violates OpenAI's ToS. Account-ban risk is yours. Don't build a product on it.

## How it works

```
remote ──ssh──▶ Mac ──[parent: ask]
                       │
                       │ writes prompt.md, meta.json
                       │ spawns detached worker (start_new_session)
                       ▼
                      [worker: _run] ──Playwright──▶ Chrome (persistent profile)
                       │                                          │
                       │ <── poll result.json ──┐                 ▼
                       │                        │      chatgpt.com / GPT-5.6 Sol / Pro
                       ▼                        │                 │
                  response on stdout            └─── result.json ◀┘
                  JSON status on stderr
```

No daemon. No HTTP server. No queue. SSH is the transport. The worker is detached from the SSH session, so a mid-run drop doesn't kill it — `gpt-pro-relay fetch <run_id>` recovers the response.

## Setup

Requires:

- A Mac that stays logged into its GUI session. Playwright drives real Chrome and needs WindowServer access, so a headless box won't work — leave the Mac signed in (and use `caffeinate` if it sleeps).
- Python 3.11+, [uv](https://docs.astral.sh/uv/), and the side-by-side Google Chrome Beta app. The relay needs real Chrome (not bundled Chromium), while Beta's distinct macOS bundle identity keeps it from intercepting Stable Chrome's Dock and update lifecycle.
- A ChatGPT Pro account.

```bash
brew install --cask google-chrome@beta
open -a "Google Chrome Beta"   # once, on the Mac's own screen; approve the first-open prompt, then quit
uv sync
uv run gpt-pro-relay login     # opens Chrome Beta; sign in to ChatGPT manually
```

**Launch Chrome Beta once interactively before the relay ever touches it.** macOS asks for approval the first time you open freshly downloaded software, and that prompt appears on the Mac's own screen — an SSH-detached worker cannot answer it. Get it out of the way while a human is present. See [Troubleshooting](#a-newly-installed-chrome-never-binds-the-cdp-port) if the relay hangs on a new Chrome anyway.

Login uses a dedicated profile at `~/.gpt-pro-profile/`. Cookies persist there. Manually select **GPT-5.6 Sol** + **Pro** once so the account preference is set.

The default app is `/Applications/Google Chrome Beta.app`. `GPT_PRO_CHROME_APP` may select a Chrome Dev or Canary app with a recognized side-by-side bundle identity, but the relay rejects Stable Chrome's `com.google.Chrome` bundle identity instead of silently recreating the Dock conflict. Existing installations that previously used Stable should migrate while no workers are running:

Migration keeps the existing `~/.gpt-pro-profile/`. Beta is a newer Chrome than Stable, so its **first launch upgrades the profile schema one-way** — back the profile up before that launch, not after, and only while Chrome is fully closed:

```bash
gpt-pro-relay close-chrome
cp -a ~/.gpt-pro-profile ~/.gpt-pro-profile.bak       # whole profile; do this while Chrome is closed
brew install --cask google-chrome@beta
open -a "Google Chrome Beta"                          # approve the first-open prompt, then quit
uv run gpt-pro-relay login
uv run gpt-pro-relay doctor
```

The ChatGPT session carried over without re-authentication in the 2026-08-06 migration, so `login` may just detect the cookie and exit — but treat that as luck, not contract, and be ready to sign in again.

### Optional: bare command on PATH

For SSH callers to use `gpt-pro-relay` without the full venv path, symlink it into a directory that's on your non-interactive shell `PATH`. On zsh, `~/.local/bin/` works if you export it in `~/.zshenv` (which zsh sources for SSH sessions, unlike `~/.zshrc`):

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/gpt-pro-relay" ~/.local/bin/gpt-pro-relay
```

After that, `ssh mac gpt-pro-relay ask ...` resolves without the absolute path. Skip if you'd rather hardcode the full venv path in your callers.

## Commands

| Command | What it does |
|---|---|
| `gpt-pro-relay login` | Open the isolated Chrome Beta app at chatgpt.com using the dedicated profile. Auto-detects login (session cookie) and exits. |
| `gpt-pro-relay doctor` | Verify the profile is logged in and that the composer is set to **GPT-5.6 Sol** + **Pro** effort (read-only; no prompt sent). Exits non-zero on a confirmed wrong model. Saves screenshot + HTML to `~/.gpt-pro/runs/`. Prints JSON status. |
| `gpt-pro-relay ask [--run-id ID] [--no-wait] [--generation-timeout SECONDS] [--output PATH]` | Read prompt from stdin. Spawns a detached worker. Default: waits indefinitely for completion and prints the response on stdout. `--generation-timeout` optionally bounds only this parent wait; it never stops the detached worker. `--no-wait`: exits 0 right after submission (use `fetch` to retrieve). Same `--run-id` + same prompt re-attaches to an in-progress run (idempotent). `--output` writes to a file instead of stdout. |
| `gpt-pro-relay fetch <run-id> [--output PATH]` | Read the result of an existing run. Waits if still running. `--timeout 0` for non-blocking check, `--timeout 60` to bound a single poll. `--output` writes to a file instead of stdout. |
| `gpt-pro-relay close-chrome [--force]` | Tear down the shared gpt-pro Chrome process. Refuses while a worker, `login`, or `doctor` holds a browser activity lease; pass `--force` to kill anyway (active users lose their CDP connection). |

## Usage

The CLI is the same whether you're calling it locally or relaying over SSH. Pick whichever matches your setup.

### Local

Same machine running ChatGPT and the caller — no transport, no wrapper:

```bash
RUN_ID=$(uuidgen)
echo "your prompt" | gpt-pro-relay ask --run-id "$RUN_ID"
```

If `gpt-pro-relay` isn't on `PATH`, prefix with `uv run --project /path/to/repo` or call the venv binary directly. Up to `GPT_PRO_MAX_PARALLEL` (default 6) concurrent runs share one Chrome process; beyond that they queue on a file-lock semaphore in `~/.gpt-pro/slots/`.

### Remote (SSH)

**Recommended: short-session polling.** Holding one SSH connection idle for the full 5–20 min reasoning window is brittle — NAT/firewall idle-drops mid-run are routine. Submit with `--no-wait`, then poll `fetch` with bounded timeouts:

```bash
SSH_OPTS=(-S none -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
RUN_ID="ask-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen | tr '[:upper:]' '[:lower:]')"

# Phase 1: submit (≤1s SSH session, idempotent on same run_id + same prompt)
ssh "${SSH_OPTS[@]}" mac gpt-pro-relay ask --run-id "$RUN_ID" --no-wait <<'PROMPT'
your prompt here
PROMPT

# Phase 2: poll (each SSH session ≤60s, exponential backoff on transport drop)
delay=5
while :; do
  out=$(ssh "${SSH_OPTS[@]}" mac gpt-pro-relay fetch "$RUN_ID" --timeout 60 2>/tmp/gpt-pro-$RUN_ID.err); rc=$?
  case $rc in
    0)   printf '%s' "$out"; exit 0 ;;
    124) delay=5; continue ;;
    255) sleep "$delay"; (( delay < 30 )) && delay=$((delay * 2)) ;;
    *)   cat /tmp/gpt-pro-$RUN_ID.err >&2; exit "$rc" ;;
  esac
done
```

The SSH options matter: `-S none` avoids ControlMaster reuse (which can resurrect stale paths), `BatchMode=yes` prevents password-prompt hangs, `ConnectTimeout=15` + `ServerAliveInterval=15`/`CountMax=4` cap a dead session at ~60s instead of 5 min. The Phase 1 submit is idempotent — same `--run-id` + same prompt bytes attaches to an existing run, so a transport-flake retry is safe.

**Blocking single-call (stable links only):**

```bash
ssh "${SSH_OPTS[@]}" mac gpt-pro-relay ask --run-id "$RUN_ID" <<<prompt
```

If the SSH session drops mid-run, **never re-run `ask`** — that would submit a fresh prompt and burn another 5–20 min of Pro reasoning. Recover with `gpt-pro-relay fetch "$RUN_ID"` (or just enter the polling loop above).

### Stdio contract (both modes)

`stdout` is the response. `stderr` is newline-delimited JSON: a `submitted` line when the run starts, then a terminal `ok`/`error`/`timeout` line.

Pass `--output PATH` to write the response to a file on the gpt-pro host instead. stdout stays empty; the terminal stderr line gains an `"output": "<resolved-path>"` field. Useful when the caller would rather `Read` a file than capture potentially-large stdout.

Exit codes:

| code | meaning |
|---|---|
| 0 | `status: ok`, response on stdout (or `ask --no-wait` submitted; nothing on stdout) |
| 1 | `status: error`, see `reason` field |
| 2 | usage error (empty prompt, prompt_too_large, run_id_conflict, invalid run_id) |
| 3 | `status: timeout` from a legacy worker created before the generation cap was removed |
| 4 | run_dir not found (fetch only) |
| 124 | wait timed out, run still pending |

## Artifacts

Each run writes to `~/.gpt-pro/runs/<run_id>/`:

- `prompt.md` — input
- `meta.json` — `{run_id, created_at, prompt_sha256}`
- `response.md` — the answer: a completed turn the model audit did not reject. `result.json` reports `extraction: "copy_button"` or `"innertext"`, and `model_audit` — which is `verified` when the served model was confirmed, or a fail-open value (`unverified_missing_slug`, `model_ok_slug_missing`) when a selector break left the model unconfirmed and the run was allowed through anyway. Check it if provenance matters to you.
- `result.json` — terminal status (atomic). **It is the authority: only `status: "ok"` makes `response.md` usable.**
- `response.rejected.md` / `response.incomplete.md` / `response.partial.md` / `response.pending.md` — the extracted text under the name its outcome earned: the served model failed the audit, an attachment-only prompt was acknowledged instead of executed, the turn never passed the completion gate, or the run died before either was decided. Diagnostics, never answers. Don't judge by reading them — a rejected or incomplete turn can be fluent and plausible, and a turn that missed the completion gate can be fully rendered.

A run leaves **at most one** of those five names (a failure before extraction publishes none), so the name tells you what you have without opening `result.json`.
- `conversation.json` — `{url, captured_at}`, the ChatGPT conversation this run submitted to. Diagnostic breadcrumb for manual recovery; written once the URL is known.
- `pre-send.png`, `streaming-NNN.png`, `final.png`, `error-*.png`
- `final.html` — last DOM snapshot
- `network.json` — captured `/backend-api/*` calls
- `worker.stdout`, `worker.stderr` — detached worker's output

## Concurrency

Up to `GPT_PRO_MAX_PARALLEL` (default 6) `ask` invocations run in parallel — each gets its own tab in a shared Chrome Beta process. Beyond that they queue on a file-lock semaphore (`~/.gpt-pro/slots/`). Set `GPT_PRO_MAX_PARALLEL=10` for the personal-use ceiling; lower it to `1` if ChatGPT account-side anti-abuse starts flagging parallel bursts (symptom: unexplained `needs_reauth`, captcha redirects, or 429s in `network.json`). The isolated browser stays alive between runs. Workers, `login`, and `doctor` hold shared browser activity leases; normal `close-chrome` must acquire the exclusive lease, so it cannot race a new browser user or terminate an active one.

Because Chrome Beta stays alive indefinitely, restart it periodically after its updater downloads a security update: wait for active browser users to finish, run `gpt-pro-relay close-chrome`, then run `gpt-pro-relay doctor` to relaunch and verify it.

## Closing the Chrome tab mid-run

If you accidentally close a worker's Chrome tab/window while it's generating, the worker reopens the **same** conversation on a fresh tab and resumes monitoring — it never re-pastes or re-sends (a resend would burn another 5–20 min of Pro reasoning). Watch for `conversation_url_captured`, then `page_recovery_attempt` → `page_recovery_succeeded` in `worker.stderr`. Recovery is bounded to 3 reopens, and each navigation/check has its own finite timeout; there is no overall generation deadline.

Terminal (not auto-recovered) cases, all fail closed with a specific `reason`:
- **Closing before the conversation URL is captured** (`page_closed_before_conversation_url` / `send_outcome_unknown`) — the send may be in flight server-side, but with no captured URL the worker refuses to guess a conversation or resubmit.
- **Quitting/killing Chrome or a full CDP disconnect** (`browser_disconnected_after_send`) — recovery reopens a tab in the *surviving* context only; it does not relaunch Chrome under sibling workers.
- **A recovery navigation that redirects to login/home/another conversation, drops auth, or never renders the conversation** (`page_recovery_failed` with a `recovery_reason`).
- **A reopened tab that is then navigated to a different conversation** (e.g. a human grabs the background tab) — `conversation_drift`; the worker refuses to extract/return another conversation's answer.
- **Repeated closes exhausting the recovery count** (`page_recovery_exhausted`).

Whether ChatGPT resumes *live* streaming on reopen (vs. only showing the finished turn) is server-side behavior; either way the Copy-button completion gate and served-model audit still apply, so the relay never returns an unverified partial as an answer. With no overall generation deadline, a frozen recovered turn must be interrupted explicitly with `gpt-pro-relay stop <run-id>`.

## Troubleshooting

### A newly installed Chrome never binds the CDP port

Symptom: `Chrome CDP not ready on port 19222 after 30s`, on a Chrome app the relay has never successfully launched before. The Chrome process **exists** with the correct argv but never listens.

Confirm it is a pre-execution hold rather than a Chrome problem — all of these were measured on 2026-08-06:

```bash
ps -o pid,stat,rss,etime -p <pid>     # alive, ~32K RSS, 0% CPU, not progressing
sample <pid> 2                        # one frame: _dyld_start
vmmap <pid> | head                    # only the main executable and dyld mapped — no libraries
# cheap discriminator: another binary in the same bundle hangs identically
"/Applications/Google Chrome Beta.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/<ver>/Helpers/chrome_crashpad_handler" --help
```

Blocked before linking, and bundle-wide rather than Chrome-specific, means macOS is refusing to execute the bundle — most likely the first-open approval macOS requires for downloaded software, which only a human at the Mac can grant. Two signals look reassuring and are not evidence against this: `spctl -a` reports `accepted` and `codesign -v` reports `valid on disk`. Both speak to signing policy, not to pending first-open consent. The unified log is silent.

Fix: open the app once from the Mac's own screen and approve the prompt (or **Open Anyway** in System Settings → Privacy & Security), then relaunch the relay.

What is *not* established: in the one observed incident, `xattr -dr com.apple.quarantine` alone did **not** release the process, while a broader `xattr -cr` (which also removed `com.apple.FinderInfo`) coincided with success — but a human approval click landed in the same window, so the two were never isolated. Do not treat `xattr -cr` as the known remedy. It clears *every* attribute on *every* bundle member, which is a wider security bypass than the problem calls for. Prefer the interactive approval; reach for attribute clearing only as a deliberate, trusted-source last resort.

## Known limitations
- ChatGPT converts large native pastes into a `Pasted markdown` attachment at a frontend-controlled threshold. The relay detects the resulting empty composer rather than hard-coding that threshold, inserts and verifies a short top-level execution instruction before Send, and fails pre-send with `instruction_boundary_lost_before_send` if it cannot prove that boundary. If the backend nevertheless only acknowledges the file and offers to continue, the run fails with `instruction_boundary_lost` and publishes the diagnostic body as `response.incomplete.md`.
- Markdown extraction uses the page's Copy button (clean LaTeX, code fences, tables); falls back to `innerText` if the Copy button isn't reachable or `pbpaste` isn't available (non-macOS).
- Completion detection is heuristic (text-stable + no Stop button), not the `/backend-api/conversation/<id>/async-status` endpoint. The async-status endpoint only fires once at the end and our heuristic catches the same moment — not worth wiring.
- If the SSH-side parent dies before reading stdin and spawning the worker, no run is created — `fetch` returns `not_found`. That's by design.

## Claude Code skill

No skill ships with this repo. For the SSH-relay flow (Claude Code on a different machine than ChatGPT), the polling pattern in [Usage over SSH](#usage-over-ssh) is the contract — wrap it in your own [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) if you want trigger-phrase activation.
