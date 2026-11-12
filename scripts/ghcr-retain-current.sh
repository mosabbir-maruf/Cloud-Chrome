#!/usr/bin/env bash
# Retain only the verified current image version in this linked GHCR package.
set -euo pipefail

: "${GH_TOKEN:?GITHUB_TOKEN is required}"
: "${API_URL:?GitHub API URL is required}"
: "${GHCR_DIGEST:?Published digest is required}"
: "${EXPECTED_SHA_TAG:?Expected SHA tag is required}"
: "${PACKAGE_OWNER:?Package owner is required}"
: "${PACKAGE_NAME:?Package name is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

[[ "$PACKAGE_OWNER" == mosabbir-maruf && "$PACKAGE_NAME" == cloud-chrome ]] || {
  echo "Refusing to operate outside mosabbir-maruf/cloud-chrome." >&2
  exit 1
}
[[ "$GHCR_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "Invalid published digest; refusing cleanup." >&2
  exit 1
}
[[ "$EXPECTED_SHA_TAG" =~ ^sha-[0-9a-f]{40,64}$ ]] || {
  echo "Invalid expected SHA tag; refusing cleanup." >&2
  exit 1
}

api() {
  local method=$1 endpoint=$2 attempt=1 status output retry_after reset_at delay now
  shift 2
  while (( attempt <= 6 )); do
    output=$(mktemp)
    status=$(curl --silent --show-error --output "$output" --write-out '%{http_code}' \
      --dump-header "$output.headers" \
      --request "$method" \
      --header "Authorization: Bearer $GH_TOKEN" \
      --header 'Accept: application/vnd.github+json' \
      --header 'X-GitHub-Api-Version: 2022-11-28' \
      "$@" "$API_URL$endpoint") || status=000
    case "$status" in
      2*) cat "$output"; rm -f "$output" "$output.headers"; return 0 ;;
      429|500|502|503|504|000)
        if (( attempt == 6 )); then
          echo "GitHub API $method $endpoint failed after retries (HTTP $status): $(cat "$output")" >&2
          rm -f "$output" "$output.headers"
          return 1
        fi
        retry_after=$(awk 'BEGIN{IGNORECASE=1} /^Retry-After:/ {gsub("\r", "", $2); print $2}' "$output.headers" | tail -1)
        reset_at=$(awk 'BEGIN{IGNORECASE=1} /^X-RateLimit-Reset:/ {gsub("\r", "", $2); print $2}' "$output.headers" | tail -1)
        rm -f "$output" "$output.headers"
        if [[ "$retry_after" =~ ^[0-9]+$ ]]; then
          delay=$retry_after
        elif [[ "$reset_at" =~ ^[0-9]+$ ]]; then
          now=$(date +%s)
          delay=$((reset_at - now))
          (( delay > 0 )) || delay=60
        else
          delay=$((attempt * attempt * 2))
        fi
        sleep "$delay"
        ;;
      401|403|404)
        echo "GitHub API $method $endpoint returned HTTP $status: $(cat "$output")" >&2
        if [[ "$method" == DELETE && "$status" == 403 ]]; then
          echo "Deletion may be blocked by package Actions access or GitHub's public-version download limit (>5,000 downloads). No PAT will be used." >&2
        elif [[ "$status" == 403 ]]; then
          echo "Check linked package Actions access. No PAT will be used." >&2
        fi
        rm -f "$output" "$output.headers"
        return 1
        ;;
      *)
        echo "Unexpected GitHub API response HTTP $status: $(cat "$output")" >&2
        rm -f "$output" "$output.headers"
        return 1
        ;;
    esac
    attempt=$((attempt + 1))
  done
}

inventory=$(mktemp "$RUNNER_TEMP/cloud-chrome-versions.XXXXXX")
old_ids_file=''
trap 'rm -f "$inventory" "${old_ids_file:-}"' EXIT
fetch_inventory() {
  local page=1 response count
  : > "$inventory"
  while :; do
    response=$(api GET "/user/packages/container/$PACKAGE_NAME/versions?per_page=100&page=$page") || return 1
    jq -e 'type == "array"' <<< "$response" >/dev/null || {
      echo "Unexpected inventory response on page $page; refusing deletion." >&2
      return 1
    }
    count=$(jq 'length' <<< "$response")
    jq -c '.[]' <<< "$response" >> "$inventory"
    (( count == 100 )) || break
    page=$((page + 1))
  done
}

fetch_inventory || { echo "Could not fetch complete package inventory; no versions deleted." >&2; exit 1; }
inventory_count=$(jq -s 'length' "$inventory")
(( inventory_count > 0 )) || { echo "Package inventory is empty; refusing cleanup." >&2; exit 1; }

matches=$(jq -s --arg digest "$GHCR_DIGEST" --arg sha "$EXPECTED_SHA_TAG" \
  '[.[] | select(.name == $digest) | select((.metadata.container.tags // []) | index("latest")) | select((.metadata.container.tags // []) | index($sha))] | length' \
  "$inventory")
(( matches == 1 )) || {
  echo "Expected exactly one package version with digest $GHCR_DIGEST and tags latest,$EXPECTED_SHA_TAG; found $matches. No versions deleted." >&2
  exit 1
}
current_id=$(jq -r -s --arg digest "$GHCR_DIGEST" --arg sha "$EXPECTED_SHA_TAG" \
  '.[] | select(.name == $digest) | select((.metadata.container.tags // []) | index("latest")) | select((.metadata.container.tags // []) | index($sha)) | .id' \
  "$inventory")
[[ "$current_id" =~ ^[0-9]+$ ]] || { echo "Current package version ID is invalid; refusing cleanup." >&2; exit 1; }
old_ids_file=$(mktemp "$RUNNER_TEMP/cloud-chrome-old-ids.XXXXXX")
jq -r -s --arg id "$current_id" '.[] | select((.id | tostring) != $id) | .id' "$inventory" > "$old_ids_file"
old_count=$(wc -l < "$old_ids_file" | tr -d ' ')
echo "Verified current package version ID $current_id; older version count: $old_count"

while IFS= read -r version_id; do
  [[ -n "$version_id" ]] || continue
  [[ "$version_id" =~ ^[0-9]+$ ]] || { echo "Invalid old version ID; refusing further cleanup." >&2; exit 1; }
  [[ "$version_id" != "$current_id" ]] || { echo "Safety guard refused to delete current version." >&2; exit 1; }
  if ! api DELETE "/user/packages/container/$PACKAGE_NAME/versions/$version_id" >/dev/null; then
    echo "Could not delete old version ID $version_id; cleanup is incomplete." >&2
    exit 1
  fi
  echo "Deleted old package version ID $version_id"
done < "$old_ids_file"

fetch_inventory || { echo "Could not fetch final package inventory; cleanup is unverified." >&2; exit 1; }
final_count=$(jq -s 'length' "$inventory")
(( final_count == 1 )) || { echo "Cleanup incomplete: expected one active version, found $final_count." >&2; exit 1; }
jq -e -s --arg digest "$GHCR_DIGEST" --arg sha "$EXPECTED_SHA_TAG" \
  'length == 1 and .[0].name == $digest and ((.[0].metadata.container.tags // []) | index("latest")) and ((.[0].metadata.container.tags // []) | index($sha))' \
  "$inventory" >/dev/null || {
  echo "Cleanup incomplete: final version digest or tags do not match this publication." >&2
  exit 1
}
echo "Verified exactly one package version remains: $GHCR_DIGEST (latest, $EXPECTED_SHA_TAG)."
