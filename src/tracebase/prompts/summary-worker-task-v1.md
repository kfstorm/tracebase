Review only the assigned Context evidence listed above and write exactly one
non-empty evidence-rich report to the canonical path below. This is an
intermediate report for the root summarizer, not the final human summary.

Attribution and work relevance are separate judgments. For OpenCode and
ChatGPT, work-related conversational activity is user work, including
delegated cognitive or agent work; non-work personal activity is context-only.
For GitHub, only records explicitly marked `[User work]` are attributable user
work; `[Context only]` remains context-only. Do not infer attribution from
actor names when these annotations are present.

Judge work relevance from purpose and intent supported by the assigned Context.
Explicit linkage to a repository, PR, issue, project, work task, client, role,
or engineering goal can support relevance, but is not required. Intrinsic work
intent such as investigation, research, evaluation, architecture, design,
implementation, debugging, validation, planning, or decision support can also
qualify. Technical subject matter, complexity, duration, interaction count,
troubleshooting depth, professional assistant responses, or time spent do not
establish work relevance by themselves. Do not invent project or workstream
relationships.

Personal operational troubleshooting may qualify when the assigned Context
establishes that it blocks development or an engineering task, is a necessary
recovery step for a workstream, or concerns project infrastructure. Treat it
as supporting work unless Context establishes an independent major workstream.

For conversational Context, `activity.md` is the only dialogue eligible to
establish requested-interval User work. `background.md` is earlier supporting
context only. It may explain in-range activity but must not independently
create requested-interval User work. If an in-range activity discusses earlier
work, classify the in-range request or analysis as User work and keep the
earlier work itself as background context only.

Treat a file-read or other tool result as partial evidence whenever it says the
output was truncated, capped, or has a continuation offset. Continue reading
until the unread portion cannot materially affect work relevance, importance,
latest state, outcome, or uncertainty. If continuation is unavailable, record
the unresolved uncertainty in the report. When state changes during the
requested interval, normally report the latest supportable state; mention a
reversal only when it matters to understanding the work.

Assistant cognitive output does not by itself prove external side effects. A
patch or command does not prove implementation, and a suggestion to run, test,
or deploy does not prove execution, validation, or deployment. An explicit
assistant report of actual execution or results may be considered according to
the strength of the evidence. Limit described external state to the evidence
provided.

Collaborator evidence may explain the user's action or resulting state, but it
is context-only. Do not summarize collaborator implementation, commits,
findings, investigation, fixes, decisions, or other authored work as the
user's work. Do not replace missing attribution with actorless or collective
wording.

For actor-scoped GitHub Context, follow `[User work]` and `[Context only]`
annotations on each atomic record. Do not infer attribution again from actor
names when annotations are present. Preserve grouping and order. A review
thread may contain both annotations in conversation order, and commits remain
in the single commits section shown in Context.

The report must contain exactly these two normalized ATX sections, at any
heading level, including an empty section when needed:

## User work

Include all assigned evidence that is both attributable to the user and
work-related, even when the root may later omit it for materiality.

## Context-only evidence

Include collaborator, non-work, or other evidence used only to explain user
work or state. Do not promote it as a workstream or describe it as the user's
work.

Preserve important motivation, decisions, final state, uncertainty, Context
paths, and technical detail for root synthesis. Preserve grouping and order
shown in Context. Keep review threads together and commits in their single
commits section. Do not decide Summary materiality, major work, or final
workstream boundaries. Do not infer that something was fixed merely because a
PR merged, a review thread resolved, or a diff looks like a fix. When
`Authored:` is shown, distinguish earlier authorship from later rebasing,
cherry-picking, or recommitting.

You are not an orchestrator. Do not call `task`, start another session, or
create child workers. Do not read or modify `/work/TASK.md`, `/work/NOTES.md`,
your `STATUS.json`, another shard directory, `/results`, or unassigned
Context evidence. Do not modify any file other than your own `REPORT.md`.
