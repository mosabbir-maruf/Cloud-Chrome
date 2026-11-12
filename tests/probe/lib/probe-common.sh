# shellcheck shell=bash
# Cloud-Chrome — the shared probe helpers.
#
# The probes run inside a Codespaces dev container and publish their
# evidence through the repository API. The publishing machinery lives
# here, so the runtime probe and the performance probe share one
# implementation instead of each carrying its own copy.
#
# The caller sets these before sourcing this file:
#
#   PROBE_SLUG     the name of the probe, used for the work directory
#   PROBE_TITLE    the title of the evidence issue
#   PROBE_EVIDENCE_NAME  the file name used by the git fallback
#   PROBE_OUTPUT   where the evidence is written; empty means a temporary
#                  file that is removed with the work directory
#
# Sourcing this file also redirects the output into the evidence file, so
# everything the probe prints is the evidence.

set -uo pipefail

: "${PROBE_SLUG:?the probe slug is required}"
: "${PROBE_TITLE:?the probe title is required}"
: "${PROBE_EVIDENCE_NAME:=probe-evidence.txt}"
: "${PROBE_OUTPUT:=}"

WORK=$(mktemp -d "/tmp/cloudchrome-${PROBE_SLUG}.XXXXXX")
EVIDENCE_FILE="${PROBE_OUTPUT:-$WORK/evidence.txt}"
mkdir -p "$(dirname "$EVIDENCE_FILE")"
exec > >(tee -a "$EVIDENCE_FILE") 2>&1

PROBE_LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$PROBE_LIB_DIR/../../.." && pwd)
export REPO_ROOT
export PYTHONPATH="$REPO_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
# The CLI is used by the probes that source this file.
# shellcheck disable=SC2034
CLI="$REPO_ROOT/bin/cloudchrome"

section() { printf '\n########## %s ##########\n' "$1"; }
kv() { printf '%-46s %s\n' "$1" "${2:-<empty>}"; }

publish_via_issue() {
    local token="$1" body_file="$WORK/issue.md" json_file="$WORK/issue.json"
    {
        echo "$PROBE_TITLE"
        echo
        echo '```'
        head -c 50000 "$EVIDENCE_FILE"
        echo '```'
    } > "$body_file"
    python3 -c 'import json,sys;print(json.dumps({"title":sys.argv[1],"body":open(sys.argv[2],encoding="utf-8",errors="replace").read()}))' \
        "$PROBE_TITLE" "$body_file" > "$json_file"
    status=$(curl -sS -o "$WORK/issue-resp.json" -w '%{http_code}' -X POST \
        -H "Authorization: Bearer ${token}" \
        -H "Accept: application/vnd.github+json" -H "Content-Type: application/json" \
        --data @"$json_file" \
        "https://api.github.com/repos/${GITHUB_REPOSITORY}/issues" 2>"$WORK/issue-err.txt")
    echo "issue publication HTTP status: ${status}"
    if [ "${status}" = "201" ]; then
        echo "published as an issue: $(jq -r '.html_url' "$WORK/issue-resp.json" 2>/dev/null)"
    else
        echo "issue publication failed: $(head -c 300 "$WORK/issue-resp.json" 2>/dev/null)"
    fi
    rm -f "$body_file" "$json_file" "$WORK/issue-resp.json" "$WORK/issue-err.txt"
    [ "${status}" = "201" ]
}

publish_via_git() {
    # The Codespaces token is a REST API credential, not a git credential,
    # and the container has no repository token by default. The git
    # credentials of the Codespace do work, so the evidence is committed to
    # a branch of the repository.
    #
    # The commit is made on the current branch on purpose: creating a new
    # branch inside the workspace is a bigger operation than the Codespaces
    # credentials are set up for.
    local branch="$1"
    (
        cd "$REPO_ROOT" || exit 1
        git config user.email "codespace@users.noreply.github.com" 2>&1
        git config user.name "Cloud-Chrome probe" 2>&1
        cp "$EVIDENCE_FILE" "$REPO_ROOT/$PROBE_EVIDENCE_NAME" 2>&1
        git add "$PROBE_EVIDENCE_NAME" 2>&1
        if git diff --cached --quiet; then
            echo "nothing to commit: the evidence is unchanged" >&2
            exit 1
        fi
        git commit -q -m "chore(probe): record the ${PROBE_SLUG} probe evidence" 2>&1
        echo "pushing to $branch" >&2
        # The outcome of the push is recorded in the commit message, so a
        # failure says why without a readable log.
        if git push origin "HEAD:refs/heads/${branch}" > "$WORK/push.log" 2>&1; then
            git commit -q --amend -m "chore(probe): record the ${PROBE_SLUG} probe evidence

pushed to ${branch}" 2>&1
            git push -f -q origin "HEAD:refs/heads/${branch}" >/dev/null 2>&1
            echo "pushed to $branch" >&2
            exit 0
        fi
        detail=$(tr '\n' ' ' < "$WORK/push.log" | cut -c1-300)
        git commit -q --amend -m "chore(probe): record the ${PROBE_SLUG} probe evidence

the push failed: ${detail}" 2>&1
        git push -f -q origin "HEAD:refs/heads/${branch}" >/dev/null 2>&1
        echo "the push failed: $detail" >&2
        exit 1
    )
}

# The branch the Codespace was created from. A Codespace checkout can be
# detached, so the upstream tracking branch is used when the current branch
# is not a real branch.
probe_branch() {
    local branch
    branch=$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        branch=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)
        branch="${branch#origin/}"
    fi
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        branch="probe/evidence"
    fi
    printf '%s\n' "$branch"
}

# Publish the evidence, whichever way is available in this container.
publish_evidence() {
    local published=false
    if [ "${CLOUDCHROME_PROBE_PUBLISH:-0}" != "1" ]; then
        echo "publishing not requested"
        return 0
    fi

    local token="${GITHUB_TOKEN:-${PROBE_GIT_TOKEN:-}}"
    local repository="${GITHUB_REPOSITORY:-}"
    if [ -z "$token" ] && command -v gh > /dev/null 2>&1; then
        # The Codespaces image authenticates the gh CLI with the user's
        # token, which is a usable repository credential.
        token=$(gh auth token 2>/dev/null || true)
        repository=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)
    fi
    if [ -n "$token" ] && [ -n "$repository" ]; then
        GITHUB_REPOSITORY="$repository"
        export GITHUB_REPOSITORY
        if publish_via_issue "$token"; then
            published=true
        fi
    else
        echo "no repository token in the container; publishing through git instead"
    fi
    if [ "$published" != "true" ]; then
        local branch="${CLOUDCHROME_PROBE_BRANCH:-$(probe_branch)}"
        if publish_via_git "$branch"; then
            echo "published as a branch: $branch"
            published=true
        else
            echo "the evidence could not be published"
        fi
    fi
    echo "published=$published"
}

# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
# A summary of a set of samples.
#
# The samples are given as one newline separated string. The median is the
# middle value of the sorted list. The p95 is the nearest-rank percentile:
# the value at the ceil(0.95 * n) position of the sorted list, counting the
# first value as position one. The method is stated in the report, because
# a percentile is not a single number and the method has to be known for
# the number to mean anything.
stats_summary() {
    printf '%s\n' "$1" | awk 'NF' | sort -n | awk '
        { v[NR] = $1 }
        END {
            if (NR == 0) { print "count=0"; exit }
            n = NR
            mid = int((n + 1) / 2)
            median = (n % 2 == 1) ? v[mid] : (v[mid] + v[mid + 1]) / 2
            rank = int(0.95 * n)
            if (rank < n) rank += 1
            if (rank < 1) rank = 1
            printf "count=%d min=%g median=%g p95=%g max=%g",
                n, v[1], median, v[rank], v[n]
        }'
}

# The milliseconds since the epoch, with millisecond resolution.
#
# The clock is read with one `date` call, so the measurement itself stays
# cheap: the time the measurement takes is part of the number it reports.
# A `date` that does not know the nanosecond format falls back to whole
# seconds, so the probe still runs where the format is missing.
now_ms() {
    local stamp
    stamp=$(date +%s%N 2>/dev/null)
    case "$stamp" in
        ''|*N*) printf '%s\n' "$(( $(date +%s) * 1000 ))" ;;
        *) printf '%s\n' "$(( stamp / 1000000 ))" ;;
    esac
}
