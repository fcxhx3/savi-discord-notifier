#!/usr/bin/env python3
"""
Paste your Spawn session cookie in, safely.

The session lives in cookies named sb-spawn-auth-token.0, .1, ... and the
combined value is several KB of base64. Hand-editing that into JSON is a
reliable way to produce a broken config, so this does it for you.

    python setup_session.py

Nothing is sent anywhere. It writes to your local config.json and prints
only masked values.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import savi_notify as sn


def mask(token: str) -> str:
    return f"{token[:8]}...{token[-6:]} ({len(token)} chars)" if token else "(none)"


def token_expiry(access_token: str):
    """Decode the JWT payload just to show when it runs out. No verification."""
    import base64
    import datetime
    try:
        payload = access_token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.datetime.fromtimestamp(data["exp"], datetime.timezone.utc)
    except Exception:
        return None


def main() -> None:
    config_path = sn.CONFIG_PATH
    if not config_path.exists():
        sys.exit(f"No config.json at {config_path}. Copy config.example.json first.")

    print(__doc__.strip().split("\n\n")[1])
    print()
    print("In Chrome: F12 > Application > Cookies > https://www.spawn.co")
    print("Click sb-spawn-auth-token.0, select the whole Cookie Value, copy it.")
    print()
    print("Paste chunk .0 below, press Enter, paste chunk .1, press Enter,")
    print("then press Enter once more on an empty line to finish.")
    print()

    chunks = []
    while True:
        try:
            line = input(f"chunk {len(chunks)}> ").strip()
        except EOFError:
            break
        if not line:
            break
        chunks.append(line)

    if not chunks:
        sys.exit("Nothing pasted - aborted, config.json untouched.")

    combined = "".join(chunks)

    try:
        access, refresh = sn.parse_session_cookie(combined)
    except ValueError as e:
        sys.exit(f"\nThat didn't parse:\n  {e}\n\nconfig.json was not changed.")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("spawn", {})["session_cookie"] = combined
    # Stale explicit tokens would otherwise win over the fresh cookie.
    config["spawn"]["access_token"] = ""
    config["spawn"]["refresh_token"] = ""
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    print()
    print(f"  access_token   {mask(access)}")
    print(f"  refresh_token  {mask(refresh)}")
    expiry = token_expiry(access)
    if expiry:
        print(f"  expires        {expiry:%Y-%m-%d %H:%M} UTC "
              f"(refreshed automatically from here on)")
    print()
    print(f"Written to {config_path}")
    print()
    print("Next:  python savi_notify.py --test-discord")
    print("Then:  python savi_notify.py --dump")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted - config.json untouched.")
