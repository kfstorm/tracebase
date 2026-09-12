# Activity Archive

This context defines the evidence and observation language for a historical, replayable activity archive. It distinguishes durable source evidence from the bounded claims made about collection.

## Language

**Source Item**:
A source-specific top-level unit that is independently discovered and represented by a Snapshot. A GitHub Item is an Issue or Pull Request that passed Eligibility; an OpenCode Item is a session. An Item does not include its related-object graph, and the shared term does not erase source-specific semantics.
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

**Hydration**:
The collection of source-native endpoint responses for one discovered GitHub Item. Hydration preserves the observed responses without asserting they formed an atomic provider snapshot.
_Avoid_: Sync, mirror, enrichment

**Coverage**:
The recorded query and permission boundary of a successfully published Collection Run. It is not a claim of complete historical account activity.
_Avoid_: Completeness, audit log

**Source Instance**:
A stable source-specific scope used to distinguish Collection Ranges and detect overlap to prevent duplicate collection. For GitHub it is the authenticated actor's stable node ID; for OpenCode it is a user-chosen `--instance-id` that is unique across machines and stable on the same machine. A machine name is valid if it meets both conditions.
_Avoid_: Project association

**Snapshot**:
An append-only, complete set of evidence from one Observation Window of one Source Item. A Snapshot does not represent a global system state.
_Avoid_: Current state, sync point

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
A disposable, in-memory result derived offline from the current archive for a mandatory, explicit half-open work time range. The shared v1 foundation groups loaded Snapshots by Source Item identity and selects exactly one Snapshot per logical object using its Observation Window completion time relative to the request end. Source-specific projections then interpret only that selected Snapshot, select source records for the requested range, and add inclusion meaning, relations, and uncertainty. It is not serialized independently: the same process uses it to assemble consumer input from those objects. Its work time range is distinct from Collection Range; the result is neither Source-native Evidence, a Snapshot, nor a synchronized view of the source.
_Avoid_: Projection, synchronized view

**Context Output**:
A disposable, self-contained directory assembled from one Context Extraction Result for consumer exploration. It is reproducible from the Raw Archive and request, owned by the caller, and not a durable derived store or a synchronized view of the source.
_Avoid_: Cache, index, intermediate archive

Context Output v1 does not perform sensitive-data redaction and should be treated with the same confidentiality as the Raw Archive.
