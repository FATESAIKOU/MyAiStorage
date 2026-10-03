#!/usr/bin/env python3
"""jsonl-shape.py -- print per-line-type counts and top-level field names.

Input:  a Claude Code session jsonl (or subagent jsonl).
Output: `<type> <count> [field, ...]` lines -- shapes only, no content.
Used to pin the schema for the future "Claude -> reading version" converter
(spike V2/V3 notes in docs/spike/claude.md).

Usage: jsonl-shape.py <file.jsonl> [more files...]
"""
import json
import sys
from collections import Counter

types: Counter = Counter()
fields: dict = {}
for path in sys.argv[1:]:
    with open(path) as f:
        for line in f:
            o = json.loads(line)
            t = o.get("type", "<missing>")
            types[t] += 1
            fields.setdefault(t, set()).update(o.keys())

for t in sorted(fields):
    print(t, types[t], sorted(fields[t]))
