# CLI and output reference

## Contents

- [xianyu.py](#xianyupy)
- [doctor.py](#doctorpy)
- [spider.py](#spiderpy)
- [task_manager.py](#task_managerpy)
- [monitor.py](#monitorpy)
- [cdp_profile.py](#cdp_profilepy)
- [create_state.py](#create_statepy)
- [login_state.py](#login_statepy)
- [install_skill.py](#install_skillpy)
- [Exit codes](#exit-codes)

All public entrypoints emit CLI parsing failures as one stdout JSON
object with `"ok": false`, `"error_type": "ArgumentError"`, and exit `2`.
`SIGTERM` enters the same controlled cancellation/cleanup contract as an
interactive cancellation and exits `130` with final JSON evidence.

Raw external TCP CDP is unsupported because Chrome does not authenticate local
clients. The former `--cdp-user-data-dir` option is hidden on the search,
monitor, and login entrypoints solely so upgrades receive the structured
`ArgumentError` response; it never opens a connection. Run browser work on the
trusted browser-owning host with `--browser-channel chrome` instead.

## `xianyu.py`

Primary workflow dispatcher:

```text
doctor    -> doctor.py
login     -> login_state.py
search    -> spider.py
task      -> task_manager.py
monitor   -> monitor.py
install   -> install_skill.py
```

Run `python scripts/xianyu.py --help` or append `--help` after a command. The
dispatcher imports only the selected module in the same process and forwards
the remaining argv, stdin/TTY, cwd, stdout, stderr, `SystemExit`, and return code
without interpreting command-specific values. It changes only the displayed
program name so delegated help remains directly copyable. Unknown commands do
not echo the supplied value; they return one `ArgumentError` JSON object and
exit `2`. A `SIGTERM` received while loading a selected module still enters the
controlled cancellation contract and exits `130`.

The direct scripts remain supported for backward compatibility and advanced
use. `create_state.py` is the credential-safe Cookie/storage-state import tool;
`cdp_profile.py` is the guarded legacy migration cleanup. They are intentionally
not promoted into the six-command primary workflow.

## `doctor.py`

Run read-only prerequisite checks before login or search:

```text
--state-output-dir  Optionally check one existing private state-output directory
--tasks-dir         Optionally check one existing private task directory
```

The command does not launch a browser, import Playwright, write files, inspect
credential contents, or echo supplied paths. It checks Python 3.10+, required
imports, installed Playwright Chromium executables, local Chrome at the exact
paths used by Playwright's `chrome` channel, and only the directory metadata
explicitly requested. Output has
stable top-level `ok`, `checks`, and `next_action` fields. Exit is `0` when all
required checks pass and `2` otherwise. `next_action.code` is one of
`upgrade-python`, `install-dependencies`, `install-browser`,
`fix-private-directories`, `ready`, or `ready-use-browser-channel`.

## `spider.py`

Search Xianyu once and emit a JSON object.

```text
--keyword, -k       Required search keyword
--min-price         Inclusive local minimum price
--max-price         Inclusive local maximum price
--location          Case-insensitive location substring
--pages, -p         Maximum pages to fetch; default 1
--state, -s         Playwright state or enhanced snapshot
--proxy             HTTP(S) or SOCKS proxy; may be visible in argv
--proxy-file        Read the proxy URL from a user-private UTF-8 file
--browser-channel   Optional executable channel; does not reuse a profile
--headed            Show the browser
--retries, -r       Network/browser attempts; default 3
--debug             Include applied filters in output
--quiet             Suppress routine diagnostic logs
```

Successful output:

```json
{
  "ok": true,
  "keyword": "iPhone",
  "count": 1,
  "pages_scraped": 2,
  "items": [
    {
      "id": "123",
      "title": "listing title",
      "price": 4999,
      "url": "https://www.goofish.com/item?id=123",
      "image": "https://...",
      "location": "上海",
      "seller": "nickname",
      "publish_time": "2026-07-24 12:30",
      "wants": "5",
      "tags": ["包邮"]
    }
  ],
  "search_capability": {"status": "passed-for-this-run"},
  "authentication": {"status": "not-evaluated"},
  "identity": {"status": "not-evaluated"},
  "cleanup": {"status": "complete-or-not-required"}
}
```

Failure output:

```json
{
  "ok": false,
  "keyword": "iPhone",
  "error": "reason",
  "error_type": "SearchRejectedError",
  "search_capability": {"status": "rejected-for-this-run"},
  "authentication": {"status": "not-evaluated"},
  "identity": {"status": "not-evaluated"},
  "cleanup": {"status": "complete-or-not-required"}
}
```

Valid empty searches return `"ok": true` with an empty `items` array. Rejected or
unreadable responses return `"ok": false`.
Login challenges, risk-control, invalid-state, and missing-dependency errors are
not retried. A missing or malformed expected search response raises
`SearchCaptureError` and is also not retried. Intercepted-response transport
failures use `SearchTransportError` and the configured retry count, as do other
transient browser/network failures.
If a supplied candidate state reaches `/search` without emitting the search API
in headless mode, make at most one explicit `--headed` attempt. Do not automate
repeated headed attempts or add anti-detection bypasses.
An `RGV587` rejection ends the run. Let the request/session cool down; account
identity remains unknown. Do not re-login, rotate proxies, or make a headed
retry in response to that code.

Minimum and maximum prices must be finite, non-negative numbers; `NaN` and
infinities fail validation.

## `task_manager.py`

Always pass `--data-file` before the subcommand.

```bash
python scripts/task_manager.py --data-file TASKS create KEYWORD [OPTIONS]
python scripts/task_manager.py --data-file TASKS list [--running]
python scripts/task_manager.py --data-file TASKS stop TASK_ID
python scripts/task_manager.py --data-file TASKS resume TASK_ID
python scripts/task_manager.py --data-file TASKS reset-seen TASK_ID
python scripts/task_manager.py --data-file TASKS delete TASK_ID
```

Create options:

```text
--min-price
--max-price
--location
--criteria
--pages
--retries
--state
--browser-channel
--allow-duplicate
```

Task IDs use random UUID fragments. Task JSON is built under a private
same-filesystem directory and atomically replaced. Lock acquisition first writes
and syncs a private same-directory anchor, then publishes the lock with a
no-replace hard link. Filesystems without that primitive fail closed.
Existing lock files are never deleted based on age or PID guesses; they fail
closed with a timeout. After a crashed process, an operator must first verify
that no task mutation is running before removing the exact `.lock` file.
Existing version-1 task files are normalized when loaded.
The complete task file is schema-validated before use: field types, unique IDs,
bounded result/seen lists, and finite prices are required. A malformed entry or
non-standard JSON number fails the operation without rewriting or filtering the
file.

A successful create returns the task under `result`; use its `id` for scoped
baseline and monitor commands:

```json
{
  "ok": true,
  "result": {
    "id": "task_0123456789ab",
    "keyword": "MacBook Air M2",
    "state_file": "/absolute/private/path/xianyu-state.json",
    "browser_channel": "chrome",
    "status": "running"
  }
}
```

Cancelled mutations and post-commit finalization failures keep task-file
evidence separate from the process result:

```json
{
  "ok": false,
  "error": "task command cancelled",
  "error_type": "KeyboardInterrupt",
  "task_commit_status": "recorded",
  "persistence": {"status": "recorded"},
  "cleanup": {"status": "complete-or-not-required"},
  "result": {"updated": true}
}
```

`task_commit_status` and `persistence.status` use these values:

- `recorded`: the mutation committed. Retain the returned `result` even though
  the command failed or was cancelled.
- `not-recorded`: the mutation is known not to have committed.
- `not-established`: atomic-commit reconciliation failed. The output contains
  `possible_result`; inspect the task file before deciding whether to retry.
- `not-attempted`: no mutating command was dispatched, for example because
  task-manager initialization failed.

Every task CLI response also has independent cleanup evidence. A cleanup
failure is reported as
`{"cleanup":{"status":"failed","errors":["generic cleanup description"]}}`;
otherwise the status is `complete-or-not-required`. Cleanup failure does not
change `task_commit_status`: a mutation can be `recorded` while lock cleanup
failed, so callers must handle both fields.

For `stop`, `resume`, and `reset-seen`, `result`/`possible_result` wraps the
boolean as `updated`; `delete` uses `deleted`; `create` returns the task object.

An equivalent active task is returned with `"existing": true` unless
`--allow-duplicate` was supplied. Equivalence includes every task-defining
field: keyword, price bounds, location, criteria, pages, retries, normalized
state path, and normalized browser channel. Do not automatically baseline an
existing task, because doing so can suppress pending new-item notifications.

`criteria` is stored and returned unchanged as an optional downstream analysis
hint. The deterministic collector does not execute it. Only keyword, numeric
price bounds, and location are enforced as filters.

Newly created `state_file` values are stored as absolute paths. A relative
`--state` passed while creating a task resolves from the task JSON's parent
directory, not the shell's working directory, so later scheduler runs remain
stable. The final path component is not dereferenced, so a stable `state.json`
symlink can be rotated to a new credential file without editing the task.
Legacy persisted relative values are preserved rather than silently
reinterpreted. `monitor.py` rejects them until the caller supplies an explicitly
authorized absolute `--state` override or recreates the task with an absolute
path.

`seen_item_ids` retains the most recent 50,000 IDs. `last_results` retains at
most 100 listings.

## `monitor.py`

Run one or every active task:

```text
--tasks-file       Task JSON path
--task-id          Run only one task; omit for all active tasks
--state            Override the task state path
--proxy            Proxy URL; may be visible in argv
--proxy-file       Read the proxy URL from a user-private UTF-8 file
--browser-channel  Optional executable channel; does not reuse a profile
--headed           Show the browser
--include-seen      Return all matches instead of only new listings
--baseline          Store current matches as seen and report zero new listings
--quiet-if-empty    Suppress stdout and routine logs after successful zero-new runs
```

The top-level `new_count` is the sum of new items across tasks. Any failed task
sets top-level `"ok": false` and causes a nonzero exit.
A missing task file is an error, never an implicit empty task set.
A stopped task is rejected even when selected explicitly with `--task-id`;
resume it before invoking its pinned monitor command.
Each task may persist a different browser channel. A monitor-level
`--browser-channel` overrides every selected task; otherwise each task value
wins over `XIANYU_BROWSER_CHANNEL`, which in turn precedes the Playwright default.

Successful output has this shape:

```json
{
  "ok": true,
  "task_count": 1,
  "new_count": 1,
  "tasks": [
    {
      "ok": true,
      "task_id": "task_0123456789ab",
      "keyword": "MacBook Air M2",
      "criteria": "",
      "pages_scraped": 2,
      "matched_count": 4,
      "new_count": 1,
      "baseline_count": 0,
      "search_capability": {"status": "passed-for-this-run"},
      "persistence": {"status": "recorded"},
      "authentication": {"status": "not-evaluated"},
      "identity": {"status": "not-evaluated"},
      "cleanup": {"status": "complete-or-not-required"},
      "items": [
        {
          "id": "123",
          "title": "listing title",
          "price": 4999,
          "url": "https://www.goofish.com/item?id=123"
        }
      ]
    }
  ]
}
```

By default, each task's `items` contains only newly observed listings. “New”
means an item ID not previously stored in `seen_item_ids`; edits to an already
seen listing do not create another notification. With `--include-seen`, `items`
contains every current match instead.

Per-task `persistence.status` is independent from search capability:

- `recorded`: the seen-ID/task update committed.
- `not-recorded`: the update is known not to have committed.
- `not-established`: atomic task-file commit status could not be established.
  The task may retain candidate `items` and sets `possible_duplicate: true`;
  cleanup is reported independently.
- `not-attempted`: search did not reach persistence.

Failed tasks also expose `error_recording.status` independently. `recorded`
means `last_error` committed, `not-recorded` means it is known not to have
committed, `not-established` means that commit could not be reconciled, and
`not-attempted` means cleanup or an earlier interruption prevented the
recording attempt. This field does not change search or seen-item persistence.

Cancellation after a task commit exits `130` with top-level `"ok": false`, but
the committed task remains `"ok": true`, retains its `items`/`new_count`, and
contains `interruption.status: cancelled-after-task-commit`. A non-cancellation
finalization failure uses task `"ok": false`,
`finalization.status: failed`, and `persistence.status: recorded`, while also
retaining the new items. When atomic-commit reconciliation itself fails, the
task is `"ok": false`, retains candidate new items with
`persistence.status: not-established` and `possible_duplicate: true`, and stops
the batch. Consume or durably queue all retained items **and** surface the
failure. `recorded` items will be deduplicated later; `not-established` items
may appear again if the commit did not land, so at-least-once delivery is safer
than permanent loss. Never start another task in the same batch after
incomplete cleanup.

Run `--baseline` once before scheduling notifications. It reports
`baseline_count` per task and keeps `new_count` at zero.

`--quiet-if-empty` is intended for schedulers where process output becomes a
notification. It suppresses routine scraper logs for the invocation and
suppresses final JSON only after a successful zero-new run. It cannot be
combined with `--include-seen` or `--baseline`, and it does not hide errors:
failed runs still emit JSON and exit nonzero.

Proxy precedence is `--proxy`, then `--proxy-file`, then `XIANYU_PROXY`.
Credential values are never logged. Prefer a `0600`/ACL-protected file or
scheduler-injected environment secret; avoid credentialed command arguments.
HTTP(S) credentials are split into Playwright's dedicated username and password
fields. SOCKS5 is supported only without credentials; authenticated SOCKS5
input fails explicitly rather than silently launching an unauthenticated proxy.

## `cdp_profile.py`

Guarded migration cleanup for a temporary profile initialized by a legacy
CDP-capable release:

```text
--directory        Required exact legacy temporary profile directory
--cleanup          Required; remove it after its Chrome process stops
```

The command no longer initializes or enables CDP profiles. It requires the old
sentinel and an exact temporary path, refuses user-controlled symlink
components, detected Chrome activity, a still-listening legacy debugging
endpoint, and platforms without symlink-safe recursive removal. Run close then
cleanup strictly serially. Success reports `profile.status: removed`; a known
validation failure reports `not-removed`, while interruption/OS uncertainty
reports `not-established` with failed cleanup. No result echoes the directory.

## `create_state.py`

Accept exactly one input method:

```text
--cookie-stdin     Preferred; read a Cookie header from stdin
--cookie-file      Read the Cookie header from a local text file
--cookie, -c       Legacy and insecure; visible to process inspection
```

Use `--output` to select the JSON path and `--force` to replace an existing
file. Interactive TTY input is hidden and completes with Enter; piped or
redirected input completes at EOF. If terminal echo cannot be disabled, the
command fails instead of falling back to visible input. The output is atomic
and uses `0600` permissions where supported. Output includes independent
`state`, `authentication`, `identity`, `search_capability`, and `cleanup`
evidence. A cookie-derived state is only `candidate-saved`;
`authentication.status` remains `not-established`.
Keep credential and task files outside the checkout. If an operator deliberately
stores them inside the repository, use only its root `private/` directory, which
is ignored as a whole; an arbitrary custom JSON filename is not a security
boundary.

## `login_state.py`

Open a dedicated visible browser and save a candidate Playwright browser state:

```text
--output, -o       Required private state-file path
--browser-channel  Optional executable channel; never reuses an existing profile
--confirm-in-browser
                   Use a local-only visible confirmation page instead of terminal input
--timeout          Login timeout in seconds (default 1800)
--force            Explicitly replace an existing state file
```

The command always launches a Playwright-owned browser and closes it during
cleanup. `--browser-channel chrome` selects the Chrome executable but never
reuses an existing session or profile.

The user must complete QR, OTP, password, and CAPTCHA interactions personally.
After scanning a QR code, they may still need to approve the login on the phone;
the QR disappearing is not completion. Wait until the original tab is a normal
Goofish page before final confirmation. In default mode the command prints a
random `SAVE-...` token to the interactive terminal. Agents must pause for the
user to provide that exact token and must not enter or pipe it for them.
`--confirm-in-browser` instead presents the token on a local-only page and
permits a non-TTY command; the agent must release that page to the user.
After token acceptance, this page stays open while validation and persistence
run. It shows `candidate-saved` for five seconds before the dedicated browser is
closed; a pre-save failure shows a generic failure state instead. If persistence
committed but a later writer step failed, it says the candidate was saved while
the command was incomplete. These page messages never claim authentication,
identity, or search capability.
Default-mode non-TTY input, EOF, a wrong token, a login/challenge page, or no
retained filtered Goofish browser-storage material fails without writing.

The default 1800-second window covers login through final confirmation. Only
after that confirmation does the command open a fresh page and spend at most 15
seconds best-effort observing the current PC navigation response's nonempty
`displayName`. The signal is optional: absence or an ordinary probe failure
reports `not-observed` and does not discard the candidate. Cancellation and
cleanup failures remain terminal.
Candidate persistence requires final user confirmation, a normal HTTPS Goofish
page, and nonempty filtered Goofish state—not this navigation signal.

Default-mode non-TTY failure includes a structured `handoff` object with
`required: true`, `environment: normal-user-terminal`, and an `argv_template`
array. Private state paths are placeholders, while argument boundaries are
preserved. An agent must not inject the confirmation token on the user's behalf.

Xianyu's current PC layout exposes that navigation field as optional evidence,
but the command reports only whether it was observed and never treats it as
authentication or identity proof. The raw response can contain identity fields,
so the command does not copy it into output or state. It writes only filtered
Playwright state atomically, uses `0600` permissions where supported, creates a
missing containing directory as `0700`, and rejects a final output symlink.
The `browser-opening`, `browser-confirmation-ready`,
`browser-confirmation-accepted`, and `browser-confirmation-complete` progress
objects go to stderr. If the candidate committed but a later writer step fails,
stderr instead adds `browser-confirmation-warning` and the page distinguishes
the saved candidate from the failed command. Stdout contains exactly one final
success, failure, or cancellation JSON object with `exit_reason` set to
`completed`, `failed`, or `cancelled`.

Success keeps evidence dimensions separate:

```json
{
  "ok": true,
  "exit_reason": "completed",
  "state": {
    "status": "candidate-saved"
  },
  "confirmation": {
    "status": "interactive-token-received",
    "actor": "not-machine-verified",
    "channel": "terminal"
  },
  "session": {"nav_display_name": "present"},
  "authentication": {"status": "not-established"},
  "identity": {"status": "not-machine-verified"},
  "search_capability": {"status": "not-tested"},
  "cleanup": {"status": "complete-or-not-required"}
}
```

When the optional signal is absent, a successful candidate instead reports:

```json
{
  "state": {"status": "candidate-saved"},
  "session": {"nav_display_name": "not-observed"},
  "authentication": {"status": "not-established"},
  "identity": {"status": "not-established"},
  "search_capability": {"status": "not-tested"}
}
```

`state.status` can also be `not-saved` when the publish is known not to have
committed, or `not-established` when interruption or an OS error prevented the
atomic publish status from being determined. Treat a `not-established` output
path as a secret, anomalous candidate: do not use or inspect it, and do not
claim it was either saved or absent.

The confirmation status proves only that the interactive terminal received the
token, or that the local confirmation page observed the matching token. The
channel is `terminal` or `browser`; neither identifies the actor. In browser
mode an agent must release browser control and must not inspect, fill, or click
the confirmation page. If the user denies providing the confirmation, or an
agent entered it, treat the output and any resulting file as anomalous and
unusable. After every saved candidate, run a real controlled search and require
`search_capability.status: passed-for-this-run`; login capture alone does not
validate capability. Even a passing search proves neither authentication nor
account identity. Do not automate or bypass login challenges or risk control.

If the optional display-name signal is observed, identity remains
`not-machine-verified`; if it is not observed, session reports `not-observed`
and identity remains `not-established`. Authentication is always
`not-established` at capture time. Before writing, the command removes all
Cookies and origins outside `goofish.com`. The remaining site-created Goofish
state is still a secret and may encode account data. The command does not echo
the selected output path.
Keep login-command logs local and never upload them with support bundles or CI
artifacts anyway.

## `install_skill.py`

Install one checkout into current Agent Skills discovery roots:

```text
--host       codex, claude, openclaw, or all; repeatable (default all)
--mode       symlink or copy (default symlink)
--dry-run    Report planned targets without writing
```

Codex and OpenClaw currently share `~/.agents/skills/xianyu-monitor`. Claude
Code uses `~/.claude/skills/xianyu-monitor`, so an `all` install creates at most
two entries. The installer refuses to replace any existing unrelated path or
silently convert an existing symlink install into copy mode. If a later target
fails, targets created by that same invocation are rolled back.

Copy mode installs the minimal runtime, Skill-facing references/metadata, and
license. It excludes the repository README, tests, virtual environments, caches,
and local state/task data. The hidden `--home` option exists only for isolated
testing and packaging. Copy and symlink installs are built
under a private same-filesystem staging path and published with the platform's
atomic no-replace rename. If that primitive or filesystem guarantee is
unavailable, installation fails closed instead of using a check-then-rename
fallback.

Successful and dry-run output keeps the existing `installs` records, including
each exact `target`. If cancellation occurs, exit `130` instead emits
path-private evidence: overall `installation.status` and each install status are
`installed`, `not-installed`, or `not-established`, with an independent
`cleanup` object. A `not-established` result means rollback or target
reconciliation was incomplete; inspect the configured discovery root before
retrying. Cancellation output intentionally omits target paths.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Operation succeeded |
| `2` | Validation, login challenge, risk-control, browser, or task failure |
| `130` | Operation was cancelled; inspect independent commit/cleanup evidence |

Diagnostic logs go to stderr. Machine-readable JSON goes to stdout unless
`--quiet-if-empty` suppresses a successful zero-new monitor run.
Stdout JSON escapes non-ASCII characters so redirected Windows and legacy
scheduler encodings cannot lose a notification; JSON parsers recover the
original Unicode text.

The supported entrypoint contracts are a script path from any working
directory, such as `python /absolute/skill/scripts/xianyu.py monitor`, or a
module from the skill root, such as `python -m scripts.xianyu monitor` and the
legacy `python -m scripts.monitor`. Agent and scheduler examples use script
paths because they are simplest to make absolute. Use the script-path form for
the doctor's strict no-write guarantee; Python itself may create package
bytecode before a `-m scripts.xianyu doctor` module begins unless `-B` is used.
