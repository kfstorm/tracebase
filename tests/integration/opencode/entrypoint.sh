#!/usr/bin/env bash
set -euo pipefail
exec python -m pytest -q -o addopts= /integration/test_collector.py
