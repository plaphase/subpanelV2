#!/usr/bin/env python3
"""Add the missing CSRF header to the arrow-based source-order save handler."""
from pathlib import Path
import re
import sys


target = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/multisub-panel/templates/user.html")
with target.open("r", encoding="utf-8", newline="") as stream:
    content = stream.read()

move_marker = "async function move(item, direction)"
move_pos = content.find(move_marker)
if move_pos < 0:
    raise SystemExit("Could not locate source reorder controls in user.html")
save_pos = content.rfind("async function saveOrder()", 0, move_pos)
if save_pos < 0:
    raise SystemExit("Could not locate the arrow reorder save handler")
segment = content[save_pos:move_pos]

if "X-CSRFToken" not in segment:
    pattern = re.compile(r'("X-Requested-With"\s*:\s*"XMLHttpRequest")(\s*})')
    segment, replacements = pattern.subn(
        r'\1,\n                    "X-CSRFToken": "{{ csrf_token() }}"\2',
        segment,
        count=1,
    )
    if replacements != 1:
        raise SystemExit("Expected request headers not found; no file was changed")
    content = content[:save_pos] + segment + content[move_pos:]
    with target.open("w", encoding="utf-8", newline="") as stream:
        stream.write(content)
