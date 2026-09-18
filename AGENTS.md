# Agent Guide

Tracebase collects private work evidence, derives offline Context Output, and produces Summary Output. GitHub, OpenCode, and ordinary personal ChatGPT conversations are the currently supported sources; additional sources may be added. See [README.md](README.md) for setup and CLI usage.

## Task Context

- Before exploring implementation, read [CONTEXT.md](CONTEXT.md) and relevant ADRs under `docs/adr/` if present. Follow [domain guidance](docs/agents/domain.md) when changing terminology or architectural decisions.
- For issues and specs, follow [issue tracker conventions](docs/agents/issue-tracker.md); for triage, read [triage labels](docs/agents/triage-labels.md).
- When changing collection orchestration, diagnostics, or publication, read the [Collection CLI Contract](docs/collection-cli-contract.md). It supersedes the historical failure-output clause.

## Consumer Contracts

Before adding a fact to a prompt or document, identify its consumer and include
only what that consumer needs to use Tracebase correctly:

- `README.md` is for installation, operation, safe use, user-visible behavior,
  operational constraints, important limitations, and security/privacy
  requirements. Keep archive layouts, reconstruction or projection mechanics,
  internal identity representation, test history, algorithms, and similar
  implementation details out unless they directly affect correct operation.
- `CONTEXT.md` is authoritative for archive, collection, identity, observation,
  projection, attribution, and source-specific semantic contracts. Keep detailed
  domain and source semantics here instead of duplicating them in the README or
  model prompts.
- For domain and source facts, Summarizer and other model prompts may use only
  information observable in their actual input, plus rules needed to interpret
  that input. Prompts may additionally define the execution, orchestration,
  validation, state-management, and output protocols required to perform the
  task. Those protocols must not depend on hidden archive internals, collector
  mechanics, projection implementation, source identifiers unavailable in the
  model input, path-generation algorithms, historical schemas, or other hidden
  implementation details.
- Implementation and tests may know how higher-level contracts are produced;
  that knowledge must not automatically propagate into higher layers.

Prefer the weakest sufficient contract. If a consumer can discover a value from
its actual input, tell it to use that value instead of explaining how the value
was generated. For example, derive Context item roots from `index.md`; do not
teach the Summarizer how those roots are numbered, encoded, sorted, or tied to
archive identity.

Do not propagate implementation changes into every documentation or prompt
layer. For each affected layer, ask whether its observable contract changed
before editing it. An internal representation, path, or algorithm change does
not require README or prompt changes when consumer-visible behavior is
unchanged.

For README, prompt, or other consumer-facing contract changes, review every new
fact:

1. Does this consumer need it to behave correctly?
2. Can the consumer discover it from its actual input instead?
3. Is it observable behavior, or only an implementation detail?
4. Would an implementation change require updating this text while the
   consumer-visible contract stayed the same? If so, move the detail lower.

Prompt and documentation contract tests should check required behavior and
user-visible guarantees, not incidental prose or implementation details. Prefer
semantic assertions for evidence coverage, attribution, deriving roots or
identifiers from observable input, exact/disjoint scopes, and safety or
correctness invariants. Use exact wording assertions only when the wording is
an intentional protocol/API contract. Narrow negative assertions may protect an
explicitly retired concept or schema; avoid broad keyword blacklists.

Keep this policy in `AGENTS.md`. `docs/agents/domain.md` remains focused on
reading domain documentation and using established domain vocabulary, while
`CONTEXT.md` defines domain facts and contracts.

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
- Keep the v0 archive in user-controlled private storage and preserve source-native content unchanged. When persisting data acquired from an external API, prefer preserving the complete source-native response body unchanged and derive narrower representations later. Do not discard response fields during acquisition unless an explicit contract requires it. Preserve paginated responses page by page rather than merging or truncating them. This applies to collection and to other archive-level API acquisition such as identity profiles. Archive privacy and synchronization are caller preconditions; any third-party or AI-service use requires a separate derived-data scope and sanitization decision.
- Source identity profiles live under `profiles/<source>/` as archive-level mutable metadata outside `runs/`, Snapshots, and Source-native Evidence. GitHub profiles are keyed by the stable source scope from `/user.node_id`, matching GitHub `Collection Run.source.scope_id`. GitHub collection must not require or automatically refresh profiles; Context generation that selects GitHub items requires the matching profile.
- For OpenCode, use `--instance-id` to scope overlap detection and prevent duplicate collection. Keep it unique across machines and stable on the same machine; a machine name is valid if it meets both conditions.
- Derive Context Output offline without mutating the Raw Archive. Context Output is disposable and not redacted; apply the same confidentiality as the archive.
- Use synthetic evidence in tests and keep real archives, session exports, Context Output, Summary Output, and debug output outside the repository. Progress and exceptions must not expose credentials or source-native sensitive content. Identity enrichment does not render email addresses from the GitHub identity profile. Source-native content rendered into Context Output may itself contain email addresses; Context Output does not perform redaction. Debug output must exclude credentials and secrets, but may contain sensitive work evidence; treat it with the same confidentiality as the Raw Archive.
- Source collectors must not provision or manage virtual displays, schedulers, or similar deployment infrastructure; deployment orchestration is caller-owned.
- ChatGPT tests and fixtures must use synthetic conversations. Real ChatGPT conversations, archives, browser profiles, cookies, and tokens must remain outside the repository.
- Keep provider-native evidence separate from credential-equivalent browser profile state; authentication state must never enter the Raw Archive.
