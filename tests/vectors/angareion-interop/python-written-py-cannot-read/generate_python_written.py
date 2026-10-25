"""Regenerate python-written/*.md from inputs.json with MyAiStorage's own `serialize_message`.

Usage (MyAiStorage commit 8d97ebc exported to <storage-src>):
    python3 -I generate_python_written.py <MyAiStorage>/src <this directory>

Only `serialize_message` is called. `parse_message` is NOT called on purpose:
at 8d97ebc it cannot read these messages back (it returns None).
"""
import json
import sys
from pathlib import Path

storage_src, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(storage_src))

from angareion.messages import Message, serialize_message  # noqa: E402

(out / "python-written").mkdir(exist_ok=True)
for item in json.loads((out / "inputs.json").read_text()):
    msg = Message(
        from_=item["from"], to=item["to"], channel=item["channel"], urgency=item["urgency"],
        content=item["content"], attachments=item["attachments"], id=item["id"], at=item["at"],
        mentions=item["mentions"], in_reply_to=item["in_reply_to"],
    )
    (out / "python-written" / item["file"]).write_text(serialize_message(msg))
