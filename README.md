# Tracebase

Tracebase preserves private work evidence in a replayable local archive, generates offline context, and produces work summaries. GitHub, OpenCode, and ordinary personal ChatGPT conversations are the currently supported sources; additional sources may be added.

## Contents

- [Features](#features)
- [Getting Started](#getting-started)
- [Usage](#usage)
- [Sync GitHub Identity](#sync-github-identity)
- [Development](#development)
- [License](#license)

## Features

- **GitHub collection:** Archive Issues and Pull Requests you authored, commented on, or submitted reviews for, including collaboration context and raw PR diffs.
- **OpenCode collection:** Archive OpenCode sessions for offline Context generation.
- **ChatGPT collection:** Archive ordinary personal conversations from an authenticated ChatGPT web session.
- **Explicit coverage:** Record time ranges and source boundaries, retain successful empty runs, and reject overlapping published ranges for the same logical source.
- **Offline context:** Generate a disposable, browsable directory from archived evidence without querying the sources again.
- **Explicit attribution:** Context explains how conversational work is attributed and marks applicable records as `[User work]` or `[Context only]`.
- **Work summaries:** Run the production Summarizer against an archive or existing Context Output and publish a validated Markdown summary with provenance.

Coverage records what the collector observed, not a guarantee of complete historical account activity. Tracebase preserves evidence; it does not generate long-term AI memory.

## Getting Started

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and use Python 3.14 or newer. From a checkout of this repository:

```bash
uv sync
uv run tracebase --help
```

GitHub collection additionally requires [GitHub CLI](https://cli.github.com/) installed and authenticated with `gh auth login`. OpenCode collection requires the `opencode` CLI on `PATH`.

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

### Sync GitHub Identity

Identity sync is not required for GitHub collection. Run it before generating
Context Output that includes GitHub items so historical Git commits can be
marked as `(tracked account)` when their author email matches the authenticated
GitHub account.

Identity sync requires a GitHub token with the `user:email` scope. For a GitHub CLI OAuth credential, refresh the scope and then sync using the explicit archive path:

```bash
gh auth refresh -h github.com -s user:email

uv run tracebase identity github sync \
  --archive "/path/to/archive"
```

The identity profile contains sensitive GitHub account data. Protect it with the
same care as the Raw Archive. You can refresh it without recollecting historical
evidence. Skipping identity sync does not affect collection or Context generation
when no GitHub items are included. If Context includes GitHub items, the matching
profile must exist and be valid; otherwise generation fails and reports the sync
command needed to create it. Tracebase does not display email addresses from the
identity profile. Source content rendered into Context Output may itself contain
email addresses, and Context Output does not redact them.

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

All source collectors require whole-second ISO 8601 timestamps with explicit offsets and `from < to`. Collection Ranges are half-open: `[from, to)`. A published range cannot be collected again for the same logical source; use the previous `--to` as the next `--from` for adjacent runs.

Successful collection prints one summary line to stdout; progress goes to
stderr. Failed runs remain unpublished. See the [Collection CLI Contract](docs/collection-cli-contract.md) for failure behavior.

### Collect ChatGPT Conversations

ChatGPT authentication and collection require system Chromium. The dedicated
Tracebase browser profile contains credential-equivalent sensitive state, stays
outside the Raw Archive, and must remain private. On Linux, install the
`chromium` system package before using this collector.

Launch the dedicated browser profile for interactive authentication. Complete
login yourself and close Chromium when you are finished:

```bash
uv run tracebase auth chatgpt
```

Check the saved account without displaying a browser:

```bash
uv run tracebase auth chatgpt status
```

Reset the Tracebase-owned ChatGPT browser/authentication state:

```bash
uv run tracebase auth chatgpt reset
```

Use `reset` when switching accounts, then run `auth chatgpt` again. Reset
deletes only the dedicated browser profile and does not delete previously
collected ChatGPT Raw Archive data. Reset is refused while another Tracebase
ChatGPT browser operation is using the profile.

Tracebase leaves provider login, MFA, CAPTCHA, Cloudflare, and other browser
verification flows to you. It does not automate or bypass them. Headless mode
may fail when ChatGPT requires browser verification; use headed mode when an
interactive browser is appropriate.

Collect ordinary personal ChatGPT conversations with an explicit browser mode:

```bash
uv run tracebase collect chatgpt \
  --archive "$HOME/.tracebase/archive" \
  --browser-mode headless \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00
```

`--browser-mode` accepts `headless` (the default) or `headed`. Authentication
failures, browser verification, rate limits, or unavailable provider data can
prevent a successful run from being published. Project and Custom GPT
conversations are currently excluded. Conversations that have already been
deleted or are no longer available from the provider cannot be recovered
retrospectively.

### Generate Context Offline

After collecting evidence, select a work time range to explore. Tracebase
generates Context offline from the archived evidence for that interval.

If the selected Context includes GitHub items, the matching GitHub identity profile must already exist and be valid. Run the identity sync first when needed.

```bash
uv run tracebase context \
  --archive "$HOME/.tracebase/archive" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00 \
  --output "$HOME/.tracebase/context-2026-09-01"
```

Use `index.json` as the machine-readable manifest for the requested interval
and item file inventory, then read the listed item files. Work-related
OpenCode and ChatGPT conversational activity is attributed to the user;
GitHub Context marks attributable records as `[User work]` and collaborator or
other context as `[Context only]`.
Collaborator evidence remains context-only. Context Output is disposable and
does **not** redact sensitive data: treat it with the same confidentiality as
the Raw Archive. Third-party or AI-service use requires a separate scope and
sanitization decision.

Conversational Context contains retained `user` and `assistant` dialogue for
the requested interval and bounded earlier background that may help explain it.
Later dialogue is not included. See [CONTEXT.md](CONTEXT.md) for the
authoritative source and archive semantics.

For all options, run `uv run tracebase collect chatgpt --help`, `uv run tracebase collect github --help`, `uv run tracebase collect opencode --help`, `uv run tracebase identity github sync --help`, or `uv run tracebase context --help`.

### Generate a Work Summary

The `summary` command runs the production Summarizer in a pinned Docker image. It requires Docker and a configured OpenCode authentication file so the selected model can run. The archive mode creates Context Output from the requested range, then publishes `summary.md` and `manifest.json`:

Use `--language LANGUAGE` to choose the natural language for the Summary text and
headings. For example, `--language zh-CN` requests Simplified Chinese. Omit the
option to keep the current language behavior.

By default, Summary resolves the `latest` tag for `opencode-ai` through the npm
registry before running. Use `--opencode-version VERSION_OR_TAG` to select an
exact version or an existing dist-tag such as `beta` or `next`. Dist-tags require
registry access; an exact version can reuse a cached Docker image offline. The
resolved version determines the Docker image and build version; the manifest
records the version reported by OpenCode inside the container.

```bash
uv run tracebase summary \
  --archive "$HOME/.tracebase/archive" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00 \
  --model openai/gpt-5.6-luna \
  --variant high \
  --language zh-CN \
  --output "$HOME/.tracebase/summary-2026-09-01"
```

To retain the generated Context Output or debug output, provide separate output directories:

```bash
uv run tracebase summary \
  --archive "$HOME/.tracebase/archive" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00 \
  --model openai/gpt-5.6-luna \
  --variant high \
  --context-output "$HOME/.tracebase/context-2026-09-01" \
  --debug-output "$HOME/.tracebase/summary-debug-2026-09-01" \
  --output "$HOME/.tracebase/summary-2026-09-01"
```

You can also summarize an existing Context Output with `--context` instead of `--archive`; omit `--from`, `--to`, and `--context-output` in that mode:

```bash
uv run tracebase summary \
  --context "$HOME/.tracebase/context-2026-09-01" \
  --model openai/gpt-5.6-luna \
  --variant high \
  --output "$HOME/.tracebase/summary-2026-09-01"
```

Summary Output contains materially meaningful user work rather than all activity
in Context. Unrelated personal activity and collaborator-only activity are
excluded. Summary Output, retained Context Output, and debug output may contain
sensitive work evidence and should be handled like the Raw Archive. The selected
model provider receives only the validated evidence files selected from Context
Output; `index.json` and unrelated Context root files remain host-only. Debug
output keeps the host Context and model-visible evidence in separate directories
and does not publish OpenCode authentication state. Make a separate derived-data
scope and sanitization decision before using a third-party or AI service. Failed
summary generation does not publish a successful Summary output. For all options,
run `uv run tracebase summary --help`.

## Development

Install Node.js/npm as well as uv to run the duplication check. Bootstrap dependencies and Git hooks, then run the checks:

```bash
./scripts/setup-dev.sh
./scripts/run-unit-tests.sh
./scripts/lint.sh --check
uv run pymarkdown --strict-config scan README.md AGENTS.md
uv run pre-commit run --all-files
```

The lint script checks Python style, types, dead code, dependencies, and domain documentation. Without `--check`, it also fixes and formats Python files. The full pre-commit suite includes tests and `jscpd` duplication detection. Summary integration requires Docker and OpenCode credentials.

See [AGENTS.md](AGENTS.md) for contributor guardrails, [CONTEXT.md](CONTEXT.md) for domain terminology, and [GitHub issue conventions](docs/agents/issue-tracker.md) for planning work.

## License

No license is currently declared in this repository.
