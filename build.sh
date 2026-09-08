#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec podman build -t llm-sandbox "$@" "$repo"
