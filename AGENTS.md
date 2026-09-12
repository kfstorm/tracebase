# Agent Guide

Tracebase collects private work evidence and derives offline Context Output. GitHub and OpenCode are the currently supported sources; additional sources may be added. See [README.md](README.md) for setup and CLI usage.

## Task Context

- Before exploring implementation, read [CONTEXT.md](CONTEXT.md) and relevant ADRs under `docs/adr/` if present. Follow [domain guidance](docs/agents/domain.md) when changing terminology or architectural decisions.
- For issues and specs, follow [issue tracker conventions](docs/agents/issue-tracker.md); for triage, read [triage labels](docs/agents/triage-labels.md).
- When changing collection orchestration, diagnostics, or publication, read the [Collection CLI Contract](docs/collection-cli-contract.md). It supersedes the historical failure-output clause in issue #14.

## Development Checks

Use Python 3.14+ and `uv`. Run commands from the repository root:

```bash
uv sync --group dev
uv run tracebase --help
./scripts/run-unit-tests.sh
./scripts/lint.sh --check
```

- `scripts/lint.sh` without `--check` edits Python files; use check mode for verification.
- For Markdown changes, also run `uv run pymarkdown --strict-config scan README.md AGENTS.md`; the lint script scans domain docs separately.
- Before committing, run `uv run pre-commit run --all-files` (requires Node.js/npm for `npx --yes jscpd`). Install hooks with `./scripts/setup-dev.sh`.

## Work-record Archive

Preserve evidence needed to reconstruct professional work and its necessary collaboration context. AI-generated long-term memory is a later derivative, not an acquisition input.

- Hydrate only eligible GitHub Items: an Issue or Pull Request where the tracked actor is the author, an ordinary commenter, or a submitted reviewer. Mentions, assignments, review requests, and line-level review comments do not independently qualify a GitHub Item.
- Preserve each eligible GitHub Item's source payload, ordinary comments, and complete Issue timeline. For a Pull Request, additionally preserve the Pull Request payload, submitted reviews, line-level review comments, and source-native raw diff response; do not acquire PR commits, `/files` inventories, repository contents, or full file snapshots.
- Defer non-Pull-Request Git commits from v0. Reconsider them only if their work-record value justifies the cost of reliably separating them from Pull Request commits.
- Keep acquisition scoped to work evidence. Expand to social, operational, or account-wide data only through an explicit scope decision.
- Before adding an acquisition step with material complexity or operational cost, identify its unique work evidence and expected long-term-memory value. Include it only when that value justifies the cost.
- Require every Collection Run to declare ISO 8601 `--from` and `--to` timestamps with explicit offsets; retain successful empty runs and reject known range overlap for the same logical source.
- Keep the v0 archive in user-controlled private storage and preserve source-native content unchanged. Archive privacy and synchronization are caller preconditions; any third-party or AI-service use requires a separate derived-data scope and sanitization decision.
- For OpenCode, use `--instance-id` to scope overlap detection and prevent duplicate collection. Keep it unique across machines and stable on the same machine; a machine name is valid if it meets both conditions.
- Derive Context Output offline without mutating the Raw Archive. Context Output is disposable and not redacted; apply the same confidentiality as the archive.
- Use synthetic evidence in tests and keep real archives, session exports, and Context Output outside the repository. Progress and exceptions must not expose credentials or source-native sensitive content.
