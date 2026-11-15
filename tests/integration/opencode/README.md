# OpenCode integration tests

From the repository root, run:

```bash
./scripts/test-opencode-integration.sh
```

You need Docker and network access during image builds. The script builds and
tests the `summary` and `integration` targets for both versions listed in
`versions.env`, reports all four results, and returns success only when all
pass. A failed build or test does not skip the remaining targets or Major.
GitHub Actions invokes this same command. Update
the pins in `versions.env` to test newer releases; runtime support does not use
these pins.

Each test container has its own HOME, XDG directories, and database. Runtime
networking is disabled. Collector containers have no host mounts; Summary smoke
containers mount only the repository's smoke script, read-only. Neither receives
host credentials or server configuration. The EXIT trap removes test containers
and image tags; Docker may retain reusable build layers.

## Shared contract

The same scenarios and assertions exercise both real executables: discovery
across two Git projects and directories, root/child and archived sessions,
half-open collection and message-time boundaries, complete native evidence,
text-only offline Context, non-dialogue-only activity, archive-only updates,
repeat observations and Snapshot selection, and rejection of a failed hydration
after another Snapshot has been staged.

The suite also checks V1 credential-file initialization and a V2 official
`auth import` / `auth export` round trip between isolated states using a synthetic
key. Summary version selection, credential failures, and orchestration have
separate unit tests.

V2 runs with a two-record page limit in the test process to exercise real
session and message pagination without hundreds of fixtures. API responses are
compared byte for byte with preserved `session.json` and Message pages. Each
Snapshot contains only its own `session.get` response and Message pages. Global
discovery statistics, requests, page counts, and observation times are checked in
Run Coverage. V1 exercises its full export;
its discovery endpoint does not expose a continuation contract Tracebase can
safely follow, so a continuation header fails collection. That rejection is
covered by unit tests, not a claimed real-server pagination test.

Fixtures use upstream import operations with synthetic transcripts and never
call a model. Imports can reset Session update times, so initialization fixes
timestamps in the disposable container database before collection. The fixture
checks the Docker marker and isolated data path before opening SQLite. The
collector never reads SQLite. A late-hydration failure deletes a fixture Session
between discovery and its real export/API request.

## Shared Dockerfile and Summary smoke

`src/tracebase/container/Dockerfile` installs the requested official GitHub
Release binary once for Linux x64 or arm64. Its `summary` target contains
OpenCode, Node.js/npm, Python 3.14, uv, curl, and CA certificates. The `integration`
target adds Git, Tracebase, and test dependencies. Summary builds use only the
packaged `container/` directory as their context; integration builds use the
repository root. Test versions come from `versions.env`, not the Dockerfile.

The entry point builds each Summary target from the package-sized context and
runs it with a read-only root filesystem, an unprivileged UID, private tmpfs
state, dropped capabilities, and no network. The smoke script checks the version,
CLI arguments, and Root/Recovery session reuse. A scripted HTTP provider on
container loopback exercises a real Subagent tool call and checks that both Root
and Subagent requests use the CLI-selected model. The fixture config defines
only the synthetic provider and two models; it sets no default model,
`small_model`, agent model, or depth. Production Summary supplies no inline
OpenCode configuration.

## Limits

The suite does not exercise paid-model execution, concurrent source mutations
throughout pagination, remote servers, or every upstream message subtype.
Malformed response structures, unknown message types, cursor cycles, and
unsupported Major versions are separate unit-test cases. Existing synthetic
V1 archive tests cover compatibility with previously published archives.
