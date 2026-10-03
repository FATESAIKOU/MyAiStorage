#!/bin/bash
# fork-continue.sh -- V2(c): continue a Claude session under a KNOWN new id.
#
# Wraps `claude --resume <id> --fork-session --session-id <new-uuid>
# --output-format json`, then prints the resulting session_id (== new-uuid).
# The new jsonl lands in ~/.claude/projects/<encoded-launch-cwd>/<new>.jsonl,
# so launch this from the intended project dir (design.md: --dir).
#
# Usage:
#   fork-continue.sh <src-uuid> [new-uuid] -- <prompt...>
#   Prints: <new-jsonl-path>
#   (prompt goes to the agent on stdin/args; agent stdout is discarded --
#   use --output-format text variant manually for interactive use.)
set -euo pipefail

SRC="${1:?usage: fork-continue.sh <src-uuid> [new-uuid] -- <prompt...>}"
NEW="${2:?usage: fork-continue.sh <src-uuid> [new-uuid] -- <prompt...>}"
shift 2
if [ "${1:-}" = "--" ]; then shift; fi

OUT="$(claude --resume "$SRC" --fork-session --session-id "$NEW" \
  --output-format json -p "$*" | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')"
ENCODED="$(pwd | sed 's|/|-|g; s|^|-|')"
echo "$HOME/.claude/projects/$ENCODED/$OUT.jsonl"
