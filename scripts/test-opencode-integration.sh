#!/usr/bin/env bash
set -euo pipefail
root="$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")"
# shellcheck source=tests/integration/opencode/versions.env
source "$root/tests/integration/opencode/versions.env"
prefix="tracebase-opencode-test-$$"
# Invoked by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
  for major in 1 2; do
    docker rm -f "$prefix-v$major" >/dev/null 2>&1 || true
    docker image rm "$prefix:v$major" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT
status=0
results=()
for major in 1 2; do
  variable="OPENCODE_V${major}_VERSION"
  version="${!variable}"
  if docker build --build-arg "OPENCODE_VERSION=$version" \
    -f "$root/tests/integration/opencode/Dockerfile" -t "$prefix:v$major" "$root" &&
    docker run --rm --name "$prefix-v$major" --network none \
      --cap-drop ALL --security-opt no-new-privileges "$prefix:v$major"; then
    results+=("V$major ($version): PASS")
  else
    results+=("V$major ($version): FAIL")
    status=1
  fi
done
printf '%s\n' "${results[@]}"
exit "$status"
