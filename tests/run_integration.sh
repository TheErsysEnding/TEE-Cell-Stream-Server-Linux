#!/usr/bin/env bash
# run_integration.sh - end to end on this machine: the headless server against tests/fake_ps3.py.
#
# Uses the server's test capture source and leaves the desktop resolution alone (TEE_CST_TEST_SOURCE=1,
# TEE_CST_NO_DISPLAY_SWITCH=1), and keeps settings and log away from the real ones. Safe on the live desktop:
# the pad drives the server's virtual gamepad only, and no key is typed unless FAKE_PS3_KEY is set.
#
#   tests/run_integration.sh [--duration 8] [--padmode gamepad|mouse] [--keep] [more fake_ps3.py flags]
#   FAKE_PS3_KEY=a tests/run_integration.sh          # also sends KEY a at t=5s (the server TYPES it into the focused window)
#   TEE_CST_INTEGRATION_DIR=/some/dir ...             # where log, settings and stream files go (default: mktemp -d)
#   FAKE_PS3_SESSIONS=1 tests/run_integration.sh      # one session instead of two (see below)
#
# Exit code 0 = fake_ps3 passed, the server exited cleanly on SIGTERM, and its log carries the expected lines.
#
# The server runs on a throwaway settings file, so it streams with the SHIPPED defaults; those are asserted
# twice over - in its own "bereit:" line and, independently, on the wire (fake_ps3 --expect-kbps/--expect-entropy
# reads the bitrate off the packets and the entropy coder out of the PPS). Both must move together, deliberately.
EXPECT_KBPS=6000
EXPECT_ENTROPY=cavlc
RESYNC_INTERVAL_MS=2000   # the console re-syncs its clock every 30s; nothing shorter would run inside a test
# two connect-stream-STOP cycles against ONE server process: the PS3 reconnects whenever it is restarted or
# loses the server for two seconds, and everything a stream turns on (capture child, ffmpeg, audio ffmpeg,
# uinput device) has to be given back in between. a leak shows as a surviving child or a second session that
# never gets its first frame.
SESSIONS="${FAKE_PS3_SESSIONS:-2}"

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# PipeWire/pulse/portal need the session bus even from a bare shell
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export TEE_CST_TEST_SOURCE=1
export TEE_CST_NO_DISPLAY_SWITCH=1

WORK="${TEE_CST_INTEGRATION_DIR:-$(mktemp -d -t tee-cst-integration.XXXXXX)}"
mkdir -p "$WORK" || exit 1
export TEE_CST_SETTINGS_PATH="$WORK/settings.json"
export TEE_CST_LOG_PATH="$WORK/server.log"
SERVER_OUT="$WORK/server.stdout"
rm -f "$TEE_CST_LOG_PATH" "$TEE_CST_SETTINGS_PATH"

echo "== integration: work dir $WORK"

# a server already holding :38310 would make ours give up after 5s, and the fake PS3 would talk to the wrong one
if ss -ulnH 2>/dev/null | grep -qE ':3831[01]\b'; then
    echo "FAIL  udp :38310/:38311 already in use - stop the running server (or fake PS3) first:"
    ss -ulnpH 2>/dev/null | grep -E ':3831[01]\b'
    exit 1
fi

cd "$ROOT" || exit 1
# setsid: the server becomes its own session leader, so `pgrep -s $SERVER_PID` is exactly its process tree -
# capture and ffmpeg children included, and still including them once they are orphaned. matching on command
# lines instead would blame us for another test's ffmpeg (measured: it does, this machine runs several).
setsid env PYTHONPATH=src python3 -m teecellstream --headless >"$SERVER_OUT" 2>&1 &
SERVER_PID=$!
echo "== server pid $SERVER_PID (headless), log $TEE_CST_LOG_PATH"

cleanup() {
    if kill -0 "$SERVER_PID" 2>/dev/null; then
        kill -TERM "$SERVER_PID" 2>/dev/null
        for _ in $(seq 1 50); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 0.1; done
        kill -KILL "$SERVER_PID" 2>/dev/null
    fi
}
trap cleanup EXIT

# up to 20s for the socket: the bind is the server's first step, but a slow encoder probe on a cold GPU is not unheard of
for _ in $(seq 1 200); do
    grep -q 'lausche auf udp' "$TEE_CST_LOG_PATH" 2>/dev/null && break
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.1
done
if ! grep -q 'lausche auf udp' "$TEE_CST_LOG_PATH" 2>/dev/null; then
    echo "FAIL  server did not come up within 20s"
    echo "-- server stdout/stderr:"; cat "$SERVER_OUT" 2>/dev/null
    echo "-- server log:"; cat "$TEE_CST_LOG_PATH" 2>/dev/null
    exit 1
fi
echo "== server listening"

FAKE_ARGS=(--out "$WORK")
# without python3-evdev or a usable /dev/uinput the server cannot open its virtual gamepad and quietly stays on
# the mouse - then the gamepad script's cross press and full stick sweep would land on the REAL desktop. rehearse
# exactly what the server does (VirtualGamepad.try_open), not just the permission bits, and fall back to the
# hands-off mouse script when it does not work (a --padmode given on the command line still wins, it comes later).
GAMEPAD_OK=1
python3 - <<'PY' 2>"$WORK/uinput-probe.txt" || GAMEPAD_OK=0
import evdev
device = evdev.UInput(name="tee-cst-integration-probe")
device.close()
PY
if [ "$GAMEPAD_OK" -ne 1 ]; then
    echo "WARN  the server could not open a virtual gamepad here ($(tail -n 1 "$WORK/uinput-probe.txt" 2>/dev/null)):"
    echo "WARN  it would fall back to mouse and keyboard, so the hands-off pad script is used (--padmode mouse)"
    FAKE_ARGS+=(--padmode mouse)
fi
FAKE_ARGS+=(--expect-kbps "$EXPECT_KBPS" --expect-entropy "$EXPECT_ENTROPY" --resync-interval-ms "$RESYNC_INTERVAL_MS")
if [ -n "${FAKE_PS3_KEY:-}" ]; then FAKE_ARGS+=(--key "$FAKE_PS3_KEY"); fi

# which script the client will actually run (the last --padmode wins, and the caller's comes last)
PADMODE=gamepad
PREVIOUS=""
for argument in "${FAKE_ARGS[@]}" "$@"; do
    [ "$PREVIOUS" = "--padmode" ] && PADMODE="$argument"
    case "$argument" in --padmode=*) PADMODE="${argument#--padmode=}";; esac
    PREVIOUS="$argument"
done
echo "== pad script: $PADMODE"

# everything in the server's session except the server itself
list_children() { pgrep -s "$SERVER_PID" 2>/dev/null | grep -v "^${SERVER_PID}$"; }
FAILED=0
FAKE_RC=0
for session in $(seq 1 "$SESSIONS"); do
    echo "== fake_ps3 session $session/$SESSIONS"
    python3 "$ROOT/tests/fake_ps3.py" "${FAKE_ARGS[@]}" "$@"
    RC=$?
    echo "== fake_ps3 session $session exit code $RC"
    [ "$RC" -eq 0 ] || FAKE_RC=$RC
    sleep 1   # the server tears the stream down after STOP; give it that before asking what it left behind
    IDLE_CHILDREN="$(list_children | tr '\n' ' ')"
    if [ -n "$IDLE_CHILDREN" ]; then
        echo "FAIL  session $session left child process(es) running while the server is idle:$IDLE_CHILDREN"
        ps -o pid=,args= -p "$(echo $IDLE_CHILDREN | tr ' ' ',')" 2>/dev/null | cut -c1-160 | head -n 5
        FAILED=1
    else
        echo "PASS  session $session left no capture/encoder child behind"
    fi
done

# the server must go down cleanly on SIGTERM (exit hooks: desktop back, children killed)
kill -TERM "$SERVER_PID" 2>/dev/null
for _ in $(seq 1 100); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "FAIL  server ignored SIGTERM for 10s, killing it"
    kill -KILL "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    SERVER_RC=137
else
    wait "$SERVER_PID"
    SERVER_RC=$?
fi
echo "== server exit code $SERVER_RC"

echo "-- server log (tail):"
tail -n 60 "$TEE_CST_LOG_PATH"
if [ -s "$SERVER_OUT" ]; then echo "-- server stdout/stderr:"; tail -n 20 "$SERVER_OUT"; fi
echo

expect_log() {   # $1 = regex, $2 = what the line proves
    if grep -qE -- "$1" "$TEE_CST_LOG_PATH"; then
        echo "PASS  log: $2 ('$1')"
    else
        echo "FAIL  log: $2 - no line matching '$1'"
        FAILED=1
    fi
}
reject_log() {   # $1 = regex, $2 = what a matching line would mean
    if grep -qE -- "$1" "$TEE_CST_LOG_PATH"; then
        echo "FAIL  log: $2 - line(s): $(grep -cE -- "$1" "$TEE_CST_LOG_PATH")"
        grep -E -- "$1" "$TEE_CST_LOG_PATH" | head -n 5
        FAILED=1
    else
        echo "PASS  log: no $2"
    fi
}
expect_log "bereit: 1280x720 mit 60 fps, $((EXPECT_KBPS / 1000)) Mbit/s, $(echo "$EXPECT_ENTROPY" | tr '[:lower:]' '[:upper:]')" \
                                'the server streamed with the shipped defaults (bitrate + entropy coder)'
expect_log 'live: erste[rs] Frame' 'the encoder produced a first frame (live_streamer logs "erstes Frame")'
expect_log 'capture: .* gestartet' 'the capture backend came up'
expect_log 'audio: streame'     'the audio streamer announced AINFO and started sending'
expect_log 'pad: gedrückt'      'a pad press was logged (the CP channel works end to end)'
expect_log 'custom 4'           'CUSTOM 4 reached custom_commands (slot 4 is unbound by default)'
expect_log 'Stream beendet'     'the stream stopped on STOP'
STREAMS_ENDED="$(grep -cE 'Stream beendet' "$TEE_CST_LOG_PATH")"
if [ "$STREAMS_ENDED" -ge "$SESSIONS" ]; then
    echo "PASS  log: $STREAMS_ENDED of $SESSIONS stream(s) started and ended (the server serves a reconnect)"
else
    echo "FAIL  log: only $STREAMS_ENDED of $SESSIONS streams ended - the server did not serve every session"
    FAILED=1
fi
# the fake PS3 speaks only what stream.c speaks, so anything the server calls unknown is a protocol drift;
# the other patterns are this codebase's own "something threw where it should not" lines
reject_log 'unbekanntes Paket'  'packet the server did not understand (protocol drift)'
# TEE_CST_NO_DISPLAY_SWITCH is set, so the desktop must not have been touched at all
reject_log 'display: Desktop auf' 'switched desktop resolution (TEE_CST_NO_DISPLAY_SWITCH was set)'
# the full-tilt gamepad script is only safe while the server really drives its virtual pad: if it fell back to
# the mouse, the sweep and the cross press went to the user's desktop instead
if [ "$PADMODE" = gamepad ]; then
    expect_log 'pad: steuert jetzt ein virtuelles Xbox-Gamepad' 'the pad drove the virtual gamepad, not the real mouse'
    reject_log 'kein virtuelles Gamepad verfügbar' 'silent fallback to mouse and keyboard while the gamepad script ran'
fi
reject_log 'Traceback|gestorben|Pumpe abgebrochen|Fehler bei' 'thread death or a swallowed exception'

if [ "$FAKE_RC" -eq 0 ]; then echo "PASS  fake_ps3 exit code 0"; else echo "FAIL  fake_ps3 exit code $FAKE_RC"; FAILED=1; fi
if [ "$SERVER_RC" -eq 0 ]; then echo "PASS  server exit code 0"; else echo "FAIL  server exit code $SERVER_RC (expected 0)"; FAILED=1; fi
# nothing but the banner may reach stdout/stderr: a traceback from a daemon thread lands there, not in the log
if grep -qE 'Traceback|Exception|Fatal' "$SERVER_OUT" 2>/dev/null; then
    echo "FAIL  the server printed a traceback on stdout/stderr:"; cat "$SERVER_OUT"; FAILED=1
else
    echo "PASS  server stdout/stderr free of tracebacks"
fi
# a capture or encoder still running would mean the server did not clean up its children (childproc.kill_all
# and PR_SET_PDEATHSIG). the session id keeps this honest even after the children were orphaned.
LEFTOVERS="$(list_children | tr '\n' ' ')"
if [ -n "$LEFTOVERS" ]; then
    echo "FAIL  child processes survived the server:$LEFTOVERS"
    ps -o pid=,args= -p "$(echo $LEFTOVERS | tr ' ' ',')" 2>/dev/null | cut -c1-160 | head -n 5
    kill -KILL $LEFTOVERS 2>/dev/null   # do not leave them running on the user's desktop
    FAILED=1
else
    echo "PASS  no capture/encoder child survived the server"
fi

if [ "$FAILED" -eq 0 ]; then echo "== INTEGRATION PASS ($WORK)"; else echo "== INTEGRATION FAIL ($WORK)"; fi
exit "$FAILED"
