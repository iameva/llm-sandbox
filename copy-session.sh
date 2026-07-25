#!/usr/bin/env bash
set -euo pipefail

BASE_SRC="${HOME}/.config/llm-sandbox/claude/projects/-workspace"
BASE_DST="${HOME}/.config/llm-sandbox/deepseek-claude/projects/-workspace"

usage() {
    cat <<EOF
Usage: $(basename "$0") <session-uuid>

Copy a session (directory + .jsonl) from:
  ${BASE_SRC}
to:
  ${BASE_DST}
EOF
    exit 1
}

main() {
    local uuid="${1:-}"

    if [[ -z "$uuid" ]]; then
        echo "Error: session UUID required" >&2
        usage
    fi

    local src_dir="${BASE_SRC}/${uuid}"
    local src_jsonl="${BASE_SRC}/${uuid}.jsonl"
    local dst_dir="${BASE_DST}/${uuid}"
    local dst_jsonl="${BASE_DST}/${uuid}.jsonl"

    if [[ ! -d "$src_dir" ]]; then
        echo "Error: session directory not found: ${src_dir}" >&2
        exit 1
    fi

    if [[ ! -f "$src_jsonl" ]]; then
        echo "Error: session .jsonl not found: ${src_jsonl}" >&2
        exit 1
    fi

    if [[ -d "$dst_dir" ]] || [[ -f "$dst_jsonl" ]]; then
        echo "Error: destination already exists for ${uuid}" >&2
        exit 1
    fi

    mkdir -p "$BASE_DST"
    cp -r "$src_dir" "$dst_dir"
    cp "$src_jsonl" "$dst_jsonl"

    echo "Copied ${uuid} -> ${BASE_DST}/${uuid}"
}

main "$@"
