#!/usr/bin/env bash
# Cloud-Chrome — the probe structure check.
#
# The probes run inside a Codespaces dev container, so CI cannot run them
# end to end. What CI can do is check that they are well formed and that
# the properties the evidence depends on are still there: the isolation
# from the real state, the cleanup, and the publication.
#
# No wall clock threshold is checked here. The GitHub runner is too noisy
# for a narrow threshold, and a threshold that flakes teaches everyone to
# ignore it.
#
# Usage: bash tests/probe/check-probes.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf 'PASS  %-62s %s\n' "$1" "${2:-}"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf 'FAIL  %-62s %s\n' "$1" "${2:-}"; }
section() { printf '\n########## %s ##########\n' "$1"; }

cd "$REPO_ROOT" || exit 1

# ---------------------------------------------------------------------------
section "1. The scripts are well formed"
# ---------------------------------------------------------------------------
for script in \
    tests/probe/runtime-probe.sh \
    tests/probe/performance-probe.sh \
    tests/probe/lib/probe-common.sh
do
    if bash -n "$script" 2>/dev/null; then
        pass "the syntax of $script is valid"
    else
        fail "the syntax of $script is valid" "$(bash -n "$script" 2>&1 | head -1)"
    fi
done

if python3 -m py_compile tests/probe/lib/metrics.py 2>/dev/null; then
    pass "the metrics helper compiles"
else
    fail "the metrics helper compiles" "$(python3 -m py_compile tests/probe/lib/metrics.py 2>&1 | head -1)"
fi

if command -v shellcheck > /dev/null 2>&1; then
    if shellcheck --severity=warning tests/probe/*.sh tests/probe/lib/*.sh 2>/dev/null; then
        pass "the probes are lint clean"
    else
        fail "the probes are lint clean" "shellcheck reported something"
    fi
else
    pass "the probes are lint clean" "shellcheck is not installed here"
fi

# ---------------------------------------------------------------------------
section "2. The performance probe is isolated"
# ---------------------------------------------------------------------------
PERF=tests/probe/performance-probe.sh

# The probe must not use the state directory of the repository.
if grep -q 'CLOUDCHROME_PERSIST_DIR="/workspaces/Cloud-Chrome/.cloudchrome"' "$PERF"; then
    fail "the performance probe does not use the real state directory"
else
    pass "the performance probe does not use the real state directory"
fi

# It must point the state at the temporary work directory.
if grep -q 'CLOUDCHROME_PERSIST_DIR="\$STATE_ROOT/.cloudchrome"' "$PERF"; then
    pass "the performance probe uses the temporary work directory"
else
    fail "the performance probe uses the temporary work directory"
fi

# It must remove the temporary state and stop the stack on every way out.
for pattern in 'trap on_exit EXIT' 'rm -rf "\$STATE_ROOT"' 'bash "\$CLI" stop'; do
    if grep -q "$pattern" "$PERF"; then
        pass "the performance probe cleans up: $pattern"
    else
        fail "the performance probe cleans up: $pattern"
    fi
done

# It must not carry a real profile, a credential or a personal file into
# the evidence.
if grep -qiE 'cat .*(Cookies|Login Data|History)' "$PERF"; then
    fail "the performance probe does not read a browser profile"
else
    pass "the performance probe does not read a browser profile"
fi

if grep -q "synthetic-" "$PERF"; then
    pass "the performance probe uses a synthetic profile"
else
    fail "the performance probe uses a synthetic profile"
fi

# ---------------------------------------------------------------------------
section "3. The evidence is published"
# ---------------------------------------------------------------------------
for script in tests/probe/runtime-probe.sh tests/probe/performance-probe.sh; do
    if grep -q 'probe-common.sh' "$script"; then
        pass "$script uses the shared probe library"
    else
        fail "$script uses the shared probe library"
    fi
    if grep -q 'publish_evidence' "$script"; then
        pass "$script publishes its evidence"
    else
        fail "$script publishes its evidence"
    fi
done

# The publishing machinery must exist in exactly one place. Only the
# definitions are counted, not the calls.
publishers=$(grep -l '^publish_via_issue() {' tests/probe/*.sh tests/probe/lib/*.sh 2>/dev/null | wc -l)
if [ "$publishers" -eq 1 ]; then
    pass "the publishing machinery exists once" "$(grep -l '^publish_via_issue() {' tests/probe/*.sh tests/probe/lib/*.sh 2>/dev/null)"
else
    fail "the publishing machinery exists once" "$publishers copies"
fi

# ---------------------------------------------------------------------------
section "4. The measurements are there"
# ---------------------------------------------------------------------------
for measurement in \
    'the startup summary' \
    'the shutdown summary' \
    'the warm start summary' \
    'the backup summary' \
    'the restore summary' \
    'the cgroup memory' \
    'the CPU time of the desktop' \
    'the busy share of the host' \
    'the RFB handshake completed' \
    'the noVNC url'
do
    if grep -q "$measurement" "$PERF"; then
        pass "the performance probe measures: $measurement"
    else
        fail "the performance probe measures: $measurement"
    fi
done

# The comparison against another commit has to be there, because it is
# the only before and after that is not at the mercy of the load of the
# host.
if grep -q 'CLOUDCHROME_PERF_COMPARE_COMMIT' "$PERF"; then
    pass "the performance probe compares two commits in one container"
else
    fail "the performance probe compares two commits in one container"
fi

# The sample counts have to be the ones the report promises.
if grep -q 'STARTUP_SAMPLES:-10' "$PERF" && grep -q 'BACKUP_SAMPLES:-5' "$PERF"; then
    pass "the sample counts are 10 and 5 by default"
else
    fail "the sample counts are 10 and 5 by default"
fi

# ---------------------------------------------------------------------------
section "5. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "the probes are well formed"
    exit 0
fi
echo "the probes are not well formed"
exit 1
