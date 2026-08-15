#!/usr/bin/env python3
"""Keep never/not in gold thumb text so 'Never Fell' does not become 'Fell'."""

from pathlib import Path

path = Path("src/services/thumbnail_prompt.py")
text = path.read_text(encoding="utf-8")
old = '''        "done",
        "never",
        "not",
        "the",
'''
new = '''        "done",
        # Keep never/not — dropping them inverts meaning (e.g. Never Fell -> Fell).
        "the",
'''
if old not in text:
    raise SystemExit("stopword snippet not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("kept never/not in gold phrases")
