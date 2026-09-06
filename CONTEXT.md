# Activity Archive

This context defines the evidence and observation language for a historical, replayable activity archive. It distinguishes durable source evidence from the bounded claims made about collection.

## Language

**Artifact**:
A source-owned GitHub Issue or Pull Request identified by its stable provider identifier. An Artifact is not its related-object graph.
_Avoid_: Record, work item

**Source-native Evidence**:
Unmodified bytes provided by a source and preserved in the private v0 archive. It is distinct from any later redacted or derived content.
_Avoid_: Sanitized original, memory

**Hydration**:
The collection of source-native endpoint responses for one discovered Artifact. Hydration preserves the observed responses without asserting they formed an atomic provider snapshot.
_Avoid_: Sync, mirror, enrichment

**Coverage**:
The recorded query and permission boundary of a successfully published Collection Run. It is not a claim of complete historical account activity.
_Avoid_: Completeness, audit log

**Source Instance**:
A stable source-specific scope used to distinguish Collection Ranges. For GitHub it is the authenticated actor's stable node ID; for OpenCode it is a user-maintained, globally unique opaque `--instance-id` that never encodes a hostname or filesystem path.
_Avoid_: Machine identity, project path

**Snapshot**:
An append-only, complete set of evidence from one Observation Window of one Artifact or OpenCode session. A Snapshot does not represent a global system state.
_Avoid_: Current state, sync point

**Collection Run**:
An all-or-nothing manual observation of a Collection Range that publishes its Snapshots only when every planned source operation succeeds. A successful empty Collection Run publishes its manifest; a failed Collection Run publishes no run in `runs/`.
_Avoid_: Partial collection, background sync

**Collection Range**:
The required half-open interval `[from, to)` supplied to a Collection Run as ISO 8601 timestamps with explicit offsets. The endpoints are parsed as instants, must satisfy `from < to`, and successful ranges are compared within the same logical source to prevent unintended overlap.
_Avoid_: Run date, observation time

**Observation Window**:
The start and end of the collector's observation of one source object. It is distinct from timestamps reported by the source object.
_Avoid_: Snapshot time, source update time
