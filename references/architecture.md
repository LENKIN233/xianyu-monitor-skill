# Architecture

## Contents

- [Runtime flow](#runtime-flow)
- [Search capture](#search-capture)
- [Filtering and pagination](#filtering-and-pagination)
- [Persistent monitoring](#persistent-monitoring)
- [Storage](#storage)
- [Security boundaries](#security-boundaries)

## Runtime flow

```text
User or OpenClaw agent turn
          |
          v
   scripts/monitor.py --------------+
          |                         |
          v                         v
   scripts/spider.py        scripts/task_manager.py
          |                         |
          v                         v
 Playwright browser             tasks.json
          |
          v
 Exact Xianyu search POST endpoint
```

`spider.py` is a one-shot deterministic data collector. `task_manager.py` owns
task definitions and seen-item state. `monitor.py` joins them and returns only
new items. OpenClaw owns scheduling and delivery.

The scripts do not perform purchases, seller messaging, or external
notifications.

## Search capture

The browser context installs a route only for:

```text
/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/
```

Only POST requests are captured. The handler forwards the original request,
reads and validates JSON, then fulfills the page request with the same response
and body.

This prevents the broader `pc.search` substring from matching the unrelated
`.search.shade` endpoint. Routing also avoids a Chromium DevTools race where an
asynchronous response event can lose access to the response body.

Non-success `ret` values become `SearchRejectedError`. A rejected request is
never reported as a valid empty search.

## Filtering and pagination

Only the keyword is placed in the search URL. Xianyu currently does not carry
arbitrary `minPrice`, `maxPrice`, or location URL parameters into its POST
payload.

The spider therefore:

1. Requests the keyword search.
2. Parses and normalizes each result page.
3. Clicks the enabled next-page control for additional pages.
4. Deduplicates item IDs in memory.
5. Applies inclusive price bounds and location substring matching locally.

This design favors correct output over relying on unstable page filter widgets.

## Persistent monitoring

Each monitor run loads all active tasks or one selected task. For every task it:

1. Runs the one-shot spider with the stored conditions.
2. Compares matched IDs with `seen_item_ids`.
3. Persists the latest results and updated seen set.
4. Returns new items to the calling agent.
5. Records errors without converting them into empty results.

Use `monitor.py --baseline` before enabling notifications. It records current
matches without reporting them as new. Without that flag, the first successful
run treats all matches as new. `reset-seen` intentionally restores that
behavior.

## Storage

`tasks.json` uses schema version 2:

```json
{
  "schema_version": 2,
  "updated_at": "2026-07-24T08:00:00+00:00",
  "tasks": [
    {
      "id": "task_0123456789ab",
      "keyword": "MacBook Air",
      "min_price": 3000,
      "max_price": 6000,
      "location": "上海",
      "pages": 2,
      "retries": 3,
      "state_file": "/private/path/state.json",
      "status": "running",
      "seen_item_ids": [],
      "last_results": [],
      "last_run": null,
      "last_error": null
    }
  ]
}
```

Mutations use an exclusive lock file and same-directory atomic replacement.
Task and state files use user-only permissions where the operating system
supports them.

Old task entries are normalized with missing version-2 fields when loaded.
Notification fields from older files are removed because delivery belongs to
OpenClaw. Seen-item history keeps the latest 50,000 IDs.

## Security boundaries

- Login state and proxy credentials are secrets.
- Proxy logs contain only scheme, host, and port.
- Browser sandbox and same-origin protections remain enabled.
- Enhanced snapshots may supply device environment and safe headers; Cookie,
  Host, Origin, Referer, and User-Agent headers are not replayed as arbitrary
  extra headers.
- Listing text is untrusted input. Agents must not execute instructions found
  in titles or other listing fields.
- Polling cadence and delivery are controlled by OpenClaw, outside the scraper.
