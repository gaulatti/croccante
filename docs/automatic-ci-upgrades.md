# Automatic Clouseau CI upgrades

Source CI defines “latest” as the commit on protected `gaulatti/clouseau` main
when the isolated `resolve-clouseau` job runs. The pinned public bootstrap has no
repository or branch inputs. It validates protection and the full SHA, checks
out that immutable revision and computes its engine digest. Each engine job
uses those same outputs; a central advance affects the next run only.

Tests, receipt verification, AI review when needed, deterministic scores,
approval and merge use that frozen checkout. Unavailable, malformed or
unauthorized resolution blocks approval with no old-version fallback. Required
native check names, forks, Dependabot and credential separation stay unchanged.
Missing resolver output selects an invalid all-zero commit so checkout cannot
silently use its default branch.
Contributor execution receives no provider or write credentials.

Protected `.github/clouseau.json` authorizes the exact central source instead of
enrolling a new digest per release. Existing signer keys, author binding,
expiry and revocation stay repository-local. Proposed PR configuration cannot
authorize itself. CI compares a signed review's exact engine SHA and computed
digest with its checked-out selection, then independently checks source, base,
policy, configuration, tests and exact 100% line/branch coverage. Matching
eligible evidence uses zero CI AI calls. Missing receipts use normal CI AI
review; invalid or mismatched receipts block without a paid fallback. Refresh
the public local installation, review the current committed source again and
start a fresh workflow. No implicit cross-version compatibility is supported.

Local agents still use an authenticated coding harness without CI keys or
integration. Bastille startup creates no Actions installation, and private
bastille access is unnecessary. Workstation signatures identify enrolled
evidence; they do not defend against a compromised workstation.

The run summary and `clouseau-engine-RUN_ID-RUN_ATTEMPT` artifact identify the
central SHA and engine digest. Acceptance records carry the same identity.
Normal central releases reach new CI runs without consumer pin or digest PRs.
The one-time migration from legacy digest enrollment uses the existing controlled
installation path: publish a passing signed review in the initial ready PR,
confirm its exact head with the owner's `/approve-intent` comment, and run the
owner-guarded installation branch. The current protected-base signer enrollment,
independent PR checks and branch protection remain mandatory. This initial
confirmation does not authorize future arbitrary engines; subsequent runs select
only the protected central source configured by the accepted workflow.
The small bootstrap needs a reviewed update only if its selection protocol
changes. Review, hosted verification, receipt reuse or AI review, approval,
merge and deployment are separate results.

When CODEOWNERS exists on the protected base, automatic merge is reserved for
authors who own every affected path. Other contributions need code-owner
involvement and manual merge; write permission alone is insufficient. Confirmed
absence leaves the optional ownership gate inactive. Tests, review, exact coverage
and branch protection remain mandatory; unreadable or invalid existing ownership
evidence does not disable the gate.
Deployment commands and triggers are unchanged by engine upgrades.
