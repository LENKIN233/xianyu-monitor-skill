# Changelog

All notable user-visible changes are recorded here. The project follows
[Semantic Versioning](https://semver.org/).

## 2.0.0-rc.1 - 2026-09-22

This release candidate freezes the documented command/schema surface for final
`2.0.0` validation. Guided setup, installation diagnostics, durable monitoring,
AI analysis, and delivery are all included in 2.0; the old 2.1/2.2 roadmap labels
were work packages. This prerelease does not establish live Xianyu, AI-provider,
or notification-service readiness. Stable promotion requires those smokes.
Formal publication requires the clean exact `v2.0.0-rc.1` tag.

### Breaking

- Direct `search` and `monitor` invocations now reject more than 20 pages or 10
  attempts instead of accepting any positive integer. Split larger authorized
  collection into bounded runs; existing persisted tasks remain readable so they
  can be listed, stopped/resumed, or deleted.
- Task commands now report a missing task file as a structured error instead of
  treating it as an empty task list. Create a task first or pass the intended
  absolute `--data-file` path.

### Added

- `xianyu demo` shows a deterministic synthetic search → validated AI analysis →
  golden evaluation → durable outbox → redacted delivery-preview workflow with
  no credentials, network access, or local writes. Release self-check runs it
  from both the extracted bundle and an empty-HOME copy installation.

- Optional `xianyu analyze` ranking for sanitized search/monitor JSON with
  no-network preview, explicit external-send consent, strict structured output,
  preview-digest binding, retained recorded-item recovery, and
  OpenAI-compatible/Netlify AI Gateway configuration.
- `xianyu state` validates an authorized browser-state candidate locally without
  launching a browser or echoing its path or contents.
- Public roadmap, security policy, and release changelog.
- macOS to the Linux/Windows CI matrix.
- Optional `xianyu setup` orchestration for doctor, visible login handoff, local
  state validation, and a one-page/one-attempt capability test with one final
  path-private JSON result.
- `xianyu version` machine-readable release/capability discovery and offline
  `install --check` health states backed by a per-copy hash manifest.
- Deterministic minimal Skill bundles with payload manifest, SPDX 2.3 SBOM,
  external SHA-256, canonical verification, exact dependency lock, clean/tagged
  release gate, and empty-HOME install smoke tests.
- Digest-bound task update previews, credential-free portable task
  export/import, and stopped/no-state import defaults.
- Schema-3 task stores with durable new-item outbox events, stable idempotency
  keys per delivery generation, baseline suppression, listing-field
  allowlisting, explicit ack, and intentional reset/replay support.
- Digest-gated HTTPS delivery adapters for generic webhooks, Bark, and WeCom;
  endpoint secrets stay in environment variables and provider-confirmed events
  are acknowledged one at a time.
- Reproducible AI run hashes and latency evidence, deterministic offline golden
  regression, and atomic private feedback-record capture.

### Changed

- Tag CI publishes verified bundles and checksums as GitHub Release attachments
  after downloading and verifying them. Release candidates stay prereleases
  without replacing the latest stable release.
- Added v1 upgrade and rollback guidance, including task-store preservation and
  the distinction between portable definitions and a full recovery backup.

- The 13-command capability surface, contract versions, discovery fields, and
  offline demo envelope are frozen by release-candidate compatibility tests.

- Search and newly created tasks now cap pages at 20 and attempts at 10.
- Item URLs are reconstructed from captured IDs on the canonical Goofish origin;
  remote-provided target links are discarded.
- Cookie-header import rejects duplicate and syntactically invalid Cookie names
  before writing a candidate.
- POSIX task persistence fails closed if user-only file permissions cannot be
  established and verified.
- Task CLI missing-file errors are explicit and recoverable; `--data-file` works
  on either side of the subcommand.
- Runtime and CI test dependencies are exact-version locked, and GitHub Actions
  are pinned to full commit SHAs.
- Installation health compares copy payloads with the current source even when
  versions match, rejects unknown nested files, and validates linked payload
  completeness before reporting `current`.
- Guided setup retains child cleanup evidence when the parent is cancelled and
  fails closed as `not-established` when cleanup cannot be proven.

### Security

- Delivery timeouts and cancellation during a send now report possible duplicate
  delivery even when no response was received; pending events remain recoverable.
- Bark/WeCom acknowledgements require integer success codes and reject booleans,
  floating-point values, and strings rather than acknowledging ambiguous replies.
- WeCom text respects its 2048-byte UTF-8 limit while preserving the complete
  item URL and event key. Oversized notification URLs fail before send instead
  of becoming truncated, misleading links.

- AI analysis excludes credentials, paths, seller, image, original URL, and
  unknown input fields; API keys are environment-only, redirects are blocked,
  preview consent is bound to the endpoint/request digest, and model
  identity/output are validated before use.
- Delivery endpoints are never accepted in argv or echoed, redirects are
  blocked, response bodies are not exposed, and uncertain sends/acks retain the
  outbox event with explicit duplicate-risk evidence.
- Browser-state preflight validates privacy and content through one open file
  descriptor and rejects mode, owner, identity, or timestamp changes mid-check.
- Prevented API-controlled listing links from becoming clickable arbitrary or
  executable URLs in downstream Agent output.
- Prevented task files from silently falling back to group/world-readable modes
  when `fchmod` fails.
- Prevented task mutations from silently recreating or overwriting a store that
  was deleted, replaced, or changed after it was loaded.

## 1.0.0 - 2026-08-11

- Unified host-neutral CLI and minimal multi-host Skill distribution.
- Dedicated login capture, deterministic search, persistent monitoring, guarded
  legacy migration cleanup, and structured cancellation/persistence evidence.
