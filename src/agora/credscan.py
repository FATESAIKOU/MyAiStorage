"""Credential scan before an import (#189): pattern names and counts, never the value.

`python -m agora.credscan FILE... [--claude-session ID]` reads jsonl or plain
text and reports which credential formats appear in it. The report holds the
pattern name, a count, and for each hit its length, how many character kinds
it uses and a short sha256 of the value (to tell "the same value again" from
"another value"). It never prints a value or any text around it.

Two things keep screenshots from raising false alarms: a jsonl line is parsed
and the image payloads (`source.data` of an `image` block, any `base64` field)
are skipped; and any hit with base64 characters touching it on both sides, the
two runs together `BASE64_RUN` or more, is dropped as letters the encoder
happened to produce. A real token has whitespace, a quote or a line end on at
least one side, so its own length never counts. A JSON key named password,
client_secret, refresh_token or access_token (any case) with a string value
of 8 or more characters is reported as `secret-field`. A line that is not
JSON, or nests too deeply to parse, is scanned as plain text.

Exit codes: 0 nothing found, 1 something found, 2 a file could not be read.
This is not wired into `agora import`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

BASE64_RUN = 200
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
PROJECTS_ENV = "AGORA_CLAUDE_PROJECTS"

_TOKEN = r"[A-Za-z0-9_-]"
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx))
    for name, rx in (
        ("google-access-token", rf"ya29\.{_TOKEN}{{20,}}"),
        ("google-refresh-token", rf"1//0{_TOKEN}{{20,}}"),
        ("google-client-secret", rf"GOCSPX-{_TOKEN}{{20,}}"),
        ("github-token", r"gh[pous]_[A-Za-z0-9]{30,}"),
        ("github-pat", r"github_pat_[A-Za-z0-9_]{30,}"),
        ("anthropic-key", rf"sk-ant-{_TOKEN}{{20,}}"),
        ("sk-key", rf"sk-{_TOKEN}{{20,}}"),
        ("aws-access-key", r"AKIA[0-9A-Z]{16}"),
        ("slack-token", r"xox[bap]-[A-Za-z0-9-]{10,}"),
        ("google-api-key", rf"AIza{_TOKEN}{{35}}"),
        ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    )
) + (
    (
        "secret-field",
        re.compile(
            r"""(?ix)(?<![A-Za-z0-9])(?:password|client_secret|refresh_token|access_token)(?![A-Za-z0-9_])
            \\?["']?\s*[:=]\s*\\?["']?([^\s"'\\,}{]{8,})"""
        ),
    ),
)
_B64 = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")


SECRET_KEY = re.compile(r"(?<![a-z0-9])(?:password|client_secret|refresh_token|access_token)$")
_B64_TEXT = _B64 | frozenset("=\r\n")


def _skip_key(parent: dict, key: str) -> bool:
    value = parent[key]
    if key == "data":
        return parent.get("type") == "base64"
    return key == "base64" and isinstance(value, str) and all(c in _B64_TEXT for c in value)


def _walk(node):
    """(string, is_secret_field_value) for every string worth scanning in a parsed line."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, str):
            yield n, False
        elif isinstance(n, dict):
            for k, v in n.items():
                if _skip_key(n, k):
                    continue
                if isinstance(v, str) and len(v) >= 8 and SECRET_KEY.search(str(k).lower()):
                    yield v, True
                stack.append(v)
        elif isinstance(n, list):
            stack.extend(n)


def _in_base64_run(text: str, start: int, end: int) -> bool:
    left = start
    while left > 0 and text[left - 1] in _B64:
        left -= 1
    right = end
    while right < len(text) and text[right] in _B64:
        right += 1
    return left < start and right > end and (start - left) + (right - end) >= BASE64_RUN


def _kinds(value: str) -> int:
    return sum(
        (
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
            any(not c.isalnum() for c in value),
        )
    )


def scan_text(text: str) -> list[tuple[str, int, int, str]]:
    """(pattern, length, char kinds, hash8) per hit; the value itself is not returned."""
    hits: list[tuple[str, int, int, str]] = []
    taken: list[tuple[int, int]] = []
    for name, rx in PATTERNS:
        for m in rx.finditer(text):
            gi = 1 if rx.groups else 0
            s, e = m.span(gi)
            if any(s < te and ts < e for ts, te in taken):
                continue
            if _in_base64_run(text, *m.span()):
                continue
            taken.append((s, e))
            value = m.group(gi)
            hits.append((name, len(value), _kinds(value), hashlib.sha256(value.encode()).hexdigest()[:8]))
    return hits


def scan_line(line: str) -> list[tuple[str, int, int, str]]:
    try:
        node = json.loads(line)
    except (ValueError, RecursionError):
        return scan_text(line)
    if not isinstance(node, (dict, list)):
        return scan_text(line)
    hits = []
    for s, is_field in _walk(node):
        if is_field:
            hits.append(("secret-field", len(s), _kinds(s), hashlib.sha256(s.encode()).hexdigest()[:8]))
        else:
            hits.extend(scan_text(s))
    return hits


def scan_file(path: Path) -> list[tuple[int, str, int, int, str]]:
    """(line number, pattern, length, char kinds, hash8) per hit. Raises OSError."""
    hits = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            hits.extend((n, *h) for h in scan_line(line.rstrip("\n")))
    return hits


def claude_session_paths(session_id: str, projects: Path) -> list[Path]:
    """`<projects>/*/<id>.jsonl` plus every file under the sidecar `<projects>/*/<id>/`."""
    found: list[Path] = []
    for proj in sorted(p for p in projects.glob("*") if p.is_dir()):
        jsonl = proj / f"{session_id}.jsonl"
        if jsonl.is_file():
            found.append(jsonl)
        side = proj / session_id
        if side.is_dir():
            found.extend(
                sorted(
                    p for p in side.rglob("*")
                    if p.is_file() and p.suffix.lower() not in IMAGE_SUFFIXES
                )
            )
    return found


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="agora.credscan", description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--claude-session", metavar="ID")
    ap.add_argument("--claude-projects", type=Path, help=f"default: ${PROJECTS_ENV} or ~/.claude/projects")
    args = ap.parse_args(argv)

    paths = list(args.files)
    if args.claude_session:
        projects = args.claude_projects or Path(
            os.environ.get(PROJECTS_ENV) or Path.home() / ".claude" / "projects"
        )
        found = claude_session_paths(args.claude_session, projects)
        if not found:
            print(f"no session file for {args.claude_session}", file=sys.stderr)
            return 2
        paths.extend(found)
    if not paths:
        ap.error("give a file or --claude-session")

    status = 0
    for path in paths:
        try:
            hits = scan_file(path)
        except OSError as exc:
            print(f"{path}: cannot read ({exc.strerror or type(exc).__name__})", file=sys.stderr)
            status = 2
            continue
        if not hits:
            continue
        if status == 0:
            status = 1
        counts: dict[str, int] = {}
        for _, name, *_ in hits:
            counts[name] = counts.get(name, 0) + 1
        print(f"{path}: {len(hits)} hit(s)", file=out)
        for name, n in sorted(counts.items()):
            print(f"  {name}: {n}", file=out)
        for line_no, name, length, kinds, h in hits:
            print(f"    line {line_no} {name} len={length} kinds={kinds} sha256={h}", file=out)
    return status


if __name__ == "__main__":
    sys.exit(main())
