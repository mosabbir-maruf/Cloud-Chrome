#!/usr/bin/env bash
# Cloud-Chrome — the performance probe.
#
# Runs INSIDE a real GitHub Codespaces dev container and measures the
# runtime of the desktop stack. It publishes the measurements as evidence
# through the same mechanism as the runtime probe.
#
# The probe is isolated: it uses a temporary state directory and a
# synthetic profile, so the user's own `.cloudchrome` state is never read,
# copied, backed up or restored. The temporary state is removed on
# success, on failure, on a timeout and on an interruption, and the
# desktop stack it starts is always stopped.
#
# Usage: bash tests/probe/performance-probe.sh
# Output: human-readable evidence on stdout, and to the file named by
#         CLOUDCHROME_PERFORMANCE_OUTPUT (default: a temporary file that is
#         removed on exit).

PROBE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

PROBE_SLUG="performance"
PROBE_TITLE="Cloud-Chrome performance probe evidence"
PROBE_EVIDENCE_NAME="performance-evidence.txt"
PROBE_OUTPUT="${CLOUDCHROME_PERFORMANCE_OUTPUT:-}"

# shellcheck source=tests/probe/lib/probe-common.sh
. "$PROBE_DIR/lib/probe-common.sh"

# The number of samples. The startup and the shutdown are sampled more
# often than the backup and the restore, because they are cheap and are
# the numbers a user feels.
STARTUP_SAMPLES="${CLOUDCHROME_PERF_STARTUP_SAMPLES:-10}"
BACKUP_SAMPLES="${CLOUDCHROME_PERF_BACKUP_SAMPLES:-5}"
IDLE_SECONDS="${CLOUDCHROME_PERF_IDLE_SECONDS:-30}"

# The isolated state. It lives in the work directory, which is removed on
# exit, and it is never the state directory of the repository.
STATE_ROOT="$WORK/state"
export CLOUDCHROME_PERSIST_DIR="$STATE_ROOT/.cloudchrome"
export CLOUDCHROME_RUNTIME_DIR="$CLOUDCHROME_PERSIST_DIR/run"
export HOME="$STATE_ROOT/home"
mkdir -p "$HOME" "$CLOUDCHROME_PERSIST_DIR"

cleanup() {
    # The stack is stopped before the state is removed, so that nothing is
    # left running and nothing is left behind.
    bash "$CLI" stop >/dev/null 2>&1 || true
    rm -rf "$STATE_ROOT"
    rm -rf "$WORK"
}

on_exit() {
    local status=$?
    trap - EXIT
    publish_evidence
    cleanup
    exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
# The PIDs of the supervised components, taken from the supervisor's own
# record. They are read before the stack is stopped, so that the shutdown
# can be measured against exactly those processes.
component_pids() {
    python3 - <<'PY' 2>/dev/null
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

layout = Layout(load_config())
pids = []
try:
    with open(layout.supervisor_state_file, encoding="utf-8") as handle:
        state = json.load(handle)
    for component in state.get("components", {}).values():
        pid = component.get("pid")
        if pid:
            pids.append(int(pid))
except Exception:
    pass

# The browser is a child of the supervisor and the largest process of the
# desktop.
from cloudchrome.lifecycle import find_browser_process
browser = find_browser_process(layout)
if browser is not None:
    pids.append(int(browser[0]))

print(" ".join(str(pid) for pid in pids))
PY
}

# How many of the given PIDs are still alive. A zombie is counted as gone,
# because it holds nothing.
alive_pids() {
    local alive=0 pid state
    for pid in $1; do
        if [ -d "/proc/$pid" ]; then
            state=$(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null || echo "")
            [ "$state" = "Z" ] || alive=$((alive + 1))
        fi
    done
    printf '%s\n' "$alive"
}

# Wait until the desktop is usable, using the runtime's own health check.
wait_until_healthy() {
    python3 - <<'PY'
import os, sys, time
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.config import load_config
from cloudchrome.lifecycle import Lifecycle
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
lifecycle = Lifecycle(config, layout)
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    try:
        if lifecycle.health().get("healthy"):
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.1)
sys.exit(1)
PY
}

# ---------------------------------------------------------------------------
section "1. Environment"
# ---------------------------------------------------------------------------
kv "date (UTC)"        "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kv "the probe"         "performance"
kv "the commit"        "$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
kv "the branch"        "$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
kv "the container"     "${CODESPACES:+Codespaces ${CODESPACES}}" "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-not a Codespace}"
kv "the machine type"  "${CODESPACES_MACHINE_TYPE:-not reported by the container}"
kv "uname -a"          "$(uname -a)"
kv "the kernel"        "$(uname -r)"
kv "the OS"            "$(. /etc/os-release && echo "$PRETTY_NAME")"
kv "the user"          "$(id -un) uid=$(id -u)"
kv "the repository"    "$REPO_ROOT"

python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "tests", "probe", "lib"))
import metrics
facts = metrics.machine_facts()
for key in ("cpu_count", "mem_total_bytes", "page_size"):
    print("%-46s %s" % ("the machine: " + key, facts.get(key)))
print("%-46s %s" % ("the machine: the load average", facts.get("load_average")))
PY

# The versions that are measured, so the numbers can be tied to a build.
kv "the CLI version"    "$(bash "$CLI" version 2>/dev/null | head -1)"
kv "the Chrome version" "$(google-chrome --version 2>/dev/null || echo 'not installed')"

# ---------------------------------------------------------------------------
section "2. The isolation"
# ---------------------------------------------------------------------------
# The probe must never touch the state of the repository. The real state
# directory is named here, and the probe's own one is beside it, so the
# difference is visible in the evidence.
REAL_STATE="/workspaces/Cloud-Chrome/.cloudchrome"
kv "the real state directory" "$REAL_STATE"
kv "the real state exists"   "$([ -d "$REAL_STATE" ] && echo yes || echo no)"
kv "the probe's state"       "$CLOUDCHROME_PERSIST_DIR"
kv "the probe's HOME"        "$HOME"
if [ -e "$REAL_STATE" ] && [ "$REAL_STATE" = "$CLOUDCHROME_PERSIST_DIR" ]; then
    kv "the isolation" "FAILED: the probe uses the real state directory"
else
    kv "the isolation" "ok: the probe uses a temporary state directory"
fi

# The profile is synthetic: it holds a marker and two small files. It
# never holds anything that came from a browser.
bash "$CLI" init >/dev/null 2>&1
kv "the init exit" "$?"
mkdir -p "$CLOUDCHROME_PERSIST_DIR/chrome/Default"
printf 'synthetic-cookie' > "$CLOUDCHROME_PERSIST_DIR/chrome/Default/Cookies"
printf 'synthetic-history' > "$CLOUDCHROME_PERSIST_DIR/chrome/Default/History"
printf 'synthetic-marker' > "$CLOUDCHROME_PERSIST_DIR/chrome/Default/CloudChromePerfMarker"
kv "the synthetic profile" "3 files, no personal data"

# ---------------------------------------------------------------------------
section "3. Startup"
# ---------------------------------------------------------------------------
# Every sample is a full stop, a full start and a health check. The
# measurement is the wall clock time from the command to a desktop that
# reports itself healthy, so it includes the time the runtime needs to
# start the supervisor, the components and Chrome.
startup_samples=""
startup_failures=0
for index in $(seq 1 "$STARTUP_SAMPLES"); do
    bash "$CLI" stop >/dev/null 2>&1 || true
    sleep 1
    started=$(now_ms)
    if bash "$CLI" start > "$WORK/start.log" 2>&1 && wait_until_healthy; then
        finished=$(now_ms)
        elapsed=$((finished - started))
        startup_samples="$startup_samples$elapsed\n"
        printf 'sample %2d  %6d ms\n' "$index" "$elapsed"
    else
        startup_failures=$((startup_failures + 1))
        printf 'sample %2d  FAILED\n' "$index"
        tail -5 "$WORK/start.log" | sed 's/^/      /'
    fi
done
kv "the startup failures" "$startup_failures of $STARTUP_SAMPLES"
kv "the startup summary (ms)" "$(stats_summary "$startup_samples")"

# ---------------------------------------------------------------------------
section "4. Shutdown"
# ---------------------------------------------------------------------------
# Every sample measures the time until the supervisor is gone and every
# desktop process has exited. The PIDs are read from the supervisor's own
# record before the stop, so the measurement is against exactly the
# processes that were started.
shutdown_samples=""
shutdown_failures=0
for index in $(seq 1 "$STARTUP_SAMPLES"); do
    bash "$CLI" start >/dev/null 2>&1 || true
    wait_until_healthy || true
    pids=$(component_pids)
    started=$(now_ms)
    bash "$CLI" stop > "$WORK/stop.log" 2>&1
    deadline=$(( $(now_ms) + 30000 ))
    while [ "$(alive_pids "$pids")" != "0" ] && [ "$(now_ms)" -lt "$deadline" ]; do
        sleep 0.05
    done
    finished=$(now_ms)
    elapsed=$((finished - started))
    left=$(alive_pids "$pids")
    if [ "$left" = "0" ]; then
        shutdown_samples="$shutdown_samples$elapsed\n"
        printf 'sample %2d  %6d ms  %s processes\n' "$index" "$elapsed" "$(echo "$pids" | wc -w)"
    else
        shutdown_failures=$((shutdown_failures + 1))
        printf 'sample %2d  %6d ms  %s processes left\n' "$index" "$elapsed" "$left"
    fi
done
kv "the shutdown failures" "$shutdown_failures of $STARTUP_SAMPLES"
kv "the shutdown summary (ms)" "$(stats_summary "$shutdown_samples")"

# ---------------------------------------------------------------------------
section "5. The warm start"
# ---------------------------------------------------------------------------
# The warm start is the start command when the stack is already up. It
# must do nothing, and it must be fast.
bash "$CLI" start >/dev/null 2>&1 || true
wait_until_healthy || true
warm_samples=""
for index in $(seq 1 "$STARTUP_SAMPLES"); do
    started=$(now_ms)
    bash "$CLI" start --if-needed > "$WORK/warm.log" 2>&1
    finished=$(now_ms)
    elapsed=$((finished - started))
    warm_samples="$warm_samples$elapsed\n"
    printf 'sample %2d  %6d ms\n' "$index" "$elapsed"
done
kv "the warm start summary (ms)" "$(stats_summary "$warm_samples")"

# ---------------------------------------------------------------------------
section "6. The memory"
# ---------------------------------------------------------------------------
# The desktop is left running, and the measurement is taken after it has
# settled, so the numbers are of a desktop at rest.
sleep 5
CLOUDCHROME_PERF_PIDS="$(component_pids)"
export CLOUDCHROME_PERF_PIDS
python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "tests", "probe", "lib"))
import metrics

cgroup = metrics.cgroup_memory()
if cgroup["available"]:
    print("%-46s %s bytes" % ("the cgroup memory", cgroup["current_bytes"]))
    print("%-46s %s" % ("the cgroup memory source", cgroup["source"]))
    print("%-46s %s bytes" % ("the cgroup peak memory", cgroup["peak_bytes"]))
    print("%-46s %s bytes" % ("the cgroup anonymous memory", cgroup["anonymous_bytes"]))
    print("%-46s %s bytes" % ("the cgroup file memory", cgroup["file_bytes"]))
    print("%-46s %s bytes" % ("the cgroup swap", cgroup["swap_bytes"]))
else:
    print("%-46s unavailable" % "the cgroup memory")
    print("%-46s %s" % ("the reason", cgroup.get("note")))

pids = os.environ.get("CLOUDCHROME_PERF_PIDS", "").split()
memory = metrics.process_memory([int(pid) for pid in pids if pid.isdigit()])
if memory["pss_available"]:
    print("%-46s %s bytes" % ("the total PSS of the desktop", memory["total_pss_bytes"]))
else:
    print("%-46s unavailable" % ("the total PSS of the desktop"))
print("%-46s %s bytes" % ("the total RSS of the desktop", memory["total_rss_bytes"]))
for process in memory["processes"]:
    print("%-46s pid=%s pss=%s rss=%s" % (
        "  a process of the desktop", process["pid"],
        process["pss_bytes"], process["rss_bytes"]))
PY

# ---------------------------------------------------------------------------
section "7. The idle CPU"
# ---------------------------------------------------------------------------
# The desktop is idle while this is measured. The sample is the share of
# the machine that was busy over the interval.
python3 - "$IDLE_SECONDS" <<'PY'
import os, sys, time
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "tests", "probe", "lib"))
import metrics

seconds = float(sys.argv[1])
first = metrics.cpu_snapshot()
started = time.monotonic()
time.sleep(seconds)
second = metrics.cpu_snapshot()
elapsed = time.monotonic() - started
usage = metrics.cpu_usage_between(first, second, elapsed)
if usage is None:
    print("%-46s unavailable" % "the idle CPU")
else:
    print("%-46s %s" % ("the idle interval (s)", round(elapsed, 1)))
    print("%-46s %.4f" % ("the busy share of the machine", usage["busy_fraction"]))
    print("%-46s %s of %s jiffies" % (
        "the busy jiffies", usage["busy_jiffies"], usage["total_jiffies"]))
    print("%-46s %.2f%%" % ("the busy share in percent", usage["busy_fraction"] * 100))
PY

# ---------------------------------------------------------------------------
section "8. The noVNC client and the RFB server"
# ---------------------------------------------------------------------------
python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "tests", "probe", "lib"))
import metrics

latency = metrics.http_latency(samples=10)
print("%-46s %s" % ("the noVNC url", latency["url"]))
print("%-46s %s" % ("the HTTP status", latency["status"]))
print("%-46s %s" % ("the failed requests", latency["failures"]))
samples = "\n".join("%.3f" % value for value in latency["samples_ms"])
print("%-46s %s" % ("the latency samples (ms)", samples))
if latency["samples_ms"]:
    ordered = sorted(latency["samples_ms"])
    middle = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[middle]
    else:
        median = (ordered[middle - 1] + ordered[middle]) / 2
    print("%-46s %.3f" % ("the median latency (ms)", median))
    print("%-46s %.3f" % ("the slowest latency (ms)", ordered[-1]))

rfb = metrics.rfb_handshake()
print("%-46s %s" % ("the RFB endpoint", "%s:%s" % (rfb["host"], rfb["port"])))
print("%-46s %s" % ("the RFB protocol version", rfb["version"]))
print("%-46s %s" % ("the RFB security types", rfb["security_types"]))
print("%-46s %s" % ("the RFB handshake completed", rfb["completed"]))
if rfb["error"]:
    print("%-46s %s" % ("the RFB error", rfb["error"]))
print("%-46s %s" % (
    "the RFB note",
    "the handshake says the port speaks RFB; it does not say that a "
    "browser can draw the desktop"))
PY

# ---------------------------------------------------------------------------
section "9. The backup"
# ---------------------------------------------------------------------------
# The backup is taken against the synthetic profile, in the isolated state
# directory. It is never the user's own state.
backup_samples=""
backup_sizes=""
backup_members=""
backup_failures=0
for index in $(seq 1 "$BACKUP_SAMPLES"); do
    started=$(now_ms)
    if bash "$CLI" backup --label "performance sample $index" > "$WORK/backup.log" 2>&1; then
        finished=$(now_ms)
        elapsed=$((finished - started))
        archive=$(sed -n 's/^backup written: //p' "$WORK/backup.log")
        size=$(stat -c '%s' "$archive" 2>/dev/null || echo 0)
        members=$(sed -n 's/^members: *//p' "$WORK/backup.log")
        backup_samples="$backup_samples$elapsed\n"
        backup_sizes="$backup_sizes$size\n"
        backup_members="$backup_members$members\n"
        printf 'sample %2d  %6d ms  %8s bytes  %s members\n' \
            "$index" "$elapsed" "$size" "$members"
    else
        backup_failures=$((backup_failures + 1))
        printf 'sample %2d  FAILED\n' "$index"
        tail -3 "$WORK/backup.log" | sed 's/^/      /'
    fi
done
kv "the backup failures" "$backup_failures of $BACKUP_SAMPLES"
kv "the backup summary (ms)" "$(stats_summary "$backup_samples")"
kv "the archive size summary (bytes)" "$(stats_summary "$backup_sizes")"
kv "the member count summary" "$(stats_summary "$backup_members")"

# The archive is encrypted, and it holds no personal data.
archive=$(sed -n 's/^backup written: //p' "$WORK/backup.log")
if [ -n "$archive" ] && [ -f "$archive" ]; then
    kv "the archive is encrypted" "$(head -c 2 "$archive" | od -An -tx1 | tr -d ' \n')"
    kv "the archive mode" "$(stat -c '%a' "$archive")"
    members=$(age --decrypt --identity \
        "$CLOUDCHROME_PERSIST_DIR/keys/backup-identity.txt" "$archive" 2>/dev/null \
        | tar -tzf - 2>/dev/null | sort)
    printf '%s\n' "$members" | sed 's/^/  member: /'
fi

# ---------------------------------------------------------------------------
section "10. The restore"
# ---------------------------------------------------------------------------
restore_samples=""
restore_failures=0
restore_healthy=0
for index in $(seq 1 "$BACKUP_SAMPLES"); do
    archive=$(ls -1t "$CLOUDCHROME_PERSIST_DIR/backups"/*.tar.gz.age 2>/dev/null | head -1)
    if [ -z "$archive" ]; then
        restore_failures=$((restore_failures + 1))
        printf 'sample %2d  FAILED: no backup\n' "$index"
        continue
    fi
    started=$(now_ms)
    if bash "$CLI" restore --source "$archive" --yes > "$WORK/restore.log" 2>&1; then
        finished=$(now_ms)
        elapsed=$((finished - started))
        restore_samples="$restore_samples$elapsed\n"
        if wait_until_healthy; then
            restore_healthy=$((restore_healthy + 1))
            printf 'sample %2d  %6d ms  healthy\n' "$index" "$elapsed"
        else
            printf 'sample %2d  %6d ms  NOT healthy\n' "$index" "$elapsed"
        fi
    else
        restore_failures=$((restore_failures + 1))
        printf 'sample %2d  FAILED\n' "$index"
        tail -3 "$WORK/restore.log" | sed 's/^/      /'
    fi
done
kv "the restore failures" "$restore_failures of $BACKUP_SAMPLES"
kv "the healthy restores" "$restore_healthy of $BACKUP_SAMPLES"
kv "the restore summary (ms)" "$(stats_summary "$restore_samples")"

# ---------------------------------------------------------------------------
section "11. The image"
# ---------------------------------------------------------------------------
# The image size and the build duration are measured by the CI build job,
# not from inside the container. The container reports what it can.
kv "the image" "the container runs the image; its size is measured by the CI build job"
kv "the Chrome version" "$(google-chrome --version 2>/dev/null || echo 'not installed')"
kv "the disk used by the state" "$(du -sh "$CLOUDCHROME_PERSIST_DIR" 2>/dev/null | cut -f1)"
kv "the disk used by the workspace" "$(du -sh "$REPO_ROOT" 2>/dev/null | cut -f1)"

# ---------------------------------------------------------------------------
section "12. The summary"
# ---------------------------------------------------------------------------
kv "the startup samples"   "$STARTUP_SAMPLES"
kv "the shutdown samples"  "$STARTUP_SAMPLES"
kv "the warm start samples" "$STARTUP_SAMPLES"
kv "the backup samples"    "$BACKUP_SAMPLES"
kv "the restore samples"   "$BACKUP_SAMPLES"
kv "the idle interval"     "${IDLE_SECONDS}s"
kv "the method" "the median is the middle value of the sorted samples; the p95 is the nearest-rank percentile"
kv "the note" "the numbers are the runtime of the container; the time GitHub takes to provision a Codespace is not measured here"

printf '\n########## END OF EVIDENCE ##########\n'
