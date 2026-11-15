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
    for target in summary integration; do
      docker rm -f "$prefix-$target-v$major" >/dev/null 2>&1 || true
      docker image rm "$prefix:$target-v$major" >/dev/null 2>&1 || true
    done
  done
}
trap cleanup EXIT
status=0
results=()
for major in 1 2; do
  variable="OPENCODE_V${major}_VERSION"
  version="${!variable}"
  for target in summary integration; do
    context="$root"
    options=()
    if [[ "$target" == summary ]]; then
      # Exercise the installed-package context: no repository files are needed.
      context="$root/src/tracebase/container"
      options=(--read-only --user "$(id -u):$(id -g)"
      --tmpfs "/tmp:rw,nosuid,size=256m"
      --tmpfs "/home/eval:rw,nosuid,uid=$(id -u),gid=$(id -g),mode=700,size=256m"
      --tmpfs "/work:rw,nosuid,uid=$(id -u),gid=$(id -g),mode=700,size=256m"
      --workdir /work --env "EXPECTED_VERSION=$version"
      --volume "$root/tests/integration/opencode/test_summary_smoke.py:/smoke.py:ro"
      --entrypoint python3)
    fi
    command=()
    [[ "$target" != summary ]] || command=(/smoke.py)
    if docker build --target "$target" --build-arg "OPENCODE_VERSION=$version" \
      -f "$root/src/tracebase/container/Dockerfile" -t "$prefix:$target-v$major" "$context" &&
      docker run --rm --name "$prefix-$target-v$major" --network none \
        --cap-drop ALL --security-opt no-new-privileges "${options[@]}" \
        "$prefix:$target-v$major" "${command[@]}"; then
      results+=("V$major ($version) $target: PASS")
    else
      results+=("V$major ($version) $target: FAIL")
      status=1
    fi
  done
done
printf '%s\n' "${results[@]}"
exit "$status"
