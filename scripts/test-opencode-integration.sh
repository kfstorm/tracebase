#!/usr/bin/env bash
set -euo pipefail
root="$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")"
# shellcheck source=tests/integration/opencode/versions.env
source "$root/tests/integration/opencode/versions.env"
prefix="tracebase-opencode-test-$$"
comparison="$(mktemp -d /tmp/tracebase-opencode-context.XXXXXX)"
# Invoked by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
  docker rm -f "$prefix-compare" >/dev/null 2>&1 || true
  for major in 1 2; do
    for target in summary integration; do
      docker rm -f "$prefix-$target-v$major" >/dev/null 2>&1 || true
      docker image rm "$prefix:$target-v$major" >/dev/null 2>&1 || true
    done
  done
  rm -rf "$comparison"
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
      options=(--rm --read-only --user "$(id -u):$(id -g)"
      --tmpfs "/tmp:rw,nosuid,size=256m"
      --tmpfs "/home/eval:rw,nosuid,uid=$(id -u),gid=$(id -g),mode=700,size=256m"
      --tmpfs "/work:rw,nosuid,uid=$(id -u),gid=$(id -g),mode=700,size=256m"
      --workdir /work --env "EXPECTED_VERSION=$version"
      --volume "$root/tests/integration/opencode/test_summary_smoke.py:/smoke.py:ro"
      --volume "$root/tests/integration/opencode/test_scripted_provider.py:/test_scripted_provider.py:ro"
      --entrypoint python3)
    fi
    command=()
    [[ "$target" != summary ]] || command=(/smoke.py)
    if docker build --target "$target" --build-arg "OPENCODE_VERSION=$version" \
      -f "$root/src/tracebase/container/Dockerfile" -t "$prefix:$target-v$major" "$context" &&
      docker run --name "$prefix-$target-v$major" --network none \
        --cap-drop ALL --security-opt no-new-privileges "${options[@]}" \
        "$prefix:$target-v$major" "${command[@]}"; then
      results+=("V$major ($version) $target: PASS")
    else
      results+=("V$major ($version) $target: FAIL")
      status=1
    fi
    if [[ "$target" == integration ]]; then
      if ! docker cp "$prefix-$target-v$major:/equivalence-results" "$comparison/v$major"; then
        results+=("V$major Context result export: FAIL")
        status=1
      fi
    fi
  done
done
# Audit copied source evidence and Context again in a fresh, networkless process.
# docker cp avoids any host directory mount in the Collector or comparator.
if docker create --name "$prefix-compare" --network none --cap-drop ALL \
  --user "$(id -u):$(id -g)" \
  --security-opt no-new-privileges --entrypoint python "$prefix:integration-v2" \
  /integration/test_context_equivalence.py compare /comparison/v1 /comparison/v2 >/dev/null &&
  docker cp "$comparison" "$prefix-compare:/comparison" &&
  docker start --attach "$prefix-compare" &&
  [[ "$(docker inspect --format '{{.State.ExitCode}}' "$prefix-compare")" == 0 ]]; then
  results+=("V1/V2 real-session Context equivalence: PASS")
else
  results+=("V1/V2 real-session Context equivalence: FAIL")
  status=1
fi
printf '%s\n' "${results[@]}"
exit "$status"
