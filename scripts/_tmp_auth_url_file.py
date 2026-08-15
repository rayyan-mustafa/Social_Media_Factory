#!/usr/bin/env python3
"""Ensure youtube_auth writes URL to config/youtube_oauth_url.txt."""
from pathlib import Path

p = Path("src/cli/youtube_auth.py")
t = p.read_text(encoding="utf-8")
if "youtube_oauth_url.txt" in t:
    print("already has url file")
else:
    t = t.replace(
        '            "client_config_path": str(secrets),\n        }',
        '            "client_config_path": str(secrets),\n'
        '            "auth_url": auth_url,\n'
        "        }",
        1,
    )
    snip = (
        "        PENDING_PATH.write_text(\n"
        '            json.dumps(pending, indent=2) + "\\n", encoding="utf-8"\n'
        "        )\n"
        "        console.print(\n"
    )
    repl = (
        "        PENDING_PATH.write_text(\n"
        '            json.dumps(pending, indent=2) + "\\n", encoding="utf-8"\n'
        "        )\n"
        '        url_path = PROJ / "config" / "youtube_oauth_url.txt"\n'
        '        url_path.write_text(auth_url + "\\n", encoding="utf-8")\n'
        '        console.print(f"[dim]URL also saved -> {url_path}[/dim]")\n'
        "        console.print(\n"
    )
    if snip not in t:
        raise SystemExit("snip not found")
    t = t.replace(snip, repl, 1)
    p.write_text(t, encoding="utf-8")
    print("patched")
compile(p.read_text(encoding="utf-8"), str(p), "exec")
print("ok")
