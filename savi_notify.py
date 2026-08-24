#!/usr/bin/env python3
"""
savi-discord-notifier - ping a Discord channel when Savi finishes a task on spawn.co.

Unofficial. Not affiliated with, endorsed by, or supported by Spawn.
It talks to a private, undocumented endpoint, so it can break at any time.

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
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SAVI_CONFIG") or HERE / "config.json")
STATE_PATH = Path(os.environ.get("SAVI_STATE") or HERE / "state.json")

USER_AGENT = "savi-discord-notifier/0.1 (+https://github.com/YOU/savi-discord-notifier)"

# Statuses that mean "this task is over, stop watching it".
DONE_OK = {"done", "complete", "completed", "succeeded", "success", "finished", "ready"}
DONE_BAD = {"failed", "error", "errored", "cancelled", "canceled", "aborted", "timeout"}

log = logging.getLogger("savi")


class AuthExpired(Exception):
    pass


class TransientError(Exception):
    pass


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

    missing = [k for k in ("discord_webhook_url", "spawn") if not cfg.get(k)]
    if missing:
        sys.exit(f"config.json is missing required key(s): {', '.join(missing)}")
    if not cfg["spawn"].get("tasks_url"):
        sys.exit("config.json: spawn.tasks_url is required (see README for how to find it)")
    return cfg


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("state.json unreadable, starting fresh")
        return {"seen": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# --------------------------------------------------------------------------
# tiny helpers
# --------------------------------------------------------------------------

def dig(obj: Any, path: str, default: Any = None) -> Any:
    """Pull a value out with a dotted path: 'data.tasks' or 'result.0.status'."""
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
# spawn
# --------------------------------------------------------------------------

def fetch_tasks(spawn_cfg: dict) -> list:
    """
    Ask spawn.co for the current task list.

    Everything about the request and the response shape comes from config, so
    when Spawn changes their API you edit config.json instead of this file.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    headers.update(spawn_cfg.get("headers", {}))
    if spawn_cfg.get("cookie"):
        headers["Cookie"] = spawn_cfg["cookie"]
    if spawn_cfg.get("bearer_token"):
        headers["Authorization"] = "Bearer " + spawn_cfg["bearer_token"]

    body = None
    method = spawn_cfg.get("method", "GET").upper()
    if spawn_cfg.get("body"):
        body = json.dumps(spawn_cfg["body"]).encode()
        headers.setdefault("Content-Type", "application/json")

    status, data = http_json(spawn_cfg["tasks_url"], headers=headers, method=method, body=body)

    if status in (401, 403):
        raise AuthExpired(f"spawn.co returned {status} - your session token has expired")
    if status >= 400:
        raise TransientError(f"spawn.co returned {status}: {str(data)[:200]}")

    tasks = dig(data, spawn_cfg.get("tasks_path", ""), default=data)
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    if not isinstance(tasks, list):
        raise TransientError(
            "Expected a list of tasks at path '{}', got {}. "
            "Check spawn.tasks_path in config.json.".format(
                spawn_cfg.get("tasks_path", ""), type(tasks).__name__)
        )
    return [t for t in tasks if isinstance(t, dict)]


# --------------------------------------------------------------------------
# discord
# --------------------------------------------------------------------------

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


def build_embed(task: dict, fields: dict, ok: bool) -> dict:
    title = dig(task, fields.get("title", "title")) or "Untitled task"
    status = dig(task, fields.get("status", "status")) or "unknown"
    url = dig(task, fields.get("url", "url"))

    embed = {
        "title": "Savi finished" if ok else "Savi hit a problem",
        "description": f"**{title}**",
        "color": 0x2ECC71 if ok else 0xE74C3C,
        "footer": {"text": f"status: {status}"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
    }
    if isinstance(url, str) and url.startswith("http"):
        embed["url"] = url
    return {"embeds": [embed]}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def check_once(cfg: dict, state: dict, seed_only: bool = False) -> int:
    spawn_cfg = cfg["spawn"]
    fields = spawn_cfg.get("fields", {})
    id_field = fields.get("id", "id")
    status_field = fields.get("status", "status")

    tasks = fetch_tasks(spawn_cfg)
    seen = state.setdefault("seen", {})
    notified = 0

    for task in tasks:
        tid = dig(task, id_field)
        if tid is None:
            continue
        tid = str(tid)
        status = str(dig(task, status_field, "") or "").lower()
        previous = seen.get(tid)

        if status in DONE_OK or status in DONE_BAD:
            already_done = previous in DONE_OK or previous in DONE_BAD
            if not already_done and not seed_only:
                log.info("Task %s -> %s, notifying", tid, status)
                post_discord(cfg["discord_webhook_url"],
                             build_embed(task, fields, ok=status in DONE_OK))
                notified += 1
        seen[tid] = status

    # Don't let state.json grow forever.
    if len(seen) > 500:
        for k in list(seen)[:-500]:
            del seen[k]

    save_state(state)
    return notified


def main() -> None:
    ap = argparse.ArgumentParser(description="Ping Discord when Savi finishes a task.")
    ap.add_argument("--once", action="store_true", help="check a single time and exit")
    ap.add_argument("--test-discord", action="store_true",
                    help="send a test message to your webhook and exit")
    ap.add_argument("--dump", action="store_true",
                    help="print the raw response from spawn.co and exit (for setup)")
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

    if args.dump:
        tasks = fetch_tasks(cfg["spawn"])
        print(json.dumps(tasks[:5], indent=2)[:4000])
        log.info("Got %d task(s). Use these field names for spawn.fields in config.json.",
                 len(tasks))
        return

    state = load_state()
    interval = int(cfg.get("poll_seconds", 60))

    # First run: learn what's already there instead of blasting the channel with
    # notifications for tasks that finished days ago.
    if not state.get("seen") and not cfg.get("notify_on_first_run", False):
        log.info("First run - recording current tasks without notifying.")
        try:
            check_once(cfg, state, seed_only=True)
        except (AuthExpired, TransientError) as e:
            sys.exit(str(e))

    if args.once:
        check_once(cfg, state)
        return

    log.info("Watching for finished Savi tasks every %ds. Ctrl+C to stop.", interval)
    backoff = 0
    while True:
        try:
            check_once(cfg, state)
            backoff = 0
        except AuthExpired as e:
            log.error("%s", e)
            post_discord(cfg["discord_webhook_url"], {
                "content": ":warning: **savi-discord-notifier stopped.** Your spawn.co "
                           "session expired - grab a fresh token and restart it."
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
