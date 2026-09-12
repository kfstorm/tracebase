# Tracebase

Tracebase preserves private work evidence in a replayable local archive and generates offline context for exploring that work. GitHub and OpenCode are the currently supported sources; additional sources may be added.

## Features

- **GitHub collection:** Archive Issues and Pull Requests you authored, commented on, or submitted reviews for, including collaboration context and raw PR diffs.
- **OpenCode collection:** Preserve source-native session exports with collection metadata.
- **Explicit coverage:** Record time ranges and source boundaries, retain successful empty runs, and reject overlapping published ranges for the same logical source.
- **Offline context:** Generate a disposable, browsable directory from archived evidence without querying the sources again.

Coverage records what the collector observed, not a guarantee of complete historical account activity. Tracebase preserves evidence; it does not generate long-term AI memory.

## Getting Started

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and use Python 3.14 or newer. From a checkout of this repository:

```bash
uv sync
uv run tracebase --help
```

GitHub collection additionally requires [GitHub CLI](https://cli.github.com/) installed and authenticated with `gh auth login`. OpenCode collection requires the `opencode` CLI on `PATH`; Tracebase starts its own temporary authenticated loopback server.

## Usage

Keep archives and generated context in user-controlled private storage outside the checkout. The paths below use your home directory; verify its permissions, backup, and synchronization policies before collecting sensitive data. Tracebase does not configure those policies for you.

### Collect GitHub Evidence

Collect for the currently authenticated GitHub actor:

```bash
gh auth status
uv run tracebase collect github \
  --archive "$HOME/.tracebase/archive" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00
```

### Collect OpenCode Sessions

Choose any instance ID that is unique across machines and stays the same for every collection on the same machine. Tracebase uses it to detect overlapping Collection Ranges and prevent duplicate collection. A machine name is valid if it meets both conditions; if the machine is renamed, keep using the original ID.

The following command prompts for your chosen ID:

```bash
read -r -p 'OpenCode instance ID: ' instance_id
uv run tracebase collect opencode \
  --archive "$HOME/.tracebase/archive" \
  --instance-id "$instance_id" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00
```

Both collectors require whole-second ISO 8601 timestamps with explicit offsets and `from < to`. Collection Ranges are half-open: `[from, to)`. A published range cannot be collected again for the same logical source; use the previous `--to` as the next `--from` for adjacent runs.

Successful collection prints one summary line to stdout; progress goes to stderr. Published runs live under `archive/runs/`, each with a `run.json` manifest and snapshots. Failed runs remain unpublished; any staging directories already created remain available for inspection. See the [Collection CLI Contract](docs/collection-cli-contract.md) for failure behavior.

### Generate Context Offline

After collecting evidence, select a work time range to explore:

```bash
uv run tracebase context \
  --archive "$HOME/.tracebase/archive" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00 \
  --output "$HOME/.tracebase/context-2026-09-01"
```

Start with `index.md` in the output directory. This range selects work records, not collector observation times. Context Output is disposable and does **not** redact sensitive data: treat it with the same confidentiality as the Raw Archive. Third-party or AI-service use requires a separate scope and sanitization decision.

For all options, run `uv run tracebase collect github --help`, `uv run tracebase collect opencode --help`, or `uv run tracebase context --help`.

## Development

Install Node.js/npm as well as uv to run the duplication check. Bootstrap dependencies and Git hooks, then run the checks:

```bash
./scripts/setup-dev.sh
./scripts/run-unit-tests.sh
./scripts/lint.sh --check
uv run pymarkdown --strict-config scan README.md AGENTS.md
uv run pre-commit run --all-files
```

The lint script checks Python style, types, dead code, dependencies, and domain documentation. Without `--check`, it also fixes and formats Python files. The full pre-commit suite includes tests and `jscpd` duplication detection.

See [AGENTS.md](AGENTS.md) for contributor guardrails, [CONTEXT.md](CONTEXT.md) for domain terminology, and [GitHub issue conventions](docs/agents/issue-tracker.md) for planning work.

## License

No license is currently declared in this repository.
