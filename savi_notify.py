#!/usr/bin/env python3
"""
savi-discord-notifier - mirror your spawn.co notifications into Discord.

Spawn already builds the notifications we want ("savi finished building in
MONSTER O'CLOCK"). This just forwards them, so you can close the app.

Unofficial. Not affiliated with, endorsed by, or supported by Spawn.
It reads a private backend, so it can break whenever they ship a change.

Standard library only. Python 3.9+. No pip install needed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SAVI_CONFIG") or HERE / "config.json")
STATE_PATH = Path(os.environ.get("SAVI_STATE") or HERE / "state.json")

USER_AGENT = "savi-discord-notifier/0.2 (+https://github.com/YOU/savi-discord-notifier)"

DEFAULT_BASE_URL = "https://kiln.spawn.co"

# Column names we'll try for the message text if config doesn't say.
TEXT_GUESSES = ("title", "body", "message", "text", "description", "content")

log = logging.getLogger("savi")


class AuthExpired(Exception):
    """Access token is dead and we could not refresh it."""


class TransientError(Exception):
    """Something went wrong that is probably worth retrying."""


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"No config found at {CONFIG_PATH}\n"
            "Copy config.example.json to config.json and fill it in."
        )
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"config.json is not valid JSON: {e}")

    if not cfg.get("discord_webhook_url"):
        sys.exit("config.json: discord_webhook_url is required")

    spawn = cfg.get("spawn") or {}
    for key in ("user_id", "apikey"):
        if not spawn.get(key):
            sys.exit(f"config.json: spawn.{key} is required (see README step 2)")
    if not spawn.get("access_token") and not spawn.get("refresh_token"):
        sys.exit("config.json: spawn needs an access_token, a refresh_token, or both")
    return cfg


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("state.json unreadable, starting fresh")
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)
    # The rotated refresh token lives in here.
    try:
        os.chmod(STATE_PATH, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# tiny helpers
# --------------------------------------------------------------------------

def dig(obj: Any, path: str, default: Any = None) -> Any:
    """Pull a value out with a dotted path: 'data.game_name' or 'items.0.id'."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return default
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def http_json(url, *, headers, method="GET", body=None, timeout=20):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# spawn / kiln
# --------------------------------------------------------------------------

class SpawnClient:
    """
    Talks to Spawn's backend (a Supabase/PostgREST deployment at kiln.spawn.co).

    Access tokens are short lived, so we refresh them ourselves rather than
    making you re-paste one every hour. Supabase rotates the refresh token on
    each use, so the new one gets written back to state.json.
    """

    def __init__(self, spawn_cfg: dict, state: dict):
        self.cfg = spawn_cfg
        self.state = state
        self.base = spawn_cfg.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        self.apikey = spawn_cfg["apikey"]
        self.user_id = spawn_cfg["user_id"]
        self.table = spawn_cfg.get("table", "notifications")
        self.access_token = spawn_cfg.get("access_token", "")
        # A rotated token in state beats the one originally pasted into config.
        self.refresh_token = state.get("refresh_token") or spawn_cfg.get("refresh_token", "")

    # -- auth ------------------------------------------------------------

    def refresh(self) -> bool:
        if not self.refresh_token:
            return False
        url = f"{self.base}/auth/v1/token?grant_type=refresh_token"
        headers = {
            "apikey": self.apikey,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        body = json.dumps({"refresh_token": self.refresh_token}).encode()
        status, data = http_json(url, headers=headers, method="POST", body=body)

        if status >= 400 or not isinstance(data, dict):
            log.debug("Token refresh failed (%s): %s", status, str(data)[:200])
            return False

        token = data.get("access_token")
        if not token:
            return False

        self.access_token = token
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]
            self.state["refresh_token"] = self.refresh_token
            save_state(self.state)
        log.info("Refreshed access token.")
        return True

    # -- data ------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "apikey": self.apikey,
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _notifications_url(self) -> str:
        limit = int(self.cfg.get("limit", 50))
        params = [
            ("select", "*"),
            ("user_id", f"eq.{self.user_id}"),
            ("order", "created_at.desc,id.desc"),
            ("limit", str(limit)),
        ]
        # Matches what the web app asks for; skips things you've archived.
        if self.cfg.get("skip_archived", True):
            params.insert(2, ("status", "neq.archived"))
        query = urllib.parse.urlencode(params, safe=".*,")
        return f"{self.base}/rest/v1/{self.table}?{query}"

    def fetch_notifications(self, _retried: bool = False) -> list:
        status, data = http_json(self._notifications_url(), headers=self._headers())

        if status in (401, 403):
            if not _retried and self.refresh():
                return self.fetch_notifications(_retried=True)
            raise AuthExpired(
                f"kiln.spawn.co returned {status} and the token could not be refreshed"
            )
        if status >= 400:
            raise TransientError(f"kiln.spawn.co returned {status}: {str(data)[:200]}")
        if not isinstance(data, list):
            raise TransientError(f"Expected a list of rows, got {type(data).__name__}")
        return [row for row in data if isinstance(row, dict)]


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def notification_text(row: dict, fields: dict) -> str:
    """Best effort at a human sentence for this notification."""
    configured = fields.get("text")
    if configured:
        value = dig(row, configured)
        if value:
            return str(value)

    for key in TEXT_GUESSES:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Nothing obvious - show the payload rather than an empty message.
    payload = row.get("data") or row.get("payload")
    if payload:
        return json.dumps(payload, ensure_ascii=False)[:300]
    return json.dumps({k: v for k, v in row.items()
                       if k not in ("id", "user_id")}, ensure_ascii=False)[:300]


def build_payload(row: dict, fields: dict, cfg: dict) -> dict:
    text = notification_text(row, fields)
    kind = str(row.get(fields.get("type", "type"), "") or "")

    embed = {
        "description": text,
        "color": 0xF97316,  # Savi's flame orange
        "timestamp": row.get("created_at"),
    }
    if kind:
        embed["footer"] = {"text": kind}

    link = dig(row, fields.get("url", "url")) or dig(row, "data.url")
    if isinstance(link, str) and link.startswith("http"):
        embed["url"] = link
        embed["title"] = "Open in Spawn"

    payload = {"embeds": [embed]}
    if cfg.get("mention"):
        payload["content"] = cfg["mention"]
    return payload


def wanted(row: dict, spawn_cfg: dict) -> bool:
    """Apply the include/exclude type filters, if the user set any."""
    type_field = spawn_cfg.get("fields", {}).get("type", "type")
    kind = str(dig(row, type_field, "") or "").lower()

    include = [t.lower() for t in spawn_cfg.get("only_types", []) if t]
    exclude = [t.lower() for t in spawn_cfg.get("ignore_types", []) if t]

    if include and kind not in include:
        return False
    if kind in exclude:
        return False
    return True


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def check_once(client: SpawnClient, cfg: dict, state: dict, seed_only: bool = False) -> int:
    spawn_cfg = cfg["spawn"]
    fields = spawn_cfg.get("fields", {})

    rows = client.fetch_notifications()
    seen = state.setdefault("seen_ids", [])
    seen_set = set(seen)
    sent = 0

    # Server gives newest first; deliver oldest first so Discord reads in order.
    for row in reversed(rows):
        rid = row.get(fields.get("id", "id"))
        if rid is None:
            continue
        rid = str(rid)
        if rid in seen_set:
            continue

        seen.append(rid)
        seen_set.add(rid)

        if seed_only or not wanted(row, spawn_cfg):
            continue

        log.info("New notification %s: %s", rid, notification_text(row, fields)[:80])
        post_discord(cfg["discord_webhook_url"], build_payload(row, fields, cfg))
        sent += 1

    # Keep the dedup list bounded; 500 is ten pages of history.
    if len(seen) > 500:
        del seen[:-500]

    save_state(state)
    return sent


def post_discord(webhook: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    status, data = http_json(webhook, headers=headers, method="POST", body=body)
    if status == 429:
        wait = float(dig(data, "retry_after", 5) or 5)
        log.warning("Discord rate limited us, sleeping %.1fs", wait)
        time.sleep(wait)
        http_json(webhook, headers=headers, method="POST", body=body)
    elif status >= 400:
        log.error("Discord webhook failed (%s): %s", status, str(data)[:300])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mirror your spawn.co notifications into Discord.")
    ap.add_argument("--once", action="store_true", help="check a single time and exit")
    ap.add_argument("--test-discord", action="store_true",
                    help="send a test message to your webhook and exit")
    ap.add_argument("--dump", action="store_true",
                    help="print your latest notifications as raw JSON and exit")
    ap.add_argument("--types", action="store_true",
                    help="list the notification types in your feed, for filtering")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config()

    if args.test_discord:
        post_discord(cfg["discord_webhook_url"], {
            "embeds": [{
                "title": "savi-discord-notifier is wired up",
                "description": "If you can read this, your webhook works.",
                "color": 0x5865F2,
            }]
        })
        log.info("Test message sent - go check your Discord channel.")
        return

    state = load_state()
    client = SpawnClient(cfg["spawn"], state)

    if args.dump:
        rows = client.fetch_notifications()
        print(json.dumps(rows[:5], indent=2, ensure_ascii=False)[:6000])
        log.info("Got %d notification(s).", len(rows))
        return

    if args.types:
        rows = client.fetch_notifications()
        counts: dict = {}
        for row in rows:
            counts[str(row.get("type", "(no type field)"))] = \
                counts.get(str(row.get("type", "(no type field)")), 0) + 1
        for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{n:4d}  {kind}")
        log.info("Put the ones you want in spawn.only_types in config.json.")
        return

    interval = int(cfg.get("poll_seconds", 60))

    # First run: learn what's already in the feed instead of dumping your
    # entire notification history into the channel at once.
    if "seen_ids" not in state and not cfg.get("notify_on_first_run", False):
        log.info("First run - recording existing notifications without sending them.")
        try:
            check_once(client, cfg, state, seed_only=True)
        except (AuthExpired, TransientError) as e:
            sys.exit(str(e))

    if args.once:
        check_once(client, cfg, state)
        return

    log.info("Watching your Spawn notifications every %ds. Ctrl+C to stop.", interval)
    backoff = 0
    while True:
        try:
            check_once(client, cfg, state)
            backoff = 0
        except AuthExpired as e:
            log.error("%s", e)
            post_discord(cfg["discord_webhook_url"], {
                "content": ":warning: **savi-discord-notifier stopped.** Your Spawn login "
                           "expired and couldn't be refreshed - grab a fresh token "
                           "(README step 2) and restart it."
            })
            sys.exit(1)
        except TransientError as e:
            backoff = min(backoff + 1, 6)
            log.warning("%s (retry %d)", e, backoff)
        except urllib.error.URLError as e:
            backoff = min(backoff + 1, 6)
            log.warning("Network problem: %s (retry %d)", e.reason, backoff)
        except Exception:
            backoff = min(backoff + 1, 6)
            log.exception("Unexpected error")

        # Jitter so we are not a perfectly predictable load on their servers.
        delay = interval * (2 ** backoff) if backoff else interval
        time.sleep(min(delay, 900) + random.uniform(0, 3))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
