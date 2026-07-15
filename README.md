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
- Python 3.11+, [uv](https://docs.astral.sh/uv/), and Google Chrome (real Chrome, not bundled Chromium — auth and anti-abuse behave differently).
- A ChatGPT Pro account.

```bash
uv sync
uv run gpt-pro-relay login    # opens Chrome; sign in to ChatGPT manually
```

Login uses a dedicated profile at `~/.gpt-pro-profile/`. Cookies persist there. Manually select **GPT-5.6 Sol** + **Pro** once so the account preference is set.

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
| `gpt-pro-relay login` | Open Chrome at chatgpt.com using the dedicated profile. Auto-detects login (session cookie) and exits. |
| `gpt-pro-relay doctor` | Verify the profile is logged in and that the composer is set to **GPT-5.6 Sol** + **Pro** effort (read-only; no prompt sent). Exits non-zero on a confirmed wrong model. Saves screenshot + HTML to `~/.gpt-pro/runs/`. Prints JSON status. |
| `gpt-pro-relay ask [--run-id ID] [--no-wait] [--output PATH]` | Read prompt from stdin. Spawns a detached worker. Default: waits for completion, prints response on stdout. `--no-wait`: exits 0 right after submission (use `fetch` to retrieve). Same `--run-id` + same prompt re-attaches to an in-progress run (idempotent). `--output` writes to a file instead of stdout. |
| `gpt-pro-relay fetch <run-id> [--output PATH]` | Read the result of an existing run. Waits if still running. `--timeout 0` for non-blocking check, `--timeout 60` to bound a single poll. `--output` writes to a file instead of stdout. |
| `gpt-pro-relay close-chrome [--force]` | Tear down the shared gpt-pro Chrome process. Refuses by default if any worker holds a `ParallelSlot`; pass `--force` to kill anyway (in-flight runs lose their CDP connection). |

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
deadline=$((SECONDS + 3600)); delay=5
while (( SECONDS < deadline )); do
  out=$(ssh "${SSH_OPTS[@]}" mac gpt-pro-relay fetch "$RUN_ID" --timeout 60 2>/tmp/gpt-pro-$RUN_ID.err); rc=$?
  case $rc in
    0)   printf '%s' "$out"; exit 0 ;;
    124) delay=5; continue ;;
    255) sleep "$delay"; (( delay < 30 )) && delay=$((delay * 2)) ;;
    *)   cat /tmp/gpt-pro-$RUN_ID.err >&2; exit "$rc" ;;
  esac
done
echo "gpt-pro-relay overall timeout for $RUN_ID" >&2; exit 124
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
| 3 | `status: timeout` (browser worker didn't finish within 60 min) |
| 4 | run_dir not found (fetch only) |
| 124 | wait timed out, run still pending |

## Artifacts

Each run writes to `~/.gpt-pro/runs/<run_id>/`:

- `prompt.md` — input
- `meta.json` — `{run_id, created_at, prompt_sha256}`
- `response.md` — extracted assistant message (atomic). `result.json` reports `extraction: "copy_button"` or `"innertext"`.
- `result.json` — terminal status (atomic)
- `conversation.json` — `{url, captured_at}`, the ChatGPT conversation this run submitted to. Diagnostic breadcrumb for manual recovery; written once the URL is known.
- `pre-send.png`, `streaming-NNN.png`, `final.png`, `error-*.png`
- `final.html` — last DOM snapshot
- `network.json` — captured `/backend-api/*` calls
- `worker.stdout`, `worker.stderr` — detached worker's output

## Concurrency

Up to `GPT_PRO_MAX_PARALLEL` (default 6) `ask` invocations run in parallel — each gets its own tab in a shared Chrome process. Beyond that they queue on a file-lock semaphore (`~/.gpt-pro/slots/`). Set `GPT_PRO_MAX_PARALLEL=10` for the personal-use ceiling; lower it to `1` if ChatGPT account-side anti-abuse starts flagging parallel bursts (symptom: unexplained `needs_reauth`, captcha redirects, or 429s in `network.json`). Chrome stays alive between runs; `gpt-pro-relay close-chrome` tears it down when no workers are in flight.

## Closing the Chrome tab mid-run

If you accidentally close a worker's Chrome tab/window while it's generating, the worker reopens the **same** conversation on a fresh tab and resumes monitoring — it never re-pastes or re-sends (a resend would burn another 5–20 min of Pro reasoning). Watch for `conversation_url_captured`, then `page_recovery_attempt` → `page_recovery_succeeded` in `worker.stderr`. Recovery is bounded (3 reopens) and runs entirely inside the **original** 60-min generation budget — it never grants a fresh deadline.

Terminal (not auto-recovered) cases, all fail closed with a specific `reason`:
- **Closing before the conversation URL is captured** (`page_closed_before_conversation_url` / `send_outcome_unknown`) — the send may be in flight server-side, but with no captured URL the worker refuses to guess a conversation or resubmit.
- **Quitting/killing Chrome or a full CDP disconnect** (`browser_disconnected_after_send`) — recovery reopens a tab in the *surviving* context only; it does not relaunch Chrome under sibling workers.
- **A recovery navigation that redirects to login/home/another conversation, drops auth, or never renders the conversation** (`page_recovery_failed` with a `recovery_reason`).
- **A reopened tab that is then navigated to a different conversation** (e.g. a human grabs the background tab) — `conversation_drift`; the worker refuses to extract/return another conversation's answer.
- **Repeated closes exhausting the budget** (`page_recovery_exhausted`), or the deadline expiring mid-recovery (`status: timeout`).

Whether ChatGPT resumes *live* streaming on reopen (vs. only showing the finished turn) is server-side behavior; either way the Copy-button completion gate and served-model audit still apply, so the worst case is a clean timeout, never a wrong or partial answer.

## Known limitations
- Markdown extraction uses the page's Copy button (clean LaTeX, code fences, tables); falls back to `innerText` if the Copy button isn't reachable or `pbpaste` isn't available (non-macOS).
- Completion detection is heuristic (text-stable + no Stop button), not the `/backend-api/conversation/<id>/async-status` endpoint. The async-status endpoint only fires once at the end and our heuristic catches the same moment — not worth wiring.
- If the SSH-side parent dies before reading stdin and spawning the worker, no run is created — `fetch` returns `not_found`. That's by design.

## Claude Code skill

No skill ships with this repo. For the SSH-relay flow (Claude Code on a different machine than ChatGPT), the polling pattern in [Usage over SSH](#usage-over-ssh) is the contract — wrap it in your own [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) if you want trigger-phrase activation.
