# OpenCode integration tests

From the repository root, run:

```bash
./scripts/test-opencode-integration.sh
```

You need Docker and network access during image builds. The script builds and
tests both versions listed in `versions.env`, reports each result, and returns
success only when both pass. GitHub Actions invokes this same command. Update
the pins in `versions.env` to test newer releases; runtime support does not use
these pins.

Each test container has its own HOME, XDG directories, and database. Runtime
networking is disabled. The framework mounts no host directories and passes no
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
key. Summary orchestration and cross-Major rejection have separate unit tests.

V2 runs with a two-record page limit in the test process to exercise real
session and message pagination without hundreds of fixtures. API responses are
compared byte for byte with preserved evidence. V1 exercises its full export;
its discovery endpoint does not expose a continuation contract Tracebase can
safely follow, so a continuation header fails collection. That rejection is
covered by unit tests, not a claimed real-server pagination test.

Fixtures use upstream import operations with synthetic transcripts and never
call a model. Imports can reset Session update times, so initialization fixes
timestamps in the disposable container database before collection. The fixture
checks the Docker marker and isolated data path before opening SQLite. The
collector never reads SQLite. A late-hydration failure deletes a fixture Session
between discovery and its real export/API request.

## Limits

The suite does not exercise paid-model execution, concurrent source mutations
throughout pagination, remote servers, or every upstream message subtype.
Malformed response structures, unknown message types, cursor cycles, and
unsupported Major versions are separate unit-test cases. Existing synthetic
V1 archive tests cover compatibility with previously published archives.
