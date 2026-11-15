#!/usr/bin/env bash
set -euo pipefail
status=0
python -m pytest -q -o addopts= /integration/test_collector.py || status=1
# The live-run scenarios must never share the imported fixtures' database.
export HOME=/isolated/equivalence/home XDG_CONFIG_HOME=/isolated/equivalence/config
export XDG_DATA_HOME=/isolated/equivalence/data XDG_CACHE_HOME=/isolated/equivalence/cache
export XDG_STATE_HOME=/isolated/equivalence/state
python /integration/test_context_equivalence.py generate /equivalence-results || status=1
exit "$status"
