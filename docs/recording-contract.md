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
| Capture leg, segment verification, finalization, quota and retention | `as_aired_capture.py` ([#17](https://github.com/gaulatti/croccante/issues/17)) | Yes: it writes only manifests that pass `validate_manifest`. Wiring it into Start/Stop, the image and metrics is separate producer/consumer work. |

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

## As-aired capture runtime (`as_aired_capture.py`)

The recorder is always on: the image contains `as_aired_capture.py`,
`capture_runtime.py` and `recording_manifest.py`, the lifecycle API opens a session
for every Start, and there is no setting that turns capture off (the storage bounds
below are the safety). It records Croccante's final relay/filler output as a separate
as-aired artifact; it never reads, writes, or replaces Alana's clean program
master.

### Wiring (`capture_runtime.py`, `control-server.py`)

- **Start** hands the runtime the prepared broadcast profile (the filler's width, height,
  frame rate and audio format, which is what the relay carries), the number of destination
  slots and the destination selection hash. The session **opens when media first goes on
  air**, so the time before a publisher or filler is `unobserved`, not recorded as silence.
- **Source.** While the publisher is live the capture reads the local relay input
  `rtmp://127.0.0.1:1935/<app>/<stream>`; when the relay falls back to filler it reads the
  active filler asset as a local file (real time, looped, as the destination supervisors
  read it); back to live, it reads the input again. `AsAiredCapture.retarget` swaps the
  capture process without ending the session, and `note_source("live"|"filler")` records the
  transition.
- **Destinations.** Once a second the runtime reads each slot's relay state: `relaying` or
  `filler` opens an attempt, `backoff` closes it `failed` (`destination_unavailable`). Stop
  closes open attempts against the final byte count: `accepted` when the relay put bytes on
  air during the attempt, `unknown` otherwise. `observedBytes` is the as-aired bytes counted
  at Croccante while the attempt was open, not an acknowledgement by the destination.
- **Stop** closes the session; joining the segments runs off the request, so a long recording
  does not delay the Stop response. A capture process that dies is `interrupted`
  (`process_crashed`) and is restarted by the same once-a-second tick; the outage stays an
  `unobserved` gap and the session ends `partial`.
- **Isolation.** Every call into the capture is guarded: a fault is counted
  (`croccante_as_aired_capture_recorder_failures_total`) and Start, Stop and delivery go on.
  Only slot numbers are written for destinations.
- **Storage.** Sessions live under `/var/lib/croccante/as-aired` (a bind mount, like the
  fillers, in the deploy workflow). Bounds are constants in `capture_runtime.py`: a 50 GiB
  store (`maxTotalBytes`), a 50 GiB session (`maxSessionBytes`), sessions kept 7 days (168 h,
  `retentionDays`), 6 s segments, classification `internal`. Worst case: the copy has the
  relay's bitrate, about 2.1 GB per hour at 4.6 Mbit/s, so the store holds roughly 24 hours;
  opening a session removes expired and then the oldest finished sessions first, and a session
  that reaches its size ends `partial` with `disk_full`.
- **Metrics** (the private `/metrics`, bounded labels only):
  `croccante_as_aired_capture_state{state}` (one set of a closed vocabulary, `unavailable`
  when there is no session), `croccante_as_aired_capture_segments`,
  `croccante_as_aired_capture_bytes` and `croccante_as_aired_capture_recorder_failures_total`.
- **Known gap:** a container that dies mid-broadcast leaves that session non-terminal on disk;
  retention never deletes non-terminal sessions, so such a session keeps its bytes until it is
  removed by hand. Tracked as a follow-up.

- **Capture leg.** `build_capture_command` is `ffmpeg -c copy` into numbered
  MPEG-TS segments under `<root>/<sessionId>/artifacts/`. The source must be an absolute local path or a credential-free `rtmp://`
  URL whose host is exactly `127.0.0.1`, `localhost` or `::1`, never a destination. A segment is
  registered only after the next segment exists (or the process ended), so a
  half-written segment is never verified.
- **Verification.** Each registered segment gets a SHA-256, byte length and an
  ffprobe duration, and its video/audio codecs must equal the declared profile.
  A mismatch discards the segment and closes the session `partial`. A segment
  that cannot be probed is discarded as a visible `unobserved` gap (the session
  can then only end `partial`) and later segments are renamed so artifact keys
  stay contiguous. Finalization
  concatenates the verified segments to `artifacts/final.ts`, re-hashes it, and
  compares its size to the sum of the segments.
- **Transitions and delivery.** `note_source("live"|"filler")` records relay and
  filler intervals (live until the first filler is noted); time not covered by a
  segment is `unobserved`. `begin_destination_attempt` / `end_destination_attempt`
  record numbered slots with `accepted`, `failed`, `unknown` or in-progress
  states. The slot number is the only destination identity written.
- **Crash tolerance.** If the capture process dies, the session becomes
  `interrupted` with `process_crashed`; `recover` restarts numbering where it
  left off and the outage stays an `unobserved` gap. A session with a gap, no
  segments, a failed final artifact, a quota stop, a codec change, or a failed or
  unattempted destination ends `partial`, never `complete`. Failed sessions never
  carry a final artifact.
- **Evidence bounds.** The v1 caps (64 errors, transitions and source events,
  32 attempts per slot) are never exceeded: when a session approaches one it
  stops recording history and closes `partial` with `capture_interrupted`, so a
  long broadcast with a flapping destination or publisher still ends with a
  valid terminal manifest and report.
- **Failure isolation.** `AsAiredCapture` never raises: every relay callback is
  guarded, failures are counted (`snapshot()["recorder_failures"]`), a close that
  cannot finalize writes a `failed` manifest, and an invalid manifest is never
  written (the previous valid one stays). The report is
  `render_session_report` of the validated manifest, written next to it.
- **Quota and retention.** `maxSessionBytes` stops capture at the first segment
  that would exceed it (`disk_full`); `maxTotalBytes` is checked before a session
  records (a full store yields a `failed` preflight manifest). Opening a session
  first runs `apply_retention`, which deletes expired terminal sessions and then
  the oldest terminal sessions over the total quota. Legal holds, non-terminal
  sessions and directories that are not valid sessions are never deleted.
- **Observability.** `snapshot()` is the bounded surface (state, segment count,
  bytes, failure count). Prometheus exposition belongs to the wiring work in
  `relay_metrics.py`.

Offsets of relay/filler transitions come from the host clock and the segment
timeline from ffprobe durations, so a transition is accurate to the clock skew
between them, not to a frame.

Declared profile: the capture declares the broadcast profile; a live publisher whose stream
differs (another frame rate or size) has its segment discarded and the session ends `partial`
(`capture_interrupted`). Not verified: a long soak against the real relay host.
