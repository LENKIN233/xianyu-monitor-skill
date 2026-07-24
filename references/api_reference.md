# CLI and output reference

## Contents

- [spider.py](#spiderpy)
- [task_manager.py](#task_managerpy)
- [monitor.py](#monitorpy)
- [create_state.py](#create_statepy)
- [login_state.py](#login_statepy)
- [install_skill.py](#install_skillpy)
- [Exit codes](#exit-codes)

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
--browser-channel   Optional Playwright channel, such as chrome
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
  ]
}
```

Failure output:

```json
{
  "ok": false,
  "keyword": "iPhone",
  "error": "reason",
  "error_type": "SearchRejectedError"
}
```

Valid empty searches return `"ok": true` with an empty `items` array. Rejected or
unreadable responses return `"ok": false`.
Authentication, risk-control, invalid-state, and missing-dependency errors are
not retried. A missing expected search request raises `SearchCaptureError` and
is also not retried. Other transient browser/network failures use the configured
retry count.
If a valid state reaches `/search` without emitting the search API in headless
mode, make at most one explicit `--headed` attempt. Do not automate repeated
headed attempts or add anti-detection bypasses.
An `RGV587` rejection ends the run. Wait for the account to cool down; do not
re-login, rotate proxies, or make a headed retry in response to that code.

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
--allow-duplicate
```

Task IDs use random UUID fragments. Writes use a lock and atomic replacement.
Existing version-1 task files are normalized when loaded.

A successful create returns the task under `result`; use its `id` for scoped
baseline and monitor commands:

```json
{
  "ok": true,
  "result": {
    "id": "task_0123456789ab",
    "keyword": "MacBook Air M2",
    "state_file": "/absolute/private/path/xianyu-state.json",
    "status": "running"
  }
}
```

An equivalent active task is returned with `"existing": true` unless
`--allow-duplicate` was supplied. Equivalence includes every task-defining
field: keyword, price bounds, location, criteria, pages, retries, and normalized
state path. Do not automatically baseline an existing task, because doing so
can suppress pending new-item notifications.

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
--browser-channel  Optional Playwright channel
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
and uses `0600` permissions where supported.

## `login_state.py`

Open a dedicated visible browser and save a candidate Playwright login state:

```text
--output, -o       Required private state-file path
--browser-channel  Optional installed browser channel, such as chrome
--timeout          Login timeout in seconds (default 600)
--force            Explicitly replace an existing state file
```

The user must complete QR, OTP, password, and CAPTCHA interactions personally.
The command detects a nonempty login Cookie without printing its value, writes
atomically, uses `0600` permissions where supported, creates a missing
containing directory as `0700`, and rejects a final output symlink. Its success
status is `candidate-state-saved`; only a later controlled successful search
verifies that Xianyu accepts the state. Do not automate or bypass login
challenges or risk control.

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

Copy mode uses a runtime allowlist and excludes repositories, virtual
environments, test caches, and local state. The hidden `--home` option exists
only for isolated testing and packaging.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Operation succeeded |
| `2` | Validation, authentication, risk-control, browser, or task failure |

Diagnostic logs go to stderr. Machine-readable JSON goes to stdout unless
`--quiet-if-empty` suppresses a successful zero-new monitor run.
Stdout JSON escapes non-ASCII characters so redirected Windows and legacy
scheduler encodings cannot lose a notification; JSON parsers recover the
original Unicode text.

The supported entrypoint contracts are a script path from any working
directory, such as `python /absolute/skill/scripts/monitor.py`, or a module from
the skill root, such as `python -m scripts.monitor`. Agent and scheduler
examples use script paths because they are simplest to make absolute.
