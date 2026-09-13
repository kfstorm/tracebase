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
- **OpenCode collection:** Preserve source-native session exports with collection metadata.
- **ChatGPT collection:** Preserve ordinary personal conversations using the authenticated ChatGPT web session.
- **Explicit coverage:** Record time ranges and source boundaries, retain successful empty runs, and reject overlapping published ranges for the same logical source.
- **Offline context:** Generate a disposable, browsable directory from archived evidence without querying the sources again.
- **Work summaries:** Run the production Summarizer against an archive or existing Context Output and publish a validated Markdown summary with provenance.

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

### Sync GitHub Identity

Identity sync is not required for GitHub collection. It is required before generating Context Output that includes GitHub items, so historical Git commits can be marked as `(tracked account)` when their author email matches the authenticated GitHub account. Regular `collect github` still calls `/user` only and does not automatically call `/user/emails`. Identity sync preserves the complete `/user` response and every paginated `/user/emails` response; attribution fields are derived later while reading the profile.

Identity sync requires a GitHub token with the `user:email` scope. For a GitHub CLI OAuth credential, refresh the scope and then sync using the explicit archive path:

```bash
gh auth refresh -h github.com -s user:email

uv run tracebase identity github sync \
  --archive "/path/to/archive"
```

The profile is stored under the stable GitHub source identity returned by `/user.node_id`:

```text
<archive>/profiles/github/<encoded-scope-id>/
├── profile.json
├── user.json
├── emails.001.json
└── ...
```

The profile is archive-level mutable metadata keyed by the same stable source scope stored in GitHub Collection Run manifests. It is not part of `runs/`, a Snapshot, or Source-native Evidence. It contains associated GitHub account data, so protect it with the same care as the Raw Archive. A profile can be refreshed without recollecting historical evidence. Skipping identity sync does not affect collection or Context generation when no GitHub items are included. If Context includes GitHub items, the matching stable-ID profile must exist and be valid; otherwise generation fails with the sync command needed to create it. Identity enrichment does not render email addresses from the GitHub identity profile. Source-native content rendered into Context Output may itself contain email addresses; Context Output does not perform redaction.

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

Successful collection prints one summary line to stdout; progress goes to stderr. Published runs live under `archive/runs/`, each with a `run.json` manifest and snapshots. Failed runs remain unpublished; any staging directories already created remain available for inspection. See the [Collection CLI Contract](docs/collection-cli-contract.md) for failure behavior.

### Collect ChatGPT Conversations

ChatGPT auth and collection require a system Chromium installation. Tracebase uses the same system Chromium executable and persistent profile for authentication and collection. Interactive authentication launches Chromium directly; collection and status use it through Playwright. Playwright's bundled browser binary is not used. On Linux, install the `chromium` system package before using this collector.

Interactive ChatGPT authentication and collection use the same system Chromium browser and Tracebase-owned profile. This keeps one browser engine and one persistent profile for cookies, OAuth state, and session probing.

Launch the Tracebase-owned persistent browser profile for interactive authentication. This command does not inject Playwright or inspect the session while the browser is open: complete login yourself and close Chromium; the command then checks the saved session automatically. The profile contains credential-equivalent sensitive browser state; it is kept outside the Raw Archive and must remain private:

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

Reset is the supported account-switching flow: run `reset`, then run `auth chatgpt` again. It deletes only the dedicated browser profile, including its cookies, local storage, and provider login state; it does not delete previously collected ChatGPT Raw Archive data. Reset is refused while another Tracebase ChatGPT browser operation is using the profile.

During interactive authentication, Tracebase leaves Google, Microsoft, Apple, MFA, CAPTCHA, Cloudflare, and other provider verification flows to the user. It does not automate or bypass passwords, account selection, MFA, CAPTCHA, Cloudflare, or browser verification. The launched Chromium process is not controlled by Playwright; close it after completing the normal third-party login flow, and Tracebase automatically inspects the shared profile.

Collect ordinary personal ChatGPT conversations with an explicit browser mode:

```bash
uv run tracebase collect chatgpt \
  --archive "$HOME/.tracebase/archive" \
  --browser-mode headless \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00
```

`--browser-mode` accepts `headless` (the default) or `headed`. Headless collection may receive ChatGPT browser verification and then fails cleanly rather than switching modes or bypassing verification. Tracebase does not start, manage, or detect Xvfb or another virtual display. A deployment wrapper may provide one for scheduled headed collection, for example with external `xvfb-run`; that is deployment-layer behavior, not collector behavior. Synthetic auth tests are hermetic and do not launch Chromium or access provider networks. Real-account OAuth behavior remains a manual validation step; macOS and Windows real-account behavior is also deferred until suitable environments are available.

ChatGPT discovery uses conversation `update_time` and the same half-open Collection Range `[from, to)`. Discovery `update_time` is validated for message/edit content changes but is not known to advance for every conversation metadata mutation; it is not a complete conversation mutation log. Message-level work-time selection belongs to a later Context projection, not collection. Authentication failures, browser verification, rate limits, or incomplete hydration prevent a successful run from being published.

Known ChatGPT source limitations include mutable offset pagination, provider responses that do not guarantee complete branch history, Canvas/textdocs outside the v1 representation, and conversations deleted before discovery being unavailable for retrospective collection. Discovery excludes Project and Custom GPT conversations when the provider item has a non-null `gizmo_id`; this predicate was validated against synthetic ordinary, Project, and Custom GPT conversations. Other provider metadata may still be incomplete or change without notice.

### Generate Context Offline

After collecting evidence, select a work time range to explore. Context extraction chooses one observation per Source Item: the earliest observation completed at or after the request end, or the latest available observation if all observations are earlier.

If the selected Context includes GitHub items, the matching GitHub identity profile must already exist and be valid. Run the identity sync first when needed.

```bash
uv run tracebase context \
  --archive "$HOME/.tracebase/archive" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00 \
  --output "$HOME/.tracebase/context-2026-09-01"
```

Start with `index.md` in the output directory. This range selects work records, not collector observation times. Context Output is disposable and does **not** redact sensitive data: treat it with the same confidentiality as the Raw Archive. Third-party or AI-service use requires a separate scope and sanitization decision.

For all options, run `uv run tracebase collect chatgpt --help`, `uv run tracebase collect github --help`, `uv run tracebase collect opencode --help`, `uv run tracebase identity github sync --help`, or `uv run tracebase context --help`.

### Generate a Work Summary

The `summary` command runs the production Summarizer in a pinned Docker image. It requires Docker and a configured OpenCode authentication file so the selected model can run. The archive mode creates Context Output from the requested range, then publishes `summary.md` and `manifest.json`:

```bash
uv run tracebase summary \
  --archive "$HOME/.tracebase/archive" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00 \
  --model openai/gpt-5 \
  --output "$HOME/.tracebase/summary-2026-09-01"
```

To retain the generated Context Output or inspect non-secret runtime and shard artifacts, provide separate output directories:

```bash
uv run tracebase summary \
  --archive "$HOME/.tracebase/archive" \
  --from 2026-09-01T00:00:00+00:00 \
  --to 2026-09-02T00:00:00+00:00 \
  --model openai/gpt-5 \
  --context-output "$HOME/.tracebase/context-2026-09-01" \
  --debug-output "$HOME/.tracebase/summary-debug-2026-09-01" \
  --output "$HOME/.tracebase/summary-2026-09-01"
```

You can also summarize an existing Context Output with `--context` instead of `--archive`; omit `--from`, `--to`, and `--context-output` in that mode:

```bash
uv run tracebase summary \
  --context "$HOME/.tracebase/context-2026-09-01" \
  --model openai/gpt-5 \
  --output "$HOME/.tracebase/summary-2026-09-01"
```

Summary Output, retained Context Output, and debug output may contain sensitive work evidence and should be handled like the Raw Archive. The selected model provider may receive the Context Output; make a separate derived-data scope and sanitization decision before using a third-party or AI service. The summary is published only after its required shard reports pass validation; failed summaries are not published. For all options, run `uv run tracebase summary --help`.

## Development

Install Node.js/npm as well as uv to run the duplication check. Bootstrap dependencies and Git hooks, then run the checks:

```bash
./scripts/setup-dev.sh
./scripts/run-unit-tests.sh
./scripts/lint.sh --check
uv run pymarkdown --strict-config scan README.md AGENTS.md
uv run pre-commit run --all-files
```

The lint script checks Python style, types, dead code, dependencies, and domain documentation. Without `--check`, it also fixes and formats Python files. The full pre-commit suite includes tests and `jscpd` duplication detection. Summary integration requires Docker and OpenCode credentials, so its container workflow is covered by synthetic tests rather than a live model call.

See [AGENTS.md](AGENTS.md) for contributor guardrails, [CONTEXT.md](CONTEXT.md) for domain terminology, and [GitHub issue conventions](docs/agents/issue-tracker.md) for planning work.

## License

No license is currently declared in this repository.
