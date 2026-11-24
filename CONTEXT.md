# Activity Archive

This context defines the evidence and observation language for a historical, replayable activity archive. It distinguishes durable source evidence from the bounded claims made about collection.

## Language

**Source Item**:
A source-specific top-level unit that is independently discovered and represented by a Snapshot. A GitHub Item is an Issue or Pull Request that passed Eligibility; an OpenCode Item is a session; a ChatGPT Item is an ordinary personal conversation. An Item does not include its related-object graph, and the shared term does not erase source-specific semantics.
_Avoid_: Artifact, universal entity

**Explicit Reference**:
An occurrence of a recognizable target identifier in a source object's native content. It directly retains the containing source object and may resolve to an archived target; an unresolved target remains a reference. It does not assert project ownership, repository work, implementation, or causality. A complete GitHub Issue or Pull Request URL is the first supported reference form, not the boundary of the concept.
_Avoid_: Project association, work attribution, Mention

**Eligibility**:
The direct-participation gate for a GitHub Item: the tracked actor is its author, wrote an ordinary comment, or submitted a review. Mentions, assignments, review requests, and line-level review comments are discovery context, not independent eligibility.
_Avoid_: Discovery result, involvement

**Source-native Evidence**:
Unmodified bytes provided by a source and preserved in the private v0 archive. It is distinct from any later redacted or derived content.
_Avoid_: Sanitized original, memory

**GitHub Identity Profile**:
Archive-level mutable metadata keyed by the tracked GitHub account's stable `/user.node_id`, matching the `source.scope_id` in GitHub Collection Run manifests. It preserves complete `/user` and paginated `/user/emails` response bodies and derives commit email attribution when read. It is not Source-native Evidence, a Snapshot, or part of a Collection Run; it lives outside `runs/` and may be refreshed without recollecting historical evidence.
_Avoid_: Identity evidence, account snapshot

**Hydration**:
The collection of source-native provider responses for one discovered Source Item. Hydration preserves the observed responses, including source-specific pagination, without asserting they formed an atomic provider snapshot.
_Avoid_: Sync, mirror, enrichment

**Coverage**:
The recorded query and permission boundary of a successfully published Collection Run. It is not a claim of complete historical account activity.
_Avoid_: Completeness, audit log

**Source Instance**:
A stable source-specific scope used to distinguish Collection Ranges and detect overlap to prevent duplicate collection. For GitHub it is the authenticated actor's stable node ID; for OpenCode it is a user-chosen `--instance-id` that is unique across machines and stable on the same machine; for ChatGPT it is the authenticated account's stable provider identity used as the scope ID. A machine name is valid as an OpenCode instance ID if it meets both conditions.
_Avoid_: Project association

**Source Attribution Policy**:
The source-declared rule for projecting evidence into the user's work. A
conversational Context identifies work-related activity, including delegated
cognitive or agent work, as user work and non-work activity as context-only.
When Context records carry `[User work]` or `[Context only]` annotations, the
annotation on each atomic record is authoritative; other participant activity
remains context-only. Attribution is applied before workstream identification,
ranking, or synthesis, and unsupported sources do not inherit a policy.

**User-work Projection**:
The attribution-filtered evidence that may become a Summary workstream. It is
applied before workstream identification, ranking, or synthesis. Context-only
evidence may explain a user's work or its state, but cannot itself become final
Summary content.

**Snapshot**:
An append-only, complete set of evidence from one Observation Window of one Source Item. A Snapshot does not represent a global system state.
_Avoid_: Current state, sync point

Every published Snapshot satisfies the archive invariant
`observation_window.to <= run.completed_at` for its containing Collection Run.
Archive publication and complete archive loading reject a violation.

**Collection Run**:
An all-or-nothing manual observation of a Collection Range that publishes its Snapshots only when every planned source operation succeeds. A successful empty Collection Run publishes its manifest; a failed Collection Run publishes no run in `runs/`.
_Avoid_: Partial collection, background sync

**Collection Range**:
The required half-open interval `[from, to)` supplied to a Collection Run as ISO 8601 timestamps with explicit offsets and whole-second precision. The endpoints are parsed as instants, must satisfy `from < to`, and successful ranges are compared within the same logical source to prevent unintended overlap.
_Avoid_: Run date, observation time

**Observation Window**:
The start and end of the collector's observation of one source object. It is distinct from timestamps reported by the source object.
_Avoid_: Snapshot time, source update time

**Context Extraction Result**:
A disposable, in-memory result derived offline from the current archive for a mandatory, explicit half-open work time range. The shared v1 foundation groups loaded Snapshots by Source Item identity and selects exactly one Snapshot per logical object using its Observation Window completion time relative to the request end. Source-specific projections then interpret only that selected Snapshot, select source records for the requested range, and add inclusion meaning, relations, and uncertainty. If selected Context items include GitHub items, the corresponding stable-scope tracked-account GitHub Identity Profile is required and is used only to attribute Git commit identities. It is not serialized independently: the same process uses it to assemble consumer input from those objects. Its work time range is distinct from Collection Range; the result is neither Source-native Evidence, a Snapshot, nor a synchronized view of the source.
_Avoid_: Projection, synchronized view

**Context Output**:
A disposable, self-contained directory assembled from one Context Extraction Result for consumer exploration. It is reproducible from selected Raw Archive evidence, the Context request, and required archive-level identity profile(s), owned by the caller, and not a durable derived store or a synchronized view of the source. Refreshing an identity profile may change tracked-account annotations without changing Source-native Evidence.
_Avoid_: Cache, index, intermediate archive

OpenCode and ChatGPT conversational Context is text-only: it contains only
`user` and `assistant` text turns. Tool/task, system/developer, reasoning, code,
multimodal, hidden, synthetic/compaction, attachment/citation metadata, and
historical-version records do not enter the dialogue. OpenCode tool or other
non-text activity cannot select a session. ChatGPT uses the provider-returned,
page-ordered observed current stream; Context does not recover branch history,
regenerations, or edits. ChatGPT `create_time` is the placement timestamp;
`update_time` and edit mutations are not v1 activity semantics. Canvas/textdocs
and attachment binaries remain unsupported.

OpenCode collection discovers root and child sessions across all projects and
directories, including archived sessions. Session `time.updated` selects
candidates within the Collection Range. Context Activity uses message creation
time, so archiving a session alone cannot select it for Context.

OpenCode Context places messages created before the selected Session's
`time.created` in Background, including fork-inherited messages whose creation
times fall within the requested range. It classifies those messages using the
selected Session snapshot alone; the source Session need not still exist.

OpenCode discovery responses select Sessions for Hydration and are not
Source-native Evidence in individual Snapshots. Collection Run Coverage records
discovery statistics, request parameters, observation times, and V2 page counts.
V1 Snapshots retain their native session exports and available Project evidence.
V2 Snapshots retain the complete native `session.get` response, including its
`data` wrapper, as `session.json`, and each complete native
`session.message.list` page under `messages/`, with request provenance and
opaque cursors. Context interprets the selected Snapshot offline for either
protocol. V2 `user.text` and assistant `content` text become
dialogue; synthetic, compaction, shell, tool, and other non-dialogue records
remain only in Source-native Evidence.

Tracebase honors `hidden`, `info.hidden`, and `metadata.hidden` for V1 legacy
compatibility. In V2, message-level `metadata` is an open event/hook dictionary;
Tracebase does not
assign hiding semantics to arbitrary keys such as `metadata.hidden`. V2 message
type and content type determine dialogue eligibility.

OpenCode Project Directory uses archived V1 `project.worktree` or V2
`project.canonical`, while Working Directory retains the Session directory
(`location.directory` in V2). V2 `subpath` describes the path inside the current
checkout and cannot identify the original project's canonical directory.
Collection fetches the unpaginated V2 `project.list` with `archived=true` once
when Sessions are selected. Each related `project.json` preserves the complete
selected native object's JSON bytes, with operation, parameters, and record
selection provenance; it is not a complete Project-list response. Unrelated
Projects are not retained in Session Snapshots. Project lookup failure or an
invalid list aborts publication. Missing Project IDs, absent matching Projects,
or invalid canonical directories record Acquisition Gaps and fall back to the
Session directory; global Projects also use the Session directory. Older V2
Snapshots without Project evidence retain that fallback. Offline Context checks
Project identity and provenance and never consults Git or a live service.

ChatGPT omits messages with a non-empty, whitespace-trimmed string
`metadata.automation_id` from both Activity and Background. Filtering applies
only to the marked message; ordinary messages in the same conversation remain
eligible. Other metadata and message relationships do not identify automation
for Context, so automated messages without this marker may remain. Raw Archive
evidence retains the complete provider responses.

Context Markdown preserves source-native evidence type, temporal grouping,
relationship, and order. Where attribution annotations are present, they are
orthogonal record-level annotations such as `[User work]` or `[Context only]`;
they do not create parallel attribution sections or reorder a thread, timeline,
or commit section. Shard reports may project those records into separate
user-work and context-only sections, but Context itself does not.

Identity enrichment does not render email addresses from the GitHub identity profile. Source-native content rendered into Context Output may itself contain email addresses; Context Output v1 does not perform sensitive-data redaction and should be treated with the same confidentiality as the Raw Archive.

**Summary Output**:
A caller-owned, derived directory containing a human-readable work summary and
provenance generated from Context Output by the production Summarizer. The final
Summary is a projection of the user's work, not a summary of all activity in
Context: attribution semantics are applied before workstream identification and
synthesis. It is not Source-native Evidence and may contain sensitive work
evidence; treat it with the same confidentiality as the Raw Archive. Sending
its input to a third-party or AI service requires a separate derived-data scope
and sanitization decision.
_Avoid_: Raw Archive, long-term memory
