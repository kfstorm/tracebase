# OpenCode integration tests

From the repository root, run:

```bash
./scripts/test-opencode-integration.sh
```

You need Docker and network access during image builds. The script builds and
tests the `summary` and `integration` targets for both versions listed in
`versions.env`, then compares real-session Context Output across versions.
It reports all four target results and the Context comparison, and returns
success only when all pass. A failed build or test does not skip the remaining
targets or Major.
GitHub Actions invokes this same command. Update
the pins in `versions.env` to test newer releases; runtime support does not use
these pins.

Each test container has its own HOME, XDG directories, and database. Runtime
networking is disabled. Collector containers have no host mounts; Summary smoke
containers mount only the repository's smoke script and shared provider helper,
read-only. Neither receives
host credentials or server configuration. The EXIT trap removes test containers
and image tags, along with the temporary comparison directory. Docker may retain
reusable build layers.

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
Snapshot contains its own `session.get` response, Message pages, and the related
native Project object from the unpaginated `project.list(archived=true)` response.
The Project object retains all its original JSON bytes and selection provenance;
it is checked against an independent native list. Global
discovery statistics, requests, page counts, and observation times are checked in
Run Coverage. V1 exercises its full export;
its discovery endpoint does not expose a continuation contract Tracebase can
safely follow, so a continuation header fails collection. That rejection is
covered by unit tests, not a claimed real-server pagination test.

The Collector fixtures use upstream import operations with synthetic transcripts and never
call a model. Imports can reset Session update times, so initialization fixes
timestamps in the disposable container database before collection. The fixture
checks the Docker marker and isolated data path before opening SQLite. The
collector never reads SQLite. A late-hydration failure deletes a fixture Session
between discovery and its real export/API request.

## Real-session Context equivalence

`test_context_equivalence.py` starts the shared loopback-only Chat Completions
provider, runs a new real `opencode run`, and resumes it with `--session`.
Both Majors receive the same prompts through stdin and use the same
prompt-to-response rules. Stdin avoids V1's quoting of space-containing
positional arguments. The fixture sets the same explicit title, working
directory, and `PWD` for both versions. OpenCode creates every Session and
Message; this test neither imports sessions nor opens or edits the database.
Its HOME/XDG state is separate from the imported-fixture tests.

The same conversation checks run from a standard directory, a committed Git
repository's `packages/api` subdirectory, and a linked Git worktree created by
`git worktree add`. The subdirectory runs before the linked worktree, so both
versions already know the original project. Between layouts, the fixture deletes
only its scripted Session through the real CLI/API and keeps Project state.
Raw evidence and structured Context must identify the same Project owner, use
the original repository as Project Directory, and retain each distinct Working
Directory. No assertion depends on how Markdown displays those directories.

For both Majors, the suite also creates a real fork from a four-exchange
conversation, removes the synthetic source Session, and collects the fork alone.
It verifies the native fork still contains all exchanges, while Context keeps
only the newest three inherited user turns in Background and leaves the new fork
turn in Activity. The request covers every exchange so the assertion exercises
fork classification rather than ordinary time-range filtering. The offline
comparator repeats these checks against the copied Archive, structured
extraction, and Markdown, then compares the fork's full Markdown output along
with the ordinary, multi, and background scenarios.

After each round, the test runs `tracebase collect opencode` into a separate
synthetic Archive and calls production `generate_context()`. It checks three
outputs: the first round alone, both rounds as Activity, and the first round as
Background with the second as Activity. The Background range starts at the
second User message's archived creation instant. The test asserts that the
first Assistant message predates this boundary; it does not mock the clock or
sleep to guess a boundary.

Each output independently passes the production inventory and mutable-state
validators. Additional assertions check the complete file inventory, containment,
and nonempty Markdown output. Raw Archive checks validate title and directory,
unique native Message IDs, exact role/text sequence, and preservation of the first
round after resume. The source timestamps come directly from V1 exports or V2
Message pages, independently of the Context projection. Structured extraction
results must retain those same turns and real timestamps in the correct Activity
and Background groups.

For all four scenarios, only during `generate_context()`, the test temporarily
patches `tracebase.dialogue.format_timestamp()` to return `2000-01-01 00:00`.
This fixes the final time display; collection, classification, and ordering still
use real timestamps. The production Markdown renderer runs unchanged, and the
test never rewrites its output. Explicit titles and identical directories keep
other inputs consistent.

The entry point copies the synthetic Archives and Context directories from both
stopped containers with `docker cp` into a temporary host directory. A fresh
networkless comparator container receives a copy, revalidates both results, and
enumerates every Markdown file under each Context Output root. It compares the
complete relative-path sets and each file's unmodified `read_bytes()` value.
It derives item roots and file lists from `index.json`, checks every reference,
and rejects undeclared output files. The fork audit checks that Activity and
Background contain the expected messages. The byte comparison itself treats
all Markdown files as opaque bytes and does not normalize them. On failure it
reports the relative path, first differing byte, and a diff limited to 40 lines
of 240 characters each.

## Shared Dockerfile and Summary smoke

`src/tracebase/container/Dockerfile` installs the requested official GitHub
Release binary once for Linux x64 or arm64. Its `summary` target contains
OpenCode, Node.js/npm, Python 3.14, uv, curl, and CA certificates. The `integration`
target adds Git, Tracebase, and test dependencies. Summary builds use only the
packaged `container/` directory as their context; integration builds use the
repository root. Test versions come from `versions.env`, not the Dockerfile.
The root `.dockerignore` excludes Git metadata, dependency and Python caches,
build and temporary directories, local environment and credential files, SQLite
databases and sidecars, logs, and test reports from the repository build context.

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
