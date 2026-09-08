# Recording manifest and session report contract

Croccante owns the evidence of what it captured and attempted to deliver. The
v1 contract is intentionally independent of a recorder process, media
container, storage provider, and destination transport so the runtime work in
[#17](https://github.com/gaulatti/croccante/issues/17) can consume it without
inventing another lifecycle or persistence format.

The machine-readable shape is
[`contracts/recording-session-v1.schema.json`](../contracts/recording-session-v1.schema.json).
`recording_manifest.py` enforces the schema's structural rules plus the ordered
state, timing, checksum, finalization, retry, and privacy rules that JSON Schema
cannot express by itself. Producers and consumers must call
`validate_manifest`; validating against the JSON Schema alone is insufficient.

## Ownership boundary

| Concern | Owner | Recorded by this contract |
|---------|-------|---------------------------|
| Program intent, scheduling, operator authorization, Start/Stop | Alana | No. Alana keeps the program-to-session mapping outside this artifact. |
| As-aired bytes observed at Croccante, segment order, gaps, final artifact | Croccante | Yes: slot-neutral media facts and SHA-256 checksums. |
| Attempted delivery to configured outputs | Croccante | Yes: numbered slots, attempts, final status, and closed error codes. |
| Platform-side playback, archive acceptance, or audience receipt | Destination/platform | No. `accepted` means the transport accepted Croccante's attempt and bytes were observed; it is not a claim about downstream playback. |
| Artifact upload/storage wiring and retention execution | #17 runtime | No runtime is introduced here; v1 defines the evidence that implementation must write and enforce. |

The manifest deliberately has no program ID, operator ID, destination ID,
hostname, URL, stream key, token, signed request, free-form note, or raw error.
`sessionId` must be a generated RFC 4122 version-4 UUID, not a user or program
identifier. This is a producer and privacy invariant: UUID shape alone cannot
prove that a producer did not improperly reuse a UUID-shaped user identifier.
`configurationHash` is the SHA-256 of the already-redacted,
immutable destination selection; destination results use contiguous slots only.

## Version and compatibility

Every document declares exactly
`croccante.recording-session/v1`. All objects are closed: an unknown or missing
field is invalid. Consumers must reject an unknown contract version rather than
guessing. JSON decoders must also reject duplicate keys at every nesting level;
first-key/last-key ambiguity is not a valid interpretation. Any field addition,
semantic change, new state, or new error code
therefore requires a new version and a documented migration. V1 readers remain
valid for the lifetime of retained v1 manifests.

Timestamps are UTC RFC 3339 values ending in `Z`. Durations and offsets are
integer milliseconds. Checksums are lowercase SHA-256 hex. Media container and
codec names are bounded identifiers, not runtime commands or URLs. Artifact
keys are relative `artifacts/segment-NNNNNN.ext` or `artifacts/final.ext`
references. The storage root remains an implementation choice.

## State and finalization

Every manifest starts with transition 1 in `pending`. Transition sequence
numbers are contiguous, timestamps never move backwards, and the top-level
`state` equals the last transition.

| From | Allowed next state |
|------|--------------------|
| `pending` | `recording`, `failed` |
| `recording` | `interrupted`, `finalizing`, `failed` |
| `interrupted` | `recovering`, `finalizing`, `failed` |
| `recovering` | `recording`, `finalizing`, `failed` |
| `finalizing` | `complete`, `partial`, `failed` |

`complete`, `partial`, and `failed` are terminal. An interrupted recorder makes
recovery explicit (`interrupted → recovering → recording`) rather than silently
rewriting history. Interrupted, recovering, partial, and failed transitions
carry a closed `reasonCode`; ordinary progress states do not. A `complete`
session requires verified segments, a final artifact whose duration equals the
elapsed session duration, and successful final status for every
destination. A `partial` or `failed` session requires at least one structured
error. Failed sessions cannot claim a final artifact.

A pending snapshot has no capture progress, source events, dropped frames,
destination attempts, or errors. Nonterminal snapshots cannot claim a final
artifact, and terminal snapshots cannot leave destination status pending. These
state gates prevent a structurally valid object from describing an impossible
lifecycle combination.

Segment numbers are contiguous from one. Offsets are monotonic and cannot
overlap. `capturedDurationMs` equals the sum of segment durations, while
`elapsedDurationMs` reaches at least the end of the last segment and may be
larger when an interruption created a gap. This distinction preserves a crash
without pretending missing media was captured.

When present, the final artifact preserves the elapsed session timeline: its
duration equals `elapsedDurationMs`, including any explicitly unobserved gap.
The finalizer must represent that gap with neutral media rather than collapsing
time. Consequently every source-timeline and session-report offset maps directly
to the same final-artifact offset; `capturedDurationMs` remains the truthful sum
of bytes captured in segments.

`sourceTimeline` begins at offset zero and records up to 64 `live`, `filler`, or
`unobserved` intervals. Adjacent events cannot repeat a mode. Its observed
intervals must match the exact union of segment intervals; an unobserved
interval therefore cannot be disguised as captured media. Reports aggregate
each mode in milliseconds and render every `offset:mode` event, preserving
relay/filler visibility and artifact-relative timecode linkage without including
publisher or program identifiers. The profile records
container plus structured video dimensions/frame rate and audio rate/channels;
an audio-only or video-only fixture is valid, but at least one stream is
required.

## Delivery, errors, and retry evidence

Destination slots are contiguous from one and contain up to 32 ordered
attempts. Before the first attempt, an active session uses `finalStatus: pending`;
a terminal preflight failure uses `not_attempted` and an empty attempt
list. An active `in_progress` attempt has a null end time and makes the final
status pending. Finished attempts are `accepted`, `failed`, or `unknown`.
Accepted means a transport accepted the attempt and Croccante observed a
positive byte count; it does not mean a platform played or archived the media.
Failed and unknown attempts require a closed error code. A destination's final
status must match its last finished attempt. Retries append attempts; they never
replace prior evidence. This preserves separate attempted, in-flight, accepted,
failed, unknown, and not-attempted facts.

Errors contain only sequence, timestamp, phase, closed code, and whether the
condition was retryable. They have no arbitrary message field. V1 codes cover
capture interruption, process crash, disk/storage failure, checksum failure,
finalization failure, and destination failure. Diagnostic logs may hold richer
private detail under their own access and retention rules, but that detail must
not enter the manifest or report.

Error sequences are contiguous and timestamps are monotonic. An error tied to
an interrupted/failed transition or a failed/unknown destination attempt cannot
predate the event that made it observable. Repeated same-code failures are
matched occurrence-by-occurrence to ordered error records, so a later retry
cannot borrow an earlier failure's evidence.

## Retention and privacy

The manifest records one classification (`public-broadcast`, `internal`, or
`restricted`) and either a future `deleteAfter` timestamp or `legalHold: true`.
A legal hold forbids a deletion timestamp. This is an instruction and audit
fact; #17 must make deletion enforcement atomic with artifact ownership and
must preserve the manifest long enough to prove the result. Moving media to a
new storage provider must not change its checksum or silently extend retention.
For a terminal session, `deleteAfter` must be later than `closedAt`, not merely
later than when the session opened.

Before persistence, `validate_manifest(..., artifact_root=...)` checks every
declared file's byte length and SHA-256. Structural validation also rejects
credential-, URL-, raw-error-, and user-identity-bearing field names at any
depth. Runtime implementations must validate before making a manifest durable
and again before publishing a session report.

## Session report

`render_session_report` returns exactly nine newline-terminated lines and at
most 4,096 UTF-8 bytes. It contains only the generated session UUID, lifecycle
state/timing, aggregate capture and live/filler/unobserved timecode facts,
final-artifact presence, aggregate attempted/in-flight/accepted/failed/unknown
destination evidence and observed bytes, closed error-code counts, and
retention facts. It never renders storage keys, configuration hashes,
destination identifiers, URLs,
credentials, user identifiers, or raw errors. Error counts are sorted, so the
same valid manifest always produces identical bytes.

## Contract fixtures

`test/fixtures/recording` includes complete, partial, crashed-and-recovered,
disk-full, destination-failure, active-in-flight, and preflight-failure
manifests. The focused test creates tiny, deterministic, license-free PCM WAV
payloads in a temporary directory, verifies
their declared byte lengths and checksums, and removes them on completion. No
recording media or generated output is committed. Negative tests cover invalid
transitions, missing checksums, sequence/duration inconsistencies, modified
artifact bytes, unsafe fields, and identity-bearing values.

This change does not start capture, write production artifacts, upload media,
alter delivery, or add runtime metrics. #17 remains the sole production-wiring
boundary; when it implements these state transitions, runtime counters should
use only the same closed state/error values and destination slots.
