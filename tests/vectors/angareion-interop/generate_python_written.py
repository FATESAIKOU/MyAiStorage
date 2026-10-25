"""Regenerate python-written/*.md and expected.json from inputs.json with MyAiStorage's own code.

Usage (MyAiStorage commit 8d97ebc checked out or exported to <storage-src>):
    python3 -I generate_python_written.py <MyAiStorage>/src <this directory>

Messages are produced by the real `serialize_message`; the recipient / mention
filter results in expected.json come from the real `recipient_matches` /
`mention_matches` in `angareion.backend`. Nothing here is hand-computed.
"""
import json
import sys
from pathlib import Path

storage_src, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(storage_src))

from angareion.backend import mention_matches, recipient_matches  # noqa: E402
from angareion.messages import Message, parse_message, serialize_message  # noqa: E402

READERS = [
    "mypmo-pm-m1",
    "mylinuxpool-impl-w1-1",
    "myaientry-secretary-p1",
    "mylinuxpool-rev-w1-1",
    "mylinuxpool-test-w1-1",
]

inputs = json.loads((out / "inputs.json").read_text())
(out / "python-written").mkdir(exist_ok=True)
messages, readers = {}, {r: {} for r in READERS}
for item in inputs:
    msg = Message(
        from_=item["from"], to=item["to"], channel=item["channel"], urgency=item["urgency"],
        content=item["content"], attachments=item["attachments"], id=item["id"], at=item["at"],
        mentions=item["mentions"], in_reply_to=item["in_reply_to"],
    )
    text = serialize_message(msg)
    (out / "python-written" / item["file"]).write_text(text)
    parsed = parse_message(text, strict=True)
    messages[item["file"]] = {
        "id": parsed.id, "at": parsed.at, "from": parsed.from_, "to": parsed.to,
        "channel": parsed.channel, "urgency": parsed.urgency, "mentions": parsed.mentions,
        "in_reply_to": parsed.in_reply_to, "attachments": parsed.attachments, "content": parsed.content,
    }
    for r in READERS:
        readers[r][item["file"]] = {
            "recipient": recipient_matches(parsed, r),
            "mention": mention_matches(parsed, r),
        }
(out / "expected.json").write_text(
    json.dumps({"messages": messages, "readers": readers}, indent=2, ensure_ascii=False) + "\n"
)
