# CLI and output reference

## Contents

- [spider.py](#spiderpy)
- [task_manager.py](#task_managerpy)
- [monitor.py](#monitorpy)
- [create_state.py](#create_statepy)
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
--proxy             HTTP(S) or SOCKS proxy
--browser-channel   Optional Playwright channel, such as chrome
--headed            Show the browser
--retries, -r       Network/browser attempts; default 3
--debug             Include applied filters in output
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

`seen_item_ids` retains the most recent 50,000 IDs. `last_results` retains at
most 100 listings.

## `monitor.py`

Run one or every active task:

```text
--tasks-file       Task JSON path
--task-id          Run only one task; omit for all active tasks
--state            Override the task state path
--proxy            Proxy URL; credentials are not logged
--browser-channel  Optional Playwright channel
--headed           Show the browser
--include-seen      Return all matches instead of only new listings
--baseline          Store current matches as seen and report zero new listings
```

The top-level `new_count` is the sum of new items across tasks. Any failed task
sets top-level `"ok": false` and causes a nonzero exit.

Run `--baseline` once before scheduling notifications. It reports
`baseline_count` per task and keeps `new_count` at zero.

## `create_state.py`

Accept exactly one input method:

```text
--cookie-stdin     Preferred; read a Cookie header from stdin
--cookie-file      Read the Cookie header from a local text file
--cookie, -c       Legacy and insecure; visible to process inspection
```

Use `--output` to select the JSON path and `--force` to replace an existing
file. The output is atomic and uses `0600` permissions where supported.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Operation succeeded |
| `2` | Validation, authentication, risk-control, browser, or task failure |

Diagnostic logs go to stderr. Machine-readable JSON goes to stdout.
