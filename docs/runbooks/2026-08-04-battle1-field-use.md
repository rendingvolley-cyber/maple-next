# Battle 1 field-use runbook (2026-08-04)

Prerequisites: Windows, Python 3.11, official local checkout at `C:\work\maple-next`.

## First-time install

```powershell
cd C:\work\maple-next
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Start the app

```powershell
scripts\start_maple_next.cmd
```

This resolves Python (`.venv\Scripts\python.exe` first, then `py -3.11`), resolves the
runtime root, and launches `python -m maple_next --database ... --export-directory ...`.
It never reads, writes, or prints `.env`.

## Runtime locations (repository-external, fixed)

| Item | Path |
| --- | --- |
| Runtime root | `%LOCALAPPDATA%\MapleNext\Battle1` (or `%USERPROFILE%\.maple-next\Battle1` if `LOCALAPPDATA` is unset) |
| Database | `<runtime root>\state\maple-next.db` |
| Match export JSON | `<runtime root>\exports\` |
| Logs | `<runtime root>\logs\maple-next-YYYYMMDD-HHMMSS.log` |
| Smoke summary | `<runtime root>\smoke\latest.json` |

To pin a different runtime root: `scripts\start_maple_next.cmd --runtime-root "D:\Maple Runtime"`
or set `MAPLE_NEXT_RUNTIME_ROOT` before launching.

### Runtime root restrictions

- The runtime root can never be the repository checkout itself, or any directory
  inside it -- this is checked (fail-closed, non-zero exit) before the launcher
  creates any directory, log file, database, or export, whether the value comes
  from `--runtime-root`, `MAPLE_NEXT_RUNTIME_ROOT`, or the `%LOCALAPPDATA%`/`%USERPROFILE%`
  fallbacks.
- A sibling directory of the repository (e.g. `C:\work\maple-next-runtime` next to
  `C:\work\maple-next`) is allowed.
- Always quote paths containing spaces, e.g. `--runtime-root "D:\Maple Runtime"`.

## UGREEN capture

- **Connected**: normal startup continues as usual.
- **Not connected / capture unavailable**: this is not a startup failure. The launcher
  logs `manual-safe startup status: ENABLED` and the app continues in manual-safe mode —
  enter board facts by hand and keep playing. Do not wait for UGREEN before continuing a match.
- **Do not use OBS.** It is not part of this app's capture path.

## Gemini model routing

Production defaults are lane-specific and exact:

| Lane | Primary | Eligible fallback |
| --- | --- | --- |
| Selection | `gemini-3.6-flash` | `gemini-3.5-flash` |
| Turn Advice | `gemini-3.5-flash-lite` | none |

Selection falls back at most once, within the same trusted human activation, and only after
a positively classified rate-limit, quota, or model-capacity exhaustion response. It never
falls back for authentication, malformed request/response, schema or binding rejection,
network failure, timeout, stale result, or a generic provider error. Both provider attempts
are recorded in the local durable audit with ordinal, model, outcome, and sanitized reason.

Lane-specific overrides, if field authorization requires them, are:

- `MAPLE_NEXT_GEMINI_SELECTION_PRIMARY_MODEL`
- `MAPLE_NEXT_GEMINI_SELECTION_FALLBACK_MODEL`
- `MAPLE_NEXT_GEMINI_TURN_MODEL`

`MAPLE_NEXT_GEMINI_MODEL` is intentionally ignored and cannot silently route both lanes.
All three production defaults use Gemini 3.x-compatible request bodies: strict JSON schema
output without `temperature`, `top_p`, `top_k`, `candidate_count`, or prefilled model turns.

## Gemini Turn Advice authorization

- The production Turn provider is fail-closed by default. An API key by itself does not
  authorize or enable a Turn request.
- Before the exact authorization is recorded on Issue #31, keep
  `MAPLE_NEXT_GEMINI_TURN_AUTHORIZED` unset. The app will report
  `GEMINI_TURN_NOT_AUTHORIZED` and perform zero provider/network sends.
- After the exact authorization is recorded, configure the Gemini credentials through the
  approved runtime secret mechanism and set `MAPLE_NEXT_GEMINI_TURN_AUTHORIZED=1` only in
  the launcher process environment. Never paste the API key into this runbook, a command
  transcript, a screenshot, or a GitHub comment.
- Even when authorized, only a trusted human activation of **SEND TURN TO GEMINI** sends one
  request to `gemini-3.5-flash-lite` (or its lane-specific override). There is no retry,
  resend, fallback model, or automatic game action.

## Ending the app

- Normal exit: close the app window, or `Ctrl+C` in the launcher console. The launcher
  waits for the app process and propagates its exit code; it does not kill anything else.
- After an abnormal exit (crash, forced shutdown): just re-run `scripts\start_maple_next.cmd`.
  The database and any unexported match state are restored automatically on startup via the
  existing restart-recovery path — no manual repair step.

## Confirming the end-of-match JSON

1. In the app, use **対戦終了・JSON出力** (end match → save match JSON) once the match has ended.
2. Confirm a file named `maple-match-<match id>.json` appears under `<runtime root>\exports\`.
3. If the button/flow does not produce a file, check the latest log at
   `<runtime root>\logs\` for the reported error before retrying — do not delete or edit the
   database by hand.

## Re-running the field-use smoke check

```powershell
scripts\start_maple_next.cmd --smoke
```

This runs `scripts\field_use_smoke.py` against an isolated, repository-external temp
runtime (never the runtime above) and writes a summary to `<runtime root>\smoke\latest.json`.
It exercises startup, UGREEN-unavailable continuation, restart/reload, and match-JSON export
using mock adapters only — no real Gemini call, no network send, no automatic game action.

## Rules for this deadline

- Never place runtime state (database, exports, logs) inside the repository checkout.
- Gemini Turn Advice is triggered only by an explicit human action in the app — never
  automatically on window open, refresh, poll, or timer.
- No automatic MOVE / SWITCH / NEXT TURN / WIN / LOSE. All in-battle actions and the final
  outcome are entered and confirmed by the operator.
- Do not put API keys or `.env` contents in chat, logs, screenshots, or this runbook.
