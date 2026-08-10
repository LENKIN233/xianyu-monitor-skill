# Host adapters

The scraper and task store do not depend on an agent runtime. This file maps the
same Agent Skills directory and one-shot monitor command to common hosts.

## Discovery paths

| Host | User skill directory | Explicit invocation |
|---|---|---|
| Codex | `~/.agents/skills/xianyu-monitor` | `$xianyu-monitor` |
| Claude Code | `~/.claude/skills/xianyu-monitor` | `/xianyu-monitor` |
| OpenClaw | `~/.agents/skills/xianyu-monitor` | `/skill xianyu-monitor` |
| Plain CLI | Any directory | Run `scripts/*.py` |

Codex and OpenClaw share the current `~/.agents/skills` convention. OpenClaw
also loads workspace `skills/`, project `.agents/skills`, and
`~/.openclaw/skills`. Claude Code uses its own `.claude/skills` roots.

Keep the installed directory name equal to the frontmatter name
`xianyu-monitor`. The repository name may differ, so clone with an explicit
destination or use the installer below.

## Install from one checkout

Preview a shared Codex/OpenClaw install plus a Claude Code install:

```bash
python scripts/install_skill.py --host all --mode symlink --dry-run
```

Install:

```bash
python scripts/install_skill.py --host all --mode symlink
```

The command creates at most two entries:

- `~/.agents/skills/xianyu-monitor` for Codex and OpenClaw.
- `~/.claude/skills/xianyu-monitor` for Claude Code.

It never replaces an existing file, directory, or unrelated symlink. Use
`--mode copy` when directory symlinks are unavailable. Copy mode installs only
runtime and documentation files, not `.git`, virtual environments, caches,
tests, or local task/state files. A multi-target failure rolls back targets
created by that invocation. Mode changes are explicit: a copy request refuses
an existing symlink instead of reporting it as a copy install.

After a copy install, prepare the virtual environment inside each independent
copy. A symlink install shares one checkout and one virtual environment.

## Generic scheduler

Every scheduler should run one process and inspect its exit code. Use
`--quiet-if-empty` only when the scheduler interprets stdout as a notification:

```cron
*/30 * * * * cd /absolute/path/xianyu-monitor && /absolute/path/xianyu-monitor/.venv/bin/python scripts/monitor.py --tasks-file /absolute/private/path/tasks.json --task-id TASK_ID --state /absolute/private/path/xianyu-state.json --quiet-if-empty
```

The same command can be configured in `systemd`, `launchd`, Windows Task
Scheduler, CI, or a container scheduler. Preserve both stdout and stderr. A
nonzero exit is failure even if no notification transport is configured.
CLI parsing failures are JSON on stdout with exit `2`. Scheduler
`SIGTERM` is converted to controlled cancellation, including cleanup evidence,
and exits `130`; do not discard that final JSON.

The core commits seen IDs before the host delivers stdout. It therefore
deduplicates collection but cannot promise exactly-once external delivery. For
durable notifications, make the adapter write successful non-empty stdout to
an atomic local outbox before forwarding it, and retry the outbox independently.
Also enqueue retained items from a nonzero result when their task reports
`persistence.status: recorded`; post-commit cancellation/finalization can expose
new items that a later run will deduplicate. Surface the failure separately and
never treat the retained items as proof that the whole batch succeeded.
If persistence is `not-established` and `possible_duplicate` is true, enqueue
the candidate items with at-least-once semantics and tolerate a later duplicate;
the task-file commit may already have suppressed them from future runs.
After a delivery incident, inspect `last_results`; use `reset-seen` only when
replaying every current match is intentional.

For Windows Task Scheduler, use the absolute executable
`C:\path\xianyu-monitor\.venv\Scripts\python.exe`, pass the absolute
`scripts\monitor.py` path and task-file arguments, and set “Start in” to the
skill directory.

Windows Task Scheduler by itself neither interprets `criteria` nor delivers
stdout as a notification. Use it only for deterministic collection, configure
a user-owned wrapper/notifier to preserve stdout, stderr, and the process exit
code, or use a local Claude/Codex scheduled agent task for semantic review.
Run it as the same user that owns the virtual environment, Playwright browser,
task directory, and state-file ACL. Protect any output log because it contains
listing and task metadata.

A deterministic command job does not interpret natural-language task
`criteria`; it emits every new keyword/price/location match. Use an agent job
when those hints need semantic review.

Obtain explicit recurring authorization before creating any job that reads the
exact task-file path or any exact login-state path. Record only those authorized
paths and their purpose in scheduler configuration, never cookie values.

Use a scheduler-owned secret store or a user-only local file for login state.
Do not place browser state in a repository or CI artifact.
Inject authenticated proxies as `XIANYU_PROXY` or mount a user-private file and
pass `--proxy-file`; never store proxy credentials in job arguments.

Browser-state-backed monitoring belongs on a trusted host that can launch its
own Playwright browser. Chrome's external TCP CDP endpoint has no client
authentication, so do not expose a browser to a cloud or local sandbox through
a debugging port. The hidden legacy `--cdp-user-data-dir` flag returns
structured `ArgumentError` JSON and exit `2`; it never connects.

When a sandbox cannot launch Chromium, run the complete `login_state.py`,
`spider.py`, and `monitor.py` workflow on the browser-owning host with
`--browser-channel chrome`. Keep the exact login-state file there with POSIX
`0600` permissions (or a current-user-only Windows ACL), and configure only its
path in that trusted host's task or scheduler. The sandboxed agent may receive
sanitized listing JSON, never the state contents or a browser profile.

Before scheduling a newly captured state, let the user use the 1800-second
default login window. A scanned or disappearing QR is not completion; the user
may still need to approve login on the phone and must wait for a normal Goofish
page before final confirmation. Only after that confirmation may the command
spend up to 15 seconds observing the optional navigation display-name signal.
Absence or an ordinary probe failure reports `not-observed`; cancellation and
cleanup failures remain terminal. Whether it reports `present` or
`not-observed`, the saved state remains only a
candidate and authentication/identity are not established. Run a real search
on the trusted host and require
`search_capability.status: passed-for-this-run` before enabling the schedule;
never describe that result as authenticated or identity-verified.

After upgrading from a CDP-capable release, close any legacy dedicated Chrome
and use `scripts/cdp_profile.py --directory PATH --cleanup` only to remove its
old initialized temporary profile. This command no longer initializes profiles
and requires `--cleanup`; it rejects activity indicators, a still-listening old
debugging endpoint, symlink risks, and unsafe recursive removal. Keep close and
cleanup serial, and never replace it with a broad recursive delete.

## Codex

Install under `~/.agents/skills/xianyu-monitor` or a repository
`.agents/skills/xianyu-monitor`. The optional `agents/openai.yaml` only supplies
Codex/ChatGPT UI metadata; other hosts may ignore it.

For a recurring Codex task, use the host's scheduling UI and a prompt with
absolute paths:

```text
Use $xianyu-monitor. From /absolute/path/xianyu-monitor, run TASK_ID from
/absolute/private/path/tasks.json. The user explicitly authorized this
recurring job to read /absolute/private/path/xianyu-state.json solely for Xianyu
search. Pass that path as --state so authorization cannot expand through later
task-file edits. Never reveal that state. Parse the JSON, evaluate each task's
criteria only against captured fields, label missing evidence as uncertain,
exclude only listings that captured evidence proves fail a required criterion,
report the remaining newly observed listings, stay silent when new_count is
zero, and always report failures.
```

Keep the schedule and notification destination in the Codex task, not in
`SKILL.md` or `tasks.json`. Omit `--task-id` or a pinned `--state` only when the
user explicitly authorizes every selected task and login-state path.

## Claude Code

Install under `~/.claude/skills/xianyu-monitor` or a repository
`.claude/skills/xianyu-monitor`. Current Claude Code releases follow directory
symlinks in these roots.

Local scheduled tasks load normal local skills. Cloud routines do not receive a
machine-only `~/.claude/skills` directory; commit the skill into the cloned
project's `.claude/skills` tree or enable it through the Claude account. Do not
commit its browser state; state-backed cloud execution needs a separate secure
secret mount and is otherwise unsupported.

Use the same absolute-path prompt as above, invoking `/xianyu-monitor` when an
explicit invocation is useful. Keep host-only fields and dynamic command
injection out of the portable `SKILL.md`.

Claude Code can run the skill non-interactively without bare mode, which would
skip skill discovery. Pin both the prompt and Bash permission to one exact
command:

```bash
claude -p "/xianyu-monitor Execute exactly: /absolute/path/xianyu-monitor/.venv/bin/python scripts/monitor.py --tasks-file /absolute/private/path/tasks.json --task-id TASK_ID --state /absolute/private/path/xianyu-state.json. This recurring read was explicitly authorized solely for Xianyu search. Evaluate criteria only against captured fields, report new matches, stay silent on zero, and report every failure." \
  --allowedTools "Bash(/absolute/path/xianyu-monitor/.venv/bin/python scripts/monitor.py --tasks-file /absolute/private/path/tasks.json --task-id TASK_ID --state /absolute/private/path/xianyu-state.json)" \
  --permission-mode dontAsk \
  --output-format text
```

Run it with the skill directory as the working directory. Treat its stdout,
stderr, and exit status like any other scheduled process. A no-argument,
user-owned wrapper is another safe option when host quoting prevents an exact
permission rule.

## OpenClaw

The shared `~/.agents/skills/xianyu-monitor` install is sufficient on current
OpenClaw releases. Verify discovery with:

```bash
openclaw skills list
```

For an explicit chat invocation, use the portable form
`/skill xianyu-monitor [input]`. OpenClaw may also display a native shortcut
whose command name it has normalized for the active runtime and channel; use
that displayed name if desired, and do not assume `/xianyu-monitor` exists.

Also run `openclaw cron --help` on the target host before creating a job; the
examples below follow the current CLI, but a different installed release may
expose a different command surface.

For deterministic collection without a model turn, create a command job. Fill
in an explicit delivery target when needed:

```bash
openclaw cron create "*/30 * * * *" \
  --name "xianyu-monitor" \
  --command-argv '["/absolute/path/xianyu-monitor/.venv/bin/python","scripts/monitor.py","--tasks-file","/absolute/private/path/tasks.json","--task-id","TASK_ID","--state","/absolute/private/path/xianyu-state.json","--quiet-if-empty"]' \
  --command-cwd "/absolute/path/xianyu-monitor" \
  --announce
```

For model analysis, create an isolated agent job instead:

```bash
openclaw cron create "0 */2 * * *" \
  "Use the xianyu-monitor skill to run TASK_ID from /absolute/private/path/tasks.json with --state /absolute/private/path/xianyu-state.json. The user explicitly authorized this recurring job to read that login state solely for Xianyu search; never reveal it. Evaluate task criteria only against captured fields and mark missing evidence uncertain. Report only new listings and all failures. If new_count is zero, return NO_REPLY." \
  --name "xianyu-monitor-analysis" \
  --session isolated \
  --announce
```

Do not use lightweight context for an isolated job that relies on skill
discovery. If that agent cannot launch a Playwright browser, run the complete
monitor command on a trusted browser-owning host and send the agent only its
listing JSON; do not mount the login-state file into that sandbox. Give the
trusted command's task-file parent write access for locks, atomic replacement,
and seen-item persistence. OpenClaw's `{baseDir}`,
`metadata.openclaw`, `NO_REPLY`, delivery flags, and cron syntax are adapter
details and must remain outside the portable core workflow.

## Upgrade strategy

A symlink install tracks the checkout; update it with `git pull`, then reinstall
runtime dependencies if `requirements.txt` changed. A copy install is a
snapshot. Install a new copy into a clean target after reviewing changes rather
than overwriting an unknown directory. Before deleting an old checkout, close
any legacy CDP Chrome and use the guarded cleanup command above for each exact
old initialized temporary profile.
