# croccante — architecture

## Shape

```
                                   ┌─► destination 1
OBS ──► nginx-rtmp (app "live") ───├─► destination 2
         │                         └─► destination 3
         │  exec_publish / exec_publish_done
         ▼
   publisher marker  ◄── polled by one supervisor per destination
```

nginx accepts exactly one publisher. It does **not** push anywhere itself.
Fan-out is done by one long-lived supervisor process per destination, each
running `ffmpeg -c copy` from the local nginx back out to its destination.

## Why supervisors instead of nginx `push`

`push` is fire-and-forget: nginx retries on its own schedule, gives no
visibility into whether a given destination is actually up, and takes every
destination down together when it reconnects. A supervisor per destination
gives independent failure, independent retry with backoff, and a state file per
destination that the healthcheck can reason about.

## Supervisor states

| State      | Meaning                                                        |
|------------|----------------------------------------------------------------|
| `idle`     | No broadcast session yet. Nothing is being pushed.             |
| `relaying` | Publisher live; ffmpeg is copying the stream to this destination. |
| `filler`   | Session open but publisher gone; the filler asset is looped out. |
| `backoff`  | The last attempt failed. Waiting before retrying.              |

Backoff doubles from 1s to `RELAY_BACKOFF_MAX` (default 30s). A relay that ran
for 30 seconds or more is treated as a transient drop and reconnects
immediately rather than backing off — only fast, repeated failures are
throttled.

A publisher disconnect is not a failure. Supervisors return to `idle` and wait.

## State directory layout

`/run/croccante` is root-owned and holds destination URLs, pid files and
supervisor state.

`/run/croccante/hooks` is owned by the **nginx worker user**, because
`exec_publish` hooks run as that user and must be able to write the publisher
marker. Nothing else lives there. This split means a compromised nginx worker
cannot rewrite where the stream is being sent.

nginx discards the stdout of `exec_publish` children, so the hooks log to
`hooks/hooks.log` and the entrypoint tails that file onto the container's
stdout. Without this, hook failures are completely silent — which is exactly
how the permission bug above went unnoticed during development.

## Three constraints that are easy to get wrong

**`worker_processes` must be 1.** nginx-rtmp keeps live stream state per worker
and does not share it between them. With multiple workers the publisher lands on
one worker while a relay's playback request lands on another that has never
heard of the stream, and every relay times out having received no data. The
symptom is `naccepted` incrementing while `nclients` stays at 0.

**`-stimeout` does not exist.** It is an RTSP-only option and was removed in
ffmpeg 6; Alpine 3.21 ships ffmpeg 6.1.2, which rejects it outright with
`Unrecognized option 'stimeout'`. The correct option for bounding a stalled
RTMP socket is `-rw_timeout`.

**Teardown belongs to the supervisor, not the hook.** The nginx hooks run as the
nginx user; relay ffmpeg processes belong to root. A hook cannot signal them.
Each supervisor therefore runs a watchdog alongside its ffmpeg that kills the
child once the publisher marker disappears.

## RTMPS, and why stunnel is gone

This image used to run stunnel as a sidecar to wrap Facebook's RTMPS ingest,
because nginx-rtmp's `push` speaks only plain RTMP.

Now that ffmpeg does the pushing, that indirection is unnecessary: Alpine's
ffmpeg build supports `rtmps://` natively. Verified as follows.

- `ffmpeg -protocols` lists `rtmps`, `tls` and `https`.
- ffmpeg completes a real TLS session — fetching an `https://` URL succeeds,
  which rules out a build without working TLS.
- The smoke harness includes an RTMPS sink (stunnel in *server* mode fronting an
  RTMP recorder) and asserts that a `rtmps://` destination receives real bytes.
  That test passes.

So a Facebook destination is now just another URL:
`rtmps://live-api-s.facebook.com:443/rtmp/<key>`.

Final confirmation against Facebook's real ingest needs a real key and has not
been done — see the open item in [operations.md](operations.md). If it ever
turns out that a specific platform needs stunnel, the generic destination model
already supports it: run stunnel beside the container and point a
`RELAY_DEST_n` at its local port.

## Healthcheck

`healthcheck.sh` reports unhealthy when:

- nginx is not accepting on 1935;
- any supervisor process has died;
- **nginx reports a connected publisher but no publisher marker exists** — the
  signature of a broken `exec_publish` hook, which is otherwise invisible;
- a supervisor still reports `idle` more than 5 seconds after a publisher
  connected;
- a supervisor claims to be `relaying` but has no live ffmpeg.

`idle` with no publisher, and `backoff` against a rejecting destination, are
both healthy. The cross-check against nginx's own `rtmp_stat` (loopback only,
port 8080) is what makes this more than a port probe.

## Timing

| Event                                   | Detection time                |
|-----------------------------------------|-------------------------------|
| Clean publisher disconnect              | ~1s (watchdog poll)           |
| Abrupt publisher loss (cable pulled)    | up to ~10s (`drop_idle_publisher`) |
| Failed relay attempt → retry            | 1s, doubling to 30s           |

## Filler, and the broadcast session

The container runs on the server, not on the machine running OBS. When the
publisher's uplink drops — a captive portal being the painful case — the
publisher leg dies, the relay hits EOF, and without intervention the outbound
leg dies with it and the platform ends the broadcast.

Filler covers that gap. While a session is open but no publisher is connected,
every destination is fed a pre-encoded asset instead of the live input.

**The asset is encoded once**, at container start, by `make-filler.sh`, at the
configured broadcast profile. It is then looped to every destination with
`-c copy`. No encoder runs per destination, so destination count does not cost
CPU, and a destination sees identical codec, resolution, framerate and audio
layout across live → filler → live. A mid-stream parameter change is one of the
things platforms drop a stream for.

### The session boundary

There is no session concept in nginx, so croccante defines one: a session opens
at the **first publish after container start** and never closes on its own.

- Before the first publish, supervisors sit `idle`. Filler never runs — it would
  start a broadcast the operator never asked for.
- After it, a publisher gap is covered by filler indefinitely. There is no
  timeout and no automatic cutoff.
- Ending a broadcast is deliberate and manual: `docker restart croccante`, which
  wipes the state directory and therefore the session.

A control surface for ending a broadcast without SSH is future work.

### Measured behaviour

Filler is a separate ffmpeg invocation from the live relay, so each transition
costs one RTMP reconnect at each destination. Whether that mattered was settled
by measurement rather than argument, against a real YouTube broadcast:

| Transition        | Outbound gap |
|-------------------|--------------|
| live → filler     | 0.18s        |
| filler → live     | 2.40s        |

A single continuous YouTube broadcast survived both transitions and a
five-minute publisher gap, still reporting one uninterrupted stream afterwards.

The alternative — one persistent ffmpeg per destination fed through a FIFO, so
the outbound connection never closes at all — would eliminate the reconnect
entirely but re-architects the fan-out path from pull to push. The measurements
above say it is not needed. Revisit only if a destination is found that does not
tolerate a sub-three-second reconnect.

`test/smoke.sh` measures this gap on every run and fails above five seconds, so
a regression that lengthens a transition is caught rather than discovered live.
