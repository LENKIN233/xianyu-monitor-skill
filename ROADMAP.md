# Roadmap

## Product thesis

Xianyu Monitor should be the safest and easiest read-only Xianyu Agent Skill,
not a hidden trading bot or a second Web dashboard. Its advantage is a small
local runtime with explicit credential boundaries, deterministic JSON, portable
Agent Skills instructions, optional consent-gated AI analysis, and failure
evidence that schedulers can trust.

## Decision principles

1. **Prove before claiming.** Separate candidate validity, search capability,
   authentication, identity, persistence, and cleanup evidence.
2. **One-command discoverability.** Every normal workflow is reachable through
   `scripts/xianyu.py`; errors name a concrete `next_action` where practical.
3. **Read-only by default.** Search and monitor never message, purchase, publish,
   rotate identity, or bypass platform controls.
4. **Local secrets stay local.** State is consumed only by exact authorized paths
   and never becomes Agent context, telemetry, fixtures, or support output.
5. **Small core, open adapters.** Scheduling stays host-owned; delivery uses an
   optional, host-neutral outbox/adapter layer. Search remains AI-independent
   and optional analysis needs no SDK or database.
6. **Regression evidence first.** Security boundaries and error semantics require
   tests on Linux, macOS, and Windows before release.

## Iteration method

Each release follows the same loop:

1. Collect real onboarding friction, failure JSON, issues, and ecosystem changes
   without collecting credentials.
2. Rank work by user value, boundary risk, frequency, and maintenance cost.
3. Write observable acceptance criteria and synthetic regression tests first.
4. Make the smallest host-neutral runtime change that satisfies them.
5. Run lint, formatting, all offline tests, CLI smoke tests, and documentation
   checks; use a user-authorized live smoke test only when the site contract changed.
6. Record the behavior in the changelog and remove obsolete paths instead of
   accumulating hidden compatibility behavior indefinitely.

## Current release scope

### 2.0 — Search, monitoring, optional analysis and delivery (RC validation)

`2.0.0-rc.1` includes all implemented capabilities below. Earlier roadmap labels
2.1 and 2.2 described work packages; those features ship together in 2.0 and are
not separate released versions. The public command surface remains 13 commands.

- Local `state` validation and actionable next steps.
- Canonical safe item links, bounded collection work, strict Cookie import, and
  fail-closed task permissions.
- Three-platform CI and clearer Skill metadata.
- Optional structured AI ranking with preview, field allowlist, explicit consent,
  and provider-independent failure evidence.
- A zero-network synthetic demo proves the complete product flow before login;
  the release self-check repeats it after empty-HOME installation.
- Optional guided `setup` command that composes doctor → login handoff → state
  check → one-page capability test while preserving user confirmation boundaries.
- Machine-readable `version` and capability discovery.
- Installation health and update diagnostics that do not overwrite existing Skills.
- Reproducible release bundle with checksums, SBOM, pinned CI actions, and an
  install-from-empty smoke test.
- Task update/export/import commands with digest-bound diff previews; exports
  exclude credentials/history and imports are stopped without state paths.
- A task-store-atomic durable outbox with stable idempotency keys, plus
  digest-gated Bark/Webhook/WeCom delivery whose endpoints stay environment-only.
- Human-readable notification text built from sanitized outbox events; an
  independent offline result-summary command is not part of this release.
- Reproducible AI run evidence (`model`, prompt/schema/input hashes, usage and
  latency), golden-dataset regression, and feedback capture.

Acceptance:

- Setup stays optional and fail-fast with one final path-private JSON;
  version/capability discovery and installation health are offline/read-only.
- Legacy task definitions and seen IDs survive schema migration; baseline runs
  enqueue no notifications; new-item recording and outbox writes are atomic.
- Adapter previews bind the endpoint hash and exact event bodies; redirects are
  blocked; only provider-confirmed success permits local ack. Uncertain sends
  and acknowledgements expose duplicate risk and preserve recovery evidence.
- AI output carries stable run hashes, golden regression is offline, and
  feedback is written once as a private minimal record without listing text.
- Linux/macOS/Windows CI validates Python 3.10/3.12, reproducible bundles,
  archive/manifest/SBOM/checksum verification, and empty-HOME copy installation.
- Formal bundles come from a clean, exactly tagged commit. GitHub Release
  attachments are downloaded and verified before publication; RCs are marked
  prerelease and do not replace the latest stable release.

Stable-release exit criteria additionally require one user-authorized real
one-page search plus preview-bound calls to the selected AI provider and delivery
adapter. Only sanitized outcomes are retained. Offline or simulated success does
not establish external-service readiness; untested adapters remain identified.

## Next candidates

### 2.1 — Listing relevance and controlled research (not implemented)

- Explainable exclusion of accessories, rentals, wanted listings, and placeholder
  prices, with explicit evidence for each exclusion.
- Controlled multi-query research with deduplication, saturation and coverage
  evidence, and checkpoint/resume.
- Detail verification and total-cost normalization only where captured evidence
  supports them.

### Later — History and integration

- Price and availability history without claiming platform-wide coverage.
- Published JSON schema documents and broader downstream compatibility fixtures;
  2.0 already exposes contract version identifiers and compatibility tests.
- A thin optional MCP façade generated from the same command contracts, without
  exposing login capture, raw credentials, purchasing, or messaging tools.
- Deprecation policy and signed release artifacts.

## Explicit non-goals

- CAPTCHA, risk-control, rate-limit, or authentication bypass.
- Automatic seller messaging, ordering, payment, publishing, or account rotation.
- Mandatory AI/API keys, a hosted credential service, or uploading browser state
  for analysis or diagnostics.
- Scraping cadence below the documented safety interval.
