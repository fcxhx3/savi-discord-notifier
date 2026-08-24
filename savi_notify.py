#!/usr/bin/env python3
"""
Posts your spawn.co notifications to a Discord webhook.

Savi keeps building after you close the app, which is the whole point, except
there's then no way to find out she's done without opening it again. Spawn
already writes a notification when she finishes, so this polls for those and
forwards them.

Nothing official about this. It reads an endpoint nobody documented, so it
will break eventually. There's a table in the README for when it does.

Stdlib only, Python 3.9+.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import subprocess
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

USER_AGENT = "savi-discord-notifier/0.1.0 (+https://github.com/fcxhx3/savi-discord-notifier)"

DEFAULT_BASE_URL = "https://kiln.spawn.co"
DEFAULT_WEB_URL = "https://www.spawn.co"

# The sentence you want is in `message`, and what it's about is in `kind`.
# Don't reach for `type` there - it says "redirect" on nearly every row and
# tells you nothing. Took me an embarrassingly long time to spot that.
DEFAULT_TEXT_FIELD = "message"
DEFAULT_TYPE_FIELD = "kind"
DEFAULT_URL_FIELD = "action_data"

# plain = a single line, embed = the boxed card with the coloured bar
DEFAULT_STYLE = "plain"
DEFAULT_LINK_LABEL = "Open in Spawn"

# only get used if they rename the column on us
TEXT_GUESSES = ("message", "title", "body", "text", "description", "content")

log = logging.getLogger("savi")


class AuthExpired(Exception):
    """Token expired and refreshing it didn't work either."""


class TransientError(Exception):
    """Went wrong, but probably worth another go in a minute."""


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
    if not any(spawn.get(k) for k in ("session_cookie", "access_token", "refresh_token")):
        sys.exit(
            "config.json: spawn needs a session_cookie (easiest - see README step 2), "
            "or an access_token / refresh_token pair"
        )
    return cfg


def warn_if_tracked() -> list:
    """
    Shout if config.json or state.json has ended up in git.

    Both hold a live login to your Spawn account. Committing one is the kind
    of mistake you only make once, and it's a lot easier to catch here than
    after it's been pushed somewhere public.
    """
    if not (HERE / ".git").exists():
        return []
    try:
        done = subprocess.run(
            ["git", "-C", str(HERE), "ls-files", "--", CONFIG_PATH.name, STATE_PATH.name],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []          # no git, or it took too long. not worth caring about

    tracked = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if tracked:
        names = " ".join(tracked)
        log.warning("=" * 68)
        log.warning("%s is tracked by git.", names)
        log.warning("That file holds a live login to your Spawn account. Get it out")
        log.warning("before you push anywhere:")
        log.warning("    git rm --cached %s", names)
        log.warning("and check it is listed in .gitignore.")
        log.warning("=" * 68)
    return tracked


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
    # the refresh token ends up in here, so keep it to ourselves
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


def parse_session_cookie(raw: str) -> tuple:
    """
    Dig (access_token, refresh_token) out of the Supabase auth cookie.

    Spawn uses @supabase/ssr, so the session lives in a cookie and not in
    local storage, which is not where I went looking first. The value is
    URL-encoded, usually carries a 'base64-' prefix, and gets split into
    .0 / .1 chunks once it outgrows the 4KB cookie limit. You paste it in
    raw and this untangles it.
    """
    if not raw:
        return "", ""

    # people paste the chunks back to back and usually catch a newline in
    # between. cookie values can't hold whitespace anyway, so strip the lot
    value = "".join(raw.split()).strip('"')
    value = urllib.parse.unquote(value)

    # the chunks are just glued together, so lose any repeated prefix
    if value.count("base64-") > 1:
        value = "".join(part for part in value.split("base64-") if part)
        value = "base64-" + value

    if value.startswith("base64-"):
        encoded = value[len("base64-"):]
        try:
            value = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError(
                "Could not decode the session cookie. It is normally split into "
                "numbered chunks (.0, .1, ...) - paste every chunk, in order, "
                f"joined into one string. ({e})"
            )

    try:
        data = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(
            "Session cookie didn't contain readable JSON. If the cookie was split "
            f"into numbered chunks, paste all of them joined together. ({e})"
        )

    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("Session cookie JSON wasn't an object")

    access = data.get("access_token", "")
    refresh = data.get("refresh_token", "")
    if not access and not refresh:
        raise ValueError("Session cookie had no access_token or refresh_token in it")
    return access, refresh


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
    Talks to kiln.spawn.co, which is a Supabase/PostgREST setup.

    Access tokens only last about an hour. Rather than make you paste a fresh
    one in that often, this renews them itself. Supabase hands back a new
    refresh token every time it does, and that goes into state.json.
    """

    def __init__(self, spawn_cfg: dict, state: dict):
        self.cfg = spawn_cfg
        self.state = state
        self.base = spawn_cfg.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        self.apikey = spawn_cfg["apikey"]
        self.user_id = spawn_cfg["user_id"]
        self.table = spawn_cfg.get("table", "notifications")
        self.access_token = spawn_cfg.get("access_token", "")
        config_refresh = spawn_cfg.get("refresh_token", "")

        # far easier to paste the raw sb-*-auth-token cookie and unpick it
        # here than to talk someone through decoding base64 by hand
        if spawn_cfg.get("session_cookie"):
            cookie_access, cookie_refresh = parse_session_cookie(spawn_cfg["session_cookie"])
            self.access_token = self.access_token or cookie_access
            config_refresh = config_refresh or cookie_refresh

        # whatever we rotated to last is newer than what's sat in the config
        self.refresh_token = state.get("refresh_token") or config_refresh

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
        # same query the site makes, minus anything you've archived
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
    """Get a readable sentence out of the row, one way or another."""
    configured = fields.get("text") or DEFAULT_TEXT_FIELD
    if configured:
        value = dig(row, configured)
        if value:
            return str(value)

    for key in TEXT_GUESSES:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # nothing obvious in there, so show the payload rather than a blank line
    payload = row.get("data") or row.get("payload")
    if payload:
        return json.dumps(payload, ensure_ascii=False)[:300]
    return json.dumps({k: v for k, v in row.items()
                       if k not in ("id", "user_id")}, ensure_ascii=False)[:300]


def resolve_link(row: dict, fields: dict, cfg: dict) -> str:
    """Full URL for this notification, or an empty string if there isn't one."""
    link = dig(row, fields.get("url") or DEFAULT_URL_FIELD) or dig(row, "data.url")
    if not isinstance(link, str) or not link:
        return ""
    # action_data is a path like "/app/<uuid>?panel=chat", not a whole URL
    if link.startswith("/"):
        link = cfg.get("web_base_url", DEFAULT_WEB_URL).rstrip("/") + link
    return link if link.startswith("http") else ""


def build_payload(row: dict, fields: dict, cfg: dict) -> dict:
    text = notification_text(row, fields)
    link = resolve_link(row, fields, cfg)
    mention = str(cfg.get("mention") or "").strip()

    if str(cfg.get("style") or DEFAULT_STYLE).lower() == "embed":
        embed = {
            "description": text,
            "color": 0xF97316,  # Savi's flame orange
            "timestamp": row.get("created_at"),
        }
        kind = str(dig(row, fields.get("type") or DEFAULT_TYPE_FIELD, "") or "")
        if kind:
            embed["footer"] = {"text": kind}
        if link:
            embed["url"] = link
            embed["title"] = cfg.get("link_label", DEFAULT_LINK_LABEL)

        payload = {"embeds": [embed]}
        if mention:
            payload["content"] = mention
        return payload

    # plain style, which is just:
    #   [Open in Spawn](url) - savi finished building in MONSTER O'CLOCK
    label = cfg.get("link_label", DEFAULT_LINK_LABEL)
    content = f"[{label}]({link}) - {text}" if link else text
    if mention:
        content = f"{mention} {content}"
    # SUPPRESS_EMBEDS, or a bare URL in the text sprouts a preview card
    return {"content": content, "flags": 4}


def wanted(row: dict, spawn_cfg: dict) -> bool:
    """Check a row against only_types / ignore_types, if either is set."""
    type_field = spawn_cfg.get("fields", {}).get("type") or DEFAULT_TYPE_FIELD
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

    # server sends newest first, so flip it and Discord reads top to bottom
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

    # don't let this grow forever. 500 is ten pages worth of history
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
    ap.add_argument("--preview", action="store_true",
                    help="send your most recent notification to Discord to check "
                         "formatting; does not affect what gets sent later")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config()
    warn_if_tracked()

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

    if args.preview:
        rows = client.fetch_notifications()
        if not rows:
            log.info("No notifications to preview.")
            return
        post_discord(cfg["discord_webhook_url"],
                     build_payload(rows[0], cfg["spawn"].get("fields", {}), cfg))
        log.info("Sent your most recent notification as a preview. "
                 "state.json untouched, so this won't affect real notifications.")
        return

    if args.types:
        rows = client.fetch_notifications()
        field = cfg["spawn"].get("fields", {}).get("type") or DEFAULT_TYPE_FIELD
        counts: dict = {}
        for row in rows:
            counts[str(dig(row, field, "(missing)"))] = \
                counts.get(str(dig(row, field, "(missing)")), 0) + 1
        for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{n:4d}  {kind}")
        log.info("Counted by '%s'. Put the ones you want in spawn.only_types.", field)
        return

    interval = int(cfg.get("poll_seconds", 60))

    # on a first run just note what's already sat there, or you get your
    # entire notification history dumped into the channel in one go
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

        # bit of jitter so we're not hitting them on a perfect metronome
        delay = interval * (2 ** backoff) if backoff else interval
        time.sleep(min(delay, 900) + random.uniform(0, 3))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
