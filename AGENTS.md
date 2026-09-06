## Agent skills

### Issue tracker

Issues are tracked in this repository's GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The default five canonical triage labels are used. See `docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain-doc layout. See `docs/agents/domain.md`.

### Work-record archive

Preserve evidence needed to reconstruct professional work and its necessary collaboration context. AI-generated long-term memory is a later derivative, not an acquisition input.

- Preserve a Pull Request's conversation, review evidence, and diff; do not acquire a full file snapshot for the Pull Request.
- Defer non-Pull-Request Git commits from v0. Reconsider them only if their work-record value justifies the cost of reliably separating them from Pull Request commits.
- Keep acquisition scoped to work evidence. Expand to social, operational, or account-wide data only through an explicit scope decision.
- Before adding an acquisition step with material complexity or operational cost, identify its unique work evidence and expected long-term-memory value. Include it only when that value justifies the cost.
- Require every Collection Run to declare ISO 8601 `--from` and `--to` timestamps with explicit offsets; retain successful empty runs and reject known range overlap for the same logical source.
- Keep the v0 archive in user-controlled private storage and preserve source-native content unchanged. Archive privacy and synchronization are caller preconditions; any third-party or AI-service use requires a separate derived-data scope and sanitization decision.
