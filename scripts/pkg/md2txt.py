#!/usr/bin/env python3
"""Tiny Markdown -> plain text (Notepad-friendly: CRLF line endings, no markup characters) for INSTALL-OFFLINE.txt."""
import re, sys
out = []
for line in open(sys.argv[1], encoding="utf8").read().splitlines():
    if line.startswith("```"):
        continue
    line = re.sub(r"^#{1,6}\s*(.*)$", lambda m: "\n" + m.group(1).upper() + "\n" + "=" * len(m.group(1)), line)
    line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
    line = re.sub(r"(?<![`\w])`([^`]+)`", r"\1", line)
    line = line.replace("`", "")
    out.append(line)
sys.stdout.buffer.write(("\r\n".join(out) + "\r\n").encode("utf8"))
