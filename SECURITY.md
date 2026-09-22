# Security policy

## Supported versions

Security fixes target the latest release and the current `main` branch. Older
versions should be upgraded before reporting runtime behavior.

## Reporting a vulnerability

Do not open a public issue containing Cookie values, browser state, proxy
credentials, local private paths, account identifiers, or live listing data.
Prefer GitHub's private vulnerability-reporting flow when it is available for
this repository. Otherwise open a minimal public issue asking the maintainer for
a private contact channel, with no exploit details or secrets.

Include only synthetic reproduction data, the affected version/commit, operating
system, Python version, expected behavior, and security impact. Never attach a
real state file—even encrypted—unless the maintainer has established a separate,
explicitly authorized process.

## Scope

High-priority reports include credential disclosure, unsafe path or symlink
handling, browser isolation failure, arbitrary code or URL execution from listing
data, task-file corruption/races, and false success evidence after failed cleanup
or persistence. They also include AI requests containing fields outside the
documented allowlist, missing consent gates, API-key disclosure, or acceptance of
model output that changes listing identity. Preview/send digest mismatches,
cross-origin Authorization redirects, and failed-run items accepted without
recorded persistence evidence are also in scope. Release checksum/manifest/SBOM
mismatches, unsafe archive entries, non-canonical bundles, dependency-lock drift,
installation-health false positives, and release-gate bypasses are in scope.
Notification endpoint disclosure, redirect following, acknowledgement before
provider-confirmed success, or loss of an outbox event after an uncertain send or
ack are also in scope. AI run-evidence hash drift, automatic copying of listing
text into feedback, or overwriting an existing feedback record are in scope.

Platform risk-control bypasses, CAPTCHA automation, unauthorized account access,
seller spam, purchasing automation, and attacks against Xianyu infrastructure are
out of scope and must not be tested through this project.
