"""
Tests for the notification forwarding logic - stdlib unittest, no network.

Run:  python -m unittest -v
"""

import base64
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import savi_notify as sn


def cfg(**overrides):
    base = {
        "discord_webhook_url": "https://discord.example/webhook",
        "spawn": {
            "base_url": "https://kiln.example",
            "user_id": "u-1",
            "apikey": "anon-key",
            "access_token": "at-1",
            "fields": {},
        },
    }
    base["spawn"].update(overrides.pop("spawn", {}))
    base.update(overrides)
    return base


def note(nid, message="savi finished building in KESSLER FLATS", kind="savi_finished",
         created="2026-08-24T19:00:00Z", **extra):
    """Shaped like a real row from Spawn's notifications table.

    Note `type` is "redirect" on essentially every row - `kind` is the field
    that actually says what happened.
    """
    row = {"id": nid, "user_id": "u-1", "message": message,
           "kind": kind, "type": "redirect", "status": "read",
           "created_at": created,
           "action_data": "/app/6d2be786?panel=chat&notif_kind=savi_finished",
           "related_game_id": "6d2be786"}
    row.update(extra)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(sn, "STATE_PATH", Path(tmp.name) / "state.json")
        p.start()
        self.addCleanup(p.stop)

        self.posts = []
        p2 = mock.patch.object(sn, "post_discord",
                               side_effect=lambda url, payload: self.posts.append(payload))
        p2.start()
        self.addCleanup(p2.stop)

    def poll(self, rows, state, config=None, seed_only=False):
        config = config or cfg()
        client = sn.SpawnClient(config["spawn"], state)
        with mock.patch.object(client, "fetch_notifications", return_value=rows):
            return sn.check_once(client, config, state, seed_only=seed_only)

    def texts(self):
        """The human sentence from each post, whichever style was used."""
        out = []
        for p in self.posts:
            if "embeds" in p:
                out.append(p["embeds"][0]["description"])
            else:
                content = p["content"]
                # Plain style prefixes "[label](url) - "; keep just the sentence.
                out.append(content.split(") - ", 1)[1] if ") - " in content else content)
        return out


class ForwardingTests(Base):
    def test_seed_run_sends_nothing(self):
        state = {}
        self.poll([note("a"), note("b")], state, seed_only=True)
        self.assertEqual(self.posts, [])
        self.assertEqual(set(state["seen_ids"]), {"a", "b"})

    def test_new_notification_is_forwarded_once(self):
        state = {}
        self.poll([note("a")], state, seed_only=True)

        self.poll([note("b"), note("a")], state)
        self.assertEqual(len(self.posts), 1)

        # Same feed again - b is now old news.
        self.poll([note("b"), note("a")], state)
        self.assertEqual(len(self.posts), 1, "must not re-send an old notification")

    def test_delivered_oldest_first(self):
        # Server returns newest first; Discord should read chronologically.
        state = {"seen_ids": []}
        rows = [note("new", message="third"), note("mid", message="second"),
                note("old", message="first")]
        self.poll(rows, state)
        self.assertEqual(self.texts(), ["first", "second", "third"])

    def test_survives_restart(self):
        state = {}
        self.poll([note("a")], state, seed_only=True)
        self.poll([note("b"), note("a")], state)
        self.assertEqual(len(self.posts), 1)

        reloaded = sn.load_state()
        self.poll([note("b"), note("a")], reloaded)
        self.assertEqual(len(self.posts), 1, "restart must not re-send")

    def test_seen_list_is_bounded(self):
        state = {"seen_ids": [str(i) for i in range(600)]}
        self.poll([], state)
        self.assertLessEqual(len(state["seen_ids"]), 500)


class FilterTests(Base):
    def test_only_types_keeps_just_those(self):
        c = cfg(spawn={"only_types": ["savi_finished"]})
        state = {"seen_ids": []}
        self.poll([note("a", kind="savi_finished", message="savi finished"),
                   note("b", kind="new_follower", message="someone followed you")],
                  state, config=c)
        self.assertEqual(self.texts(), ["savi finished"])

    def test_ignore_types_drops_those(self):
        c = cfg(spawn={"ignore_types": ["new_follower"]})
        state = {"seen_ids": []}
        self.poll([note("a", kind="savi_finished", message="savi finished"),
                   note("b", kind="new_follower", message="someone followed you")],
                  state, config=c)
        self.assertEqual(self.texts(), ["savi finished"])

    def test_filtered_rows_are_still_marked_seen(self):
        """Otherwise they'd be re-evaluated forever."""
        c = cfg(spawn={"only_types": ["savi_finished"]})
        state = {"seen_ids": []}
        self.poll([note("b", kind="new_follower")], state, config=c)
        self.assertIn("b", state["seen_ids"])


class TextTests(unittest.TestCase):
    def test_configured_field_wins(self):
        row = {"title": "ignore me", "data": {"headline": "use me"}}
        self.assertEqual(sn.notification_text(row, {"text": "data.headline"}), "use me")

    def test_falls_back_through_common_names(self):
        self.assertEqual(sn.notification_text({"body": "hello"}, {}), "hello")
        self.assertEqual(sn.notification_text({"message": "hi"}, {}), "hi")

    def test_unknown_shape_shows_payload_rather_than_nothing(self):
        row = {"id": "x", "user_id": "u", "data": {"game": "KESSLER FLATS"}}
        out = sn.notification_text(row, {})
        self.assertIn("KESSLER FLATS", out)
        self.assertNotEqual(out.strip(), "")

class LinkTests(unittest.TestCase):
    def test_relative_action_data_becomes_absolute(self):
        self.assertEqual(
            sn.resolve_link(note("a"), {}, {}),
            "https://www.spawn.co/app/6d2be786?panel=chat&notif_kind=savi_finished")

    def test_web_base_url_is_configurable(self):
        link = sn.resolve_link(note("a"), {}, {"web_base_url": "https://staging.example/"})
        self.assertTrue(link.startswith("https://staging.example/app/"))

    def test_absolute_link_is_left_alone(self):
        row = note("a", action_data="https://elsewhere.example/x")
        self.assertEqual(sn.resolve_link(row, {}, {}), "https://elsewhere.example/x")

    def test_missing_link_is_empty(self):
        self.assertEqual(sn.resolve_link(note("a", action_data=""), {}, {}), "")


class PlainStyleTests(unittest.TestCase):
    """The default: one line, the way a person would type it."""

    def test_looks_like_a_typed_message(self):
        payload = sn.build_payload(note("a", message="savi finished"), {}, {})
        self.assertEqual(
            payload["content"],
            "[Open in Spawn](https://www.spawn.co/app/6d2be786"
            "?panel=chat&notif_kind=savi_finished) - savi finished")

    def test_no_embed_is_sent(self):
        payload = sn.build_payload(note("a"), {}, {})
        self.assertNotIn("embeds", payload)

    def test_link_previews_are_suppressed(self):
        self.assertEqual(sn.build_payload(note("a"), {}, {})["flags"], 4)

    def test_without_a_link_it_is_just_the_sentence(self):
        payload = sn.build_payload(note("a", message="savi finished", action_data=""), {}, {})
        self.assertEqual(payload["content"], "savi finished")

    def test_link_label_is_configurable(self):
        payload = sn.build_payload(note("a"), {}, {"link_label": "open"})
        self.assertTrue(payload["content"].startswith("[open](https://"))

    def test_mention_goes_in_front(self):
        payload = sn.build_payload(note("a"), {}, {"mention": "<@123>"})
        self.assertTrue(payload["content"].startswith("<@123> ["))


class EmbedStyleTests(unittest.TestCase):
    EMBED = {"style": "embed"}

    def test_embed_carries_link_and_footer(self):
        payload = sn.build_payload(note("a"), {}, self.EMBED)
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "Open in Spawn")
        self.assertTrue(embed["url"].startswith("https://www.spawn.co/app/"))
        self.assertEqual(embed["footer"]["text"], "savi_finished")

    def test_footer_shows_kind_not_type(self):
        payload = sn.build_payload(note("a"), {}, self.EMBED)
        self.assertEqual(payload["embeds"][0]["footer"]["text"], "savi_finished")

    def test_mention_is_added_when_configured(self):
        payload = sn.build_payload(note("a"), {}, dict(self.EMBED, mention="<@123>"))
        self.assertEqual(payload["content"], "<@123>")


class AuthTests(unittest.TestCase):
    def make_client(self, state=None):
        return sn.SpawnClient(cfg()["spawn"], state if state is not None else {})

    def test_401_triggers_refresh_then_retries(self):
        client = self.make_client()
        responses = [
            (401, {"msg": "JWT expired"}),
            (200, [note("a")]),
        ]
        with mock.patch.object(sn, "http_json", side_effect=responses) as http, \
             mock.patch.object(client, "refresh", return_value=True) as refresh:
            rows = client.fetch_notifications()
        refresh.assert_called_once()
        self.assertEqual(len(rows), 1)
        self.assertEqual(http.call_count, 2)

    def test_401_without_refresh_raises(self):
        client = self.make_client()
        with mock.patch.object(sn, "http_json", return_value=(401, {})), \
             mock.patch.object(client, "refresh", return_value=False):
            with self.assertRaises(sn.AuthExpired):
                client.fetch_notifications()

    def test_refresh_persists_rotated_token(self):
        state = {"refresh_token": "old"}
        client = self.make_client(state)
        new = {"access_token": "at-2", "refresh_token": "rt-2"}
        with mock.patch.object(sn, "http_json", return_value=(200, new)), \
             mock.patch.object(sn, "save_state") as save:
            self.assertTrue(client.refresh())
        self.assertEqual(client.access_token, "at-2")
        self.assertEqual(state["refresh_token"], "rt-2")
        save.assert_called_once()

    def test_state_refresh_token_beats_config(self):
        spawn = dict(cfg()["spawn"], refresh_token="from-config")
        client = sn.SpawnClient(spawn, {"refresh_token": "from-state"})
        self.assertEqual(client.refresh_token, "from-state")

    def test_500_is_transient(self):
        client = self.make_client()
        with mock.patch.object(sn, "http_json", return_value=(500, "boom")):
            with self.assertRaises(sn.TransientError):
                client.fetch_notifications()


class UrlTests(unittest.TestCase):
    def test_query_matches_what_the_web_app_sends(self):
        url = sn.SpawnClient(cfg()["spawn"], {})._notifications_url()
        self.assertTrue(url.startswith("https://kiln.example/rest/v1/notifications?"))
        for part in ("select=*", "user_id=eq.u-1", "status=neq.archived",
                     "order=created_at.desc,id.desc", "limit=50"):
            self.assertIn(part, url)

    def test_skip_archived_can_be_turned_off(self):
        spawn = dict(cfg()["spawn"], skip_archived=False)
        self.assertNotIn("neq.archived", sn.SpawnClient(spawn, {})._notifications_url())


class SessionCookieTests(unittest.TestCase):
    """Spawn stores the session in a chunked, base64-wrapped cookie."""

    SESSION = {"access_token": "at-abc", "refresh_token": "rt-xyz",
               "user": {"id": "u-1"}}

    def cookie(self):
        blob = base64.b64encode(json.dumps(self.SESSION).encode()).decode()
        return "base64-" + blob

    def test_single_chunk(self):
        self.assertEqual(sn.parse_session_cookie(self.cookie()), ("at-abc", "rt-xyz"))

    def test_two_chunks_pasted_together(self):
        whole = self.cookie()
        half = len(whole) // 2
        pasted = whole[:half] + "\n" + whole[half:]   # as copied from .0 and .1
        self.assertEqual(sn.parse_session_cookie(pasted), ("at-abc", "rt-xyz"))

    def test_url_encoded_value(self):
        encoded = urllib.parse.quote(self.cookie())
        self.assertEqual(sn.parse_session_cookie(encoded), ("at-abc", "rt-xyz"))

    def test_plain_json_cookie(self):
        self.assertEqual(sn.parse_session_cookie(json.dumps(self.SESSION)),
                         ("at-abc", "rt-xyz"))

    def test_list_wrapped_session(self):
        blob = base64.b64encode(json.dumps([self.SESSION]).encode()).decode()
        self.assertEqual(sn.parse_session_cookie("base64-" + blob), ("at-abc", "rt-xyz"))

    def test_empty_is_empty(self):
        self.assertEqual(sn.parse_session_cookie(""), ("", ""))

    def test_truncated_paste_explains_itself(self):
        """Pasting only chunk .0 is the obvious mistake - say so."""
        with self.assertRaises(ValueError) as ctx:
            sn.parse_session_cookie(self.cookie()[:40])
        self.assertIn("chunks", str(ctx.exception).lower())

    def test_session_without_tokens_is_rejected(self):
        blob = base64.b64encode(json.dumps({"user": {"id": "u-1"}}).encode()).decode()
        with self.assertRaises(ValueError):
            sn.parse_session_cookie("base64-" + blob)

    def test_client_reads_tokens_from_cookie(self):
        spawn = dict(cfg()["spawn"], access_token="", session_cookie=self.cookie())
        client = sn.SpawnClient(spawn, {})
        self.assertEqual(client.access_token, "at-abc")
        self.assertEqual(client.refresh_token, "rt-xyz")

    def test_rotated_state_token_still_wins_over_cookie(self):
        spawn = dict(cfg()["spawn"], session_cookie=self.cookie())
        client = sn.SpawnClient(spawn, {"refresh_token": "rt-newer"})
        self.assertEqual(client.refresh_token, "rt-newer")


class TrackedFileWarningTests(unittest.TestCase):
    """Committing config.json is the one mistake that really costs you."""

    def fake_git(self, stdout):
        return mock.patch.object(
            sn.subprocess, "run",
            return_value=mock.Mock(stdout=stdout, returncode=0))

    def test_warns_when_config_is_tracked(self):
        with mock.patch.object(sn.Path, "exists", return_value=True), \
             self.fake_git("config.json\n"), \
             self.assertLogs(sn.log, "WARNING") as logs:
            tracked = sn.warn_if_tracked()
        self.assertEqual(tracked, ["config.json"])
        joined = " ".join(logs.output)
        self.assertIn("git rm --cached", joined)
        self.assertIn("live login", joined)

    def test_catches_state_json_too(self):
        with mock.patch.object(sn.Path, "exists", return_value=True), \
             self.fake_git("config.json\nstate.json\n"), \
             self.assertLogs(sn.log, "WARNING"):
            tracked = sn.warn_if_tracked()
        self.assertEqual(tracked, ["config.json", "state.json"])

    def test_quiet_when_nothing_is_tracked(self):
        with mock.patch.object(sn.Path, "exists", return_value=True), \
             self.fake_git("\n"):
            self.assertEqual(sn.warn_if_tracked(), [])

    def test_skips_when_not_a_repo(self):
        with mock.patch.object(sn.Path, "exists", return_value=False):
            self.assertEqual(sn.warn_if_tracked(), [])

    def test_survives_git_being_missing(self):
        with mock.patch.object(sn.Path, "exists", return_value=True), \
             mock.patch.object(sn.subprocess, "run", side_effect=OSError("no git")):
            self.assertEqual(sn.warn_if_tracked(), [])

    def test_this_repo_is_actually_clean(self):
        """Not a mock. If someone commits the real config, this fails."""
        self.assertEqual(sn.warn_if_tracked(), [])


class DigTests(unittest.TestCase):
    def test_dotted_paths(self):
        blob = {"data": {"items": [{"id": 1}, {"id": 2}]}}
        self.assertEqual(sn.dig(blob, "data.items.1.id"), 2)
        self.assertEqual(sn.dig(blob, "data.nope", "fallback"), "fallback")
        self.assertEqual(sn.dig(blob, ""), blob)


if __name__ == "__main__":
    unittest.main()
