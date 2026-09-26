#!/usr/bin/env bash
# Run launcher regression tests without starting VMs.
set -euo pipefail
repo="$(cd "$(dirname "$0")" && pwd)"
exec python3 -B -m unittest discover -s "$repo/tests" -p 'test_runner.py' -v
