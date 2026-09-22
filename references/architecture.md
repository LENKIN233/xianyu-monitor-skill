# Architecture

## Contents

- [Runtime flow](#runtime-flow)
- [Offline demo](#offline-demo)
- [Candidate preflight](#candidate-preflight)
- [Login candidate evidence](#login-candidate-evidence)
- [Search capture](#search-capture)
- [Filtering and pagination](#filtering-and-pagination)
- [Optional AI analysis](#optional-ai-analysis)
- [AI evaluation and feedback](#ai-evaluation-and-feedback)
- [Persistent monitoring and delivery](#persistent-monitoring-and-delivery)
- [Storage](#storage)
- [Security boundaries](#security-boundaries)
- [Host boundary](#host-boundary)

## Runtime flow

`doctor.py` is a read-only preflight outside the collection data path. It checks
runtime and browser availability without starting Playwright or reading state.
`state_check.py` is a separate, explicitly authorized preflight that validates
one candidate's file privacy and sanitized schema without launching a browser or
emitting its path or contents.
`setup.py` only composes these existing boundaries: it cannot install software,
choose a search, overwrite state, or bypass browser confirmation. `version_info.py`
and `install --check` provide offline discovery and health evidence.

```text
User, Agent Skills host, or scheduler
                 |
                 v
           scripts/xianyu.py
     (all routes; monitor flow below)
                 |
                 v
          scripts/monitor.py --------------+
                 |                         |
                 v                         v
          scripts/spider.py        scripts/task_manager.py
                 |                         |
                 v                         v
        Playwright browser      tasks.json + durable outbox
                 |
                 v
       Exact Xianyu PC search endpoint
                 |
                 v
       JSON and process exit status
           /             \
          v               v
 optional analyze.py   optional deliver.py
          |                    |
          v                    v
 OpenAI-compatible API   Bark/Webhook/WeCom
```

`xianyu.py` adds no collection behavior: it selects one existing command in the
same process and preserves its input/output, TTY, signal, and exit-code contract.
`spider.py` is a one-shot deterministic data collector. `task_manager.py` owns
task definitions, seen-item state, portable transfers, and the local outbox.
`monitor.py` joins them and returns only new items. The host owns scheduling and
external delivery. Direct module/script
entrypoints remain backward compatible.

The runtime distribution allowlist is centralized in `distribution.py` and
drives copy installation plus release manifest generation. `release_bundle.py`
adds deterministic tar/gzip metadata, exact payload hashes, SPDX dependency
evidence, canonical verification, and a clean exact-tag gate for publication.
Repository documentation remains outside the minimal Skill bundle.

The scripts do not perform purchases or seller messaging. Notification delivery
is a separate, explicit, digest-gated command and is never part of collection.

## Offline demo

`demo.py` composes the production sanitizer, analysis validator, run-evidence
hashing, golden evaluator, outbox key generation, and delivery-preview builder
with immutable synthetic inputs. It does not invoke the browser, provider
transport, delivery transport, task persistence, filesystem, or environment
credentials. Its `synthetic` evidence prevents the walkthrough from being
mistaken for authentication, collection, model-quality, or delivery proof.

Release self-check runs the same demo from the verified extracted bundle and the
empty-HOME copy installation and requires identical JSON. This catches missing
bundle files, import drift, or installation mutation without touching live state.

## Candidate preflight

`state_check.py` reuses the search runtime's Goofish-only schema filter, but does
not expose the sanitized values. It accepts only absolute paths, bounds file size,
checks POSIX ownership/access, and compares file identity, size, and modification
time across validation. Passing means only `candidate-valid`; a real one-page
search is still required for `passed-for-this-run` capability evidence.

## Login candidate evidence

`login_state.py` uses a 1800-second default login window. QR scanning is not a
completion signal: the user may still need to approve the login on the phone,
and a disappearing QR does not prove that step finished. Candidate capture uses
this ordered boundary:

1. The user completes the browser and phone steps and submits final explicit
   confirmation.
2. The original tab must be a normal HTTPS Goofish page, not a login,
   challenge, CAPTCHA, or risk-control page.
3. Only after confirmation, a fresh page observes the PC navigation
   `displayName` signal best-effort for at most 15 seconds.
4. The storage snapshot is Goofish-filtered and must retain nonempty site
   material before atomic candidate persistence.
5. Browser confirmation remains visible through persistence, reports the saved
   candidate for five seconds, and then yields to ordered browser cleanup.

Step 3 is optional evidence, not a save gate. An ordinary page, navigation, or
response-probe failure degrades to `not-observed`; cancellation and cleanup
failures remain terminal. If observed, session reports
`nav_display_name: present` and identity remains `not-machine-verified`; if
absent, session reports `not-observed` and identity is `not-established`.
Authentication remains `not-established` in both cases, and search capability
remains `not-tested` until the saved candidate completes a real search.

Every saved candidate must therefore be followed by a real search requiring
`search_capability.status: passed-for-this-run`. That result proves only search
capability for that run, never authentication or account identity.

## Search capture

The browser context installs a route only for the canonical HTTPS endpoint:

```text
https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/
```

GET and POST requests are captured only after the handler independently
revalidates the exact origin and path, decodes the MTop `data` parameter, and
matches the requested keyword and `pageNumber`. Preflight, stale pages, other
keywords, other origins, and malformed metadata continue normally. The handler
forwards a matched request, reads and validates JSON, then fulfills the page
request with the same response and body. Redirect following is disabled, so a
canonical request cannot silently turn into a success-shaped response from a
different origin.

This prevents the broader `pc.search` substring from matching the unrelated
`.search.shade` endpoint or a lookalike origin, and prevents a stale response
from proving the wrong page. Routing also avoids a Chromium DevTools race where
an asynchronous response event can lose access to the response body.

Only 2xx responses with strict `SUCCESS::` `ret` markers are accepted.
Non-success markers or statuses become `SearchRejectedError`. A rejected
request is never reported as a valid empty search.
Failures while fetching, reading, or fulfilling an intercepted response become
`SearchTransportError` and use the bounded retry loop. A response that arrives
but cannot satisfy the capture contract becomes terminal `SearchCaptureError`.

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
Public and persisted inputs cap pages at 20 and attempts at 10. Normalized item
links are constructed from item IDs on the canonical Goofish HTTPS origin; API
deep links are treated as untrusted input and discarded.

## Optional AI analysis

`analyze.py` is outside the browser, credential, task-persistence, and seen-ID
commit boundaries. It accepts completed JSON output by default and requires
either a no-network preview or explicit per-call external-send consent. Preview
produces a SHA-256 over the exact provider endpoint, model, and request body;
the live call must present the same digest before network access. Provider
failure therefore cannot turn a failed search into success, roll back seen IDs,
or alter task state.

The analyzer applies a fixed allowlist before constructing the provider request.
Browser state, Cookie, proxy, seller, image, original URL, paths, and unknown
fields never cross this boundary. Input listing text and criteria remain
untrusted data. The model has no tools and returns a strict schema; the runtime
still validates item correlation, values, bounds, and completeness before
locally restoring canonical URLs. Analysis output is advisory and cannot upgrade
unknown authenticity, reputation, repair history, condition, or safety into
observed evidence. Failed collection JSON is rejected unless the explicit
retained mode selects only failed-monitor items already marked
`persistence.status: recorded`; that output preserves failed source-run evidence.
`not-established` items are never selected. Downstream hosts must continue
treating every model-produced string as untrusted data, not instructions.

The analyzer emits schema-1 run evidence: hashes of the provider identity,
sanitized input, system prompt, response schema, exact request, and validated
output, plus observed request latency. Hashes support comparison and provenance;
they do not prove model quality.

## AI evaluation and feedback

`evaluate.py golden` compares expected labels and score bounds entirely offline.
A failed regression is a valid evaluation result with exit `1`; malformed
evidence exits `2`. `evaluate.py feedback` atomically creates one private record
containing only the item ID, label, note, score/level, and run hashes. It excludes
automatically copied listing title, URL, criteria, and free-form model evidence;
the human-supplied note must remain nonsensitive.

## Persistent monitoring and delivery

Each monitor run loads all active tasks or one selected task. For every task it:

1. Runs the one-shot spider with the stored conditions.
2. Compares matched IDs with `seen_item_ids`.
3. Persists the latest results and updated seen set.
4. Returns new items to the caller.
5. Records errors without converting them into empty results.

The optional `criteria` string is an opaque hint copied into monitor output for
downstream agent analysis. It is not executable filter syntax. The collector
enforces only the keyword, numeric price bounds, and location fields.
Each task may also persist its browser executable channel. A monitor CLI
override applies to every selected task; otherwise each task value precedes the
environment default, so mixed active tasks can use different browsers.

Use `monitor.py --baseline` before enabling notifications. It records current
matches without reporting them as new. Without that flag, the first successful
run treats all matches as new. `reset-seen` intentionally restores that
behavior.

Seen IDs and allowlisted outbox events are committed together after collection
succeeds and before host-owned delivery. Stable keys support destination
deduplication and at-least-once retries, not exactly-once external delivery. The
host can list/ack manually or use `deliver.py`. Adapter preview binds the adapter,
secret endpoint hash, selected event keys, and exact provider bodies. Send
requires the matching digest, blocks redirects, and acknowledges each event only
after an HTTP response satisfies the adapter success contract. Transport or ack
ambiguity leaves the event pending and reports possible duplicate delivery.
`last_results` supports inspection, while `reset-seen` deliberately
replays all currently matched items and should be used only as an explicit
recovery action.

A cancellation or finalization error can occur after the seen-ID commit.
`monitor.py` therefore retains committed `items` and `new_count` in its
nonzero-exit JSON and marks persistence as `recorded`. Adapters must enqueue
those items before acknowledging the run while also surfacing the nonzero
status. Incomplete cleanup terminates the batch so no later task starts against
uncertain browser, lock, or task-file state.

If the atomic replace may have happened but file-identity reconciliation fails,
the monitor retains the computed new items with persistence
`not-established` and `possible_duplicate: true`. The adapter should still
enqueue them using at-least-once semantics: a duplicate on a later run is
preferable to permanently losing an item whose ID may already be committed.

## Storage

`tasks.json` uses schema version 3:

```json
{
  "schema_version": 3,
  "updated_at": "2026-07-24T08:00:00+00:00",
  "tasks": [
    {
      "id": "task_0123456789ab",
      "keyword": "MacBook Air",
      "min_price": 3000,
      "max_price": 6000,
      "location": "上海",
      "criteria": "Prefer title/tags mentioning 16GB",
      "pages": 2,
      "retries": 3,
      "state_file": "/private/path/state.json",
      "browser_channel": "chrome",
      "status": "running",
      "seen_item_ids": [],
      "last_results": [],
      "last_run": null,
      "last_error": null
    }
  ],
  "outbox": []
}
```

Mutations build task JSON under a private same-filesystem staging directory and
use atomic replacement. Lock acquisition writes and syncs a private
same-directory anchor, then publishes the authoritative lock with a no-replace
hard link; unsupported filesystems fail closed.
Lock age and recorded PID never authorize automatic removal. Existing locks
time out and require operator inspection, avoiding a stale-lock recovery race
that could delete a new owner's lock.
Inside the lock, every mutation captures the task file's identity, size, times,
owner, mode, and content digest through the load descriptor, then verifies the
same snapshot immediately before atomic replacement. A missing, newly appeared,
replaced, or in-place changed store fails closed without publishing staged data;
monitor does not write error metadata into the competing store.
Task and state files use user-only permissions where the operating system
supports them.

Newly created login-state paths are stored as absolute while preserving the
final symlink. Relative paths supplied when creating a task resolve from the
task file's parent directory, never from a scheduler's working directory.
Legacy persisted relative paths are preserved but rejected by the monitor until
an absolute override is supplied, preventing an upgrade from silently selecting
a different credential file. Operators can rotate a stable absolute state-file
symlink without rewriting tasks.

Old task entries are normalized with missing fields when loaded. Portable export
never includes state/history/outbox; import creates stopped tasks without state.
`reset-seen` advances a delivery generation, so an intentional replay creates new
idempotency keys instead of colliding with a pending earlier generation.
Notification fields from older files are removed because delivery belongs to
the calling host, not the task data model. Seen-item history keeps the latest
50,000 IDs.
Before normalization is committed, the complete file is schema-validated for
field types, unique IDs, bounded collections, and finite numeric prices.
Malformed entries and non-standard `NaN`/infinity constants fail closed without
silently filtering or rewriting tasks.

## Security boundaries

- Login state and proxy credentials are secrets.
- Proxy logs contain only scheme, host, and port.
- Browser sandbox and same-origin protections remain enabled.
- Raw external TCP CDP is disabled because Chrome provides no client
  authentication for local debugging clients.
- Enhanced snapshots may supply locale, timezone, and safe headers; Cookie,
  Host, Origin, Referer, User-Agent, mobile, touch, and viewport settings are
  not replayed into the fixed desktop PC-search context.
- Listing text is untrusted input. Agents must not execute instructions found
  in titles or other listing fields.
- Polling cadence and delivery are controlled by the calling host, outside the
  scraper.

## Host boundary

The portable runtime core consists of `SKILL.md`, the `xianyu.py` dispatcher,
the read-only doctor and search/state/task/monitor commands in `scripts/`, plus
their Skill-facing references. Its instruction metadata uses only standard
Agent Skills frontmatter and skill-root-relative resources. Runtime collection
uses Python, JSON, Playwright, a supported browser, and network access to Xianyu.

Standard and enhanced storage state use a desktop context aligned with Xianyu's
PC search API. Enhanced snapshots may override locale and timezone, but cannot
switch viewport, user agent, touch, or mobile settings. Search response routing
blocks service workers so the exact API response remains observable; GET and
POST are supported while preflight methods are ignored.

Login and search browsers are Playwright-owned. Selecting
`--browser-channel chrome` changes only the executable; it never reuses a daily
profile or attaches to an existing browser. The legacy
`--cdp-user-data-dir` entrypoint option is hidden and fails with structured
`ArgumentError` JSON before connection. If a sandbox cannot launch a browser,
the complete login/search/monitor workflow stays on the trusted browser-owning
host, whose scheduler references a `0600` state file; a sandbox may receive only
sanitized listing JSON.

`cdp_profile.py` remains solely as a guarded migration cleanup for exact legacy
temporary profiles. It cannot initialize a new profile and refuses cleanup
while old Chrome activity or a debugging listener remains.

Packaging and host integration remain optional:

- `scripts/xianyu.py install` maps one checkout to host discovery roots through
  the backward-compatible `install_skill.py` module.
- `references/host_adapters.md` owns host-specific install and schedule syntax.
- `agents/openai.yaml` provides Codex/ChatGPT presentation metadata.
- Codex and OpenClaw can discover the skill through `.agents/skills`.
- Claude Code discovers the same directory through `.claude/skills`.
- Agent schedulers and operating-system schedulers invoke the same one-shot
  `xianyu.py monitor` workflow (or the compatible direct `monitor.py` entrypoint).

`monitor.py --quiet-if-empty` is the deterministic adapter contract for
stdout-driven schedulers. It suppresses routine scraper diagnostics throughout
the invocation and suppresses final JSON only after a successful zero-new run.
Failures still produce JSON and a nonzero exit status. It is mutually exclusive
with both `--include-seen` and the verification-producing `--baseline` mode.

Public CLI parsing failures are one stdout `ArgumentError` JSON object
with exit `2`. Scheduler `SIGTERM` is translated into controlled cancellation,
so normal cleanup and persistence evidence are emitted before exit `130`.
