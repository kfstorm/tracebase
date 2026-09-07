# Collection CLI Contract

This document describes the active failure and output contract for
`tracebase collect`. It supersedes the v0 specification's "safe diagnostics
only" failure-output clause in issue #14; issue #14 remains the historical
record of the v0 acquisition scope.

## Failure Output

The CLI intentionally does not catch collection or orchestration exceptions at
the top-level boundary. A failure may therefore produce a normal Python
traceback on `stderr` and a non-zero exit status.

Progress emitted before the failure remains on `stderr` and identifies the
current preparation, discovery, hydration, or publication task. The traceback
and progress output are diagnostic operational output, not Source-native
Evidence.

Tracebacks and progress output must not disclose credentials, authorization
values, raw request headers, source bodies, OpenCode session exports, or other
Source-native sensitive content. Exception messages and progress events must
follow the same boundary.

Failed Collection Runs remain unpublished. Their existing staging directories
remain available for inspection and do not enter the overlap registry.

## Success Output

A successful source command writes exactly one human-readable summary line to
`stdout`. Operational progress remains on `stderr`; its TTY presentation may
be Rich or timestamped line output, but it does not change collector behavior.
