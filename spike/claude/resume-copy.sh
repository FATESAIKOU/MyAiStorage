#!/bin/bash
# resume-copy.sh -- V2(a)(b): copy a Claude Code session jsonl to a new uuid.
#
# Rewrites the top-level `sessionId` on EVERY line (all line types carry it),
# keeps the uuid/parentUuid chain and message ids untouched, so that
# `claude --resume <new-uuid>` appends to the new file.
#
# Usage:
#   resume-copy.sh <src-uuid> [new-uuid] [projects-subdir]
#     projects-subdir defaults to the encoded cwd dir of the caller's cwd,
#     i.e. path with '/' -> '-', prefixed with '-', under ~/.claude/projects.
#   Prints the new uuid on stdout.
#
# Design note (spike V2): prefer `claude --resume <id> --fork-session
# --session-id <new-uuid>` (see fork-continue.sh) -- the CLI then does the
# copy itself and normalises sessionId+cwd. Use this script only when you
# need to relocate/hand-edit a jsonl (e.g. import path in design.md 5.2).
set -euo pipefail

SRC="${1:?usage: resume-copy.sh <src-uuid> [new-uuid] [projects-subdir]}"
NEW="${2:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
ENCODED="$(pwd | sed 's|/|-|g; s|^|-|')"
SUBDIR="${3:-$ENCODED}"
BASE="$HOME/.claude/projects/$SUBDIR"

python3 - "$BASE/$SRC.jsonl" "$BASE/$NEW.jsonl" "$SRC" "$NEW" <<'EOF'
import json, sys
src_path, dst_path, old, new = sys.argv[1:5]
n = changed = 0
with open(src_path) as f, open(dst_path, 'w') as g:
    for line in f:
        o = json.loads(line)
        if o.get('sessionId') == old:
            o['sessionId'] = new
            changed += 1
        g.write(json.dumps(o) + '\n')
        n += 1
print(f'lines={n} sessionId_rewritten={changed}', file=sys.stderr)
EOF
echo "$NEW"
