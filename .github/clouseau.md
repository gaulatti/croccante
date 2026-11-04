# Croccante review contract

Croccante is a single-program RTMP relay appliance: one container combines
nginx-rtmp ingest, per-destination ffmpeg supervisors, a private authenticated
lifecycle and metrics API, and a durable immutable filler store. Alana publishes
one program stream to it; Croccante alone resolves versioned destination secrets
from AWS Secrets Manager and fans the stream out without transcoding.

Preserve the private `broadcast-control` boundary, authenticated Start/Stop with
idempotency keys and increasing sequences, immutable per-session destination and
filler bindings, per-destination failure isolation, publisher-gap filler, and
secret redaction. Stream keys and full destination URLs never appear in APIs,
logs, metrics, process arguments or durable evidence. A command being accepted is
not proof of delivery; supervisor state and real output bytes establish that
result. Startup never invents a prior operator Start.

Assess purpose and evidence rather than the number of changed files or lines.
Bug fixes, meaningful tests that raise coverage, dependency repairs and
operational maintenance can serve this existing appliance without a new product
decision. Protocol correctness, recovery, redaction, bounded metrics and
documented runbooks are established obligations. New capabilities must have
repository documentation or linked acceptance criteria that establish their
actual need and boundaries. The as-aired recording and delivery-report capability
is tracked separately in the repository's issues and is not implied by the
existing manifest contract.

Ask gaulatti for a second opinion for a product-direction or authority decision:
for example turning the relay into a general streaming platform, exposing the
control API publicly, moving stream keys out of Secrets Manager, or changing the
Alcantara to Alana to Croccante control chain. Ordinary implementation
uncertainty and repairable bugs belong to automated investigation, not routine
human code review. Changes to review controls, coverage exclusions, or this
purpose contract require explicit owner confirmation.

Every material capability and mechanism must independently score at least 80 for
fit, need and proportionality under Clouseau's shared protocol. A causal technical
finding at or above 70 blocks; unrelated features or good scores cannot average
away a defect. Integrity and review-completeness gates remain independent.

Tests must assert actual production behavior and failure outcomes. Reject
fabricated coverage, disabled tests, swallowed errors, unconditional assertions,
test-only readiness paths and mocks that replace the behavior being claimed.
Subprocess, signal, socket, clock and filesystem fault injection is legitimate
when the real application contract and failure handling remain exercised. The
real relay smoke harness must separately prove nginx-rtmp ingest, ffmpeg
supervision, filler engagement, destination isolation and parsed bounded metrics.

All tests and exactly 100% application line and branch coverage remain mandatory
for every contribution, including external forks, with no coverage-ignore
directives. Unloaded modules must be measured. Missing or unresolved integrity
evidence holds approval; intent confirmation cannot waive it. Contributor code
never receives provider, repair or approval credentials, and fork PRs cannot
trigger automatic repairs.

Clouseau owns the shared scoring, investigation, repair bounds, evidence ledger,
native approval and auto-merge. This repository owns its purpose, real tests and
runtime evidence. CI/merge, image deployment and live relay health are separate
milestones: merging changes that do not alter the shipped image never deploys.
A production deployment interrupts a live broadcast, so every file that ships in
the image is a protected path: a change to one needs the owner's explicit intent
confirmation before it can merge. A successful merge alone is not a deployment
or delivery claim.

A passing local Clouseau review may be reused only through a signed receipt from
an enrolled trusted workstation and the matching native GitHub author. CI must
independently verify current protected policy, exact head/base and engine content,
expiry, evidence and deterministic scores. This route has no AI credentials and
must not fall back to paid review when verification fails. All native tests,
exact coverage, relay smoke checks and branch protections remain mandatory.
Agent installation and account-authenticated local execution remain independent
of CI enrollment and CI provider keys. A signature authenticates the workstation;
it does not attest against compromise of that workstation or its operator.
