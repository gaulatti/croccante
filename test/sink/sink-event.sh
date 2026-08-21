#!/bin/sh
# Test-only. Records outbound connection events so the harness can measure how
# long a destination was disconnected across a filler transition.
#
# busybox date ignores %N, so wall-clock gives only second resolution — far too
# coarse for a sub-second reconnect. /proc/uptime is centisecond-resolution and
# monotonic, which is what this measurement actually needs.
printf '%s %s %s\n' "$(cut -d' ' -f1 /proc/uptime)" "$1" "${2:-}" >> /rec/events.log
