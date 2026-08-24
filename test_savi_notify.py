"""
Tests for the notification state machine - stdlib unittest, no network.

Run:  python -m unittest -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import savi_notify as sn


CFG = {
    "discord_webhook_url": "https://discord.example/webhook",
    "spawn": {
        "tasks_url": "https://spawn.example/api/tasks",
        "fields": {"id": "id", "status": "status", "title": "name", "url": "link"},
    },
}


def task(tid, status, name="Build a spaceship"):
    return {"id": tid, "status": status, "name": name,
            "link": f"https://www.spawn.co/p/{tid}"}


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(sn, "STATE_PATH", Path(self.tmp.name) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.posts = []
        p2 = mock.patch.object(sn, "post_discord",
                               side_effect=lambda url, payload: self.posts.append(payload))
        p2.start()
        self.addCleanup(p2.stop)

    def run_poll(self, tasks, state, seed_only=False):
        with mock.patch.object(sn, "fetch_tasks", return_value=tasks):
            return sn.check_once(CFG, state, seed_only=seed_only)

    def test_seed_run_does_not_notify(self):
        state = {"seen": {}}
        n = self.run_poll([task("a", "done"), task("b", "running")], state, seed_only=True)
        self.assertEqual(n, 0)
        self.assertEqual(self.posts, [])
        self.assertEqual(state["seen"]["a"], "done")

    def test_transition_to_done_notifies_once(self):
        state = {"seen": {}}
        self.run_poll([task("a", "running")], state)
        self.assertEqual(len(self.posts), 0, "running task should not notify")

        self.run_poll([task("a", "done")], state)
        self.assertEqual(len(self.posts), 1, "finishing should notify")

        # Poll again with the task still sitting there finished.
        self.run_poll([task("a", "done")], state)
        self.assertEqual(len(self.posts), 1, "must not notify twice for the same task")

    def test_failed_task_gets_error_styling(self):
        state = {"seen": {"a": "running"}}
        self.run_poll([task("a", "failed")], state)
        self.assertEqual(len(self.posts), 1)
        embed = self.posts[0]["embeds"][0]
        self.assertEqual(embed["color"], 0xE74C3C)
        self.assertIn("problem", embed["title"].lower())

    def test_success_embed_carries_title_and_link(self):
        state = {"seen": {"a": "running"}}
        self.run_poll([task("a", "completed", name="Neon city")], state)
        embed = self.posts[0]["embeds"][0]
        self.assertIn("Neon city", embed["description"])
        self.assertEqual(embed["url"], "https://www.spawn.co/p/a")
        self.assertEqual(embed["color"], 0x2ECC71)

    def test_unknown_status_is_ignored(self):
        state = {"seen": {}}
        self.run_poll([task("a", "queued"), task("b", "generating")], state)
        self.assertEqual(self.posts, [])

    def test_state_survives_a_restart(self):
        state = {"seen": {}}
        self.run_poll([task("a", "running")], state)
        self.run_poll([task("a", "done")], state)
        self.assertEqual(len(self.posts), 1)

        reloaded = sn.load_state()          # simulate the process restarting
        self.run_poll([task("a", "done")], reloaded)
        self.assertEqual(len(self.posts), 1, "restart must not re-notify")

    def test_state_is_trimmed(self):
        state = {"seen": {str(i): "done" for i in range(600)}}
        self.run_poll([], state)
        self.assertLessEqual(len(state["seen"]), 500)


class DigTests(unittest.TestCase):
    def test_dotted_paths(self):
        blob = {"data": {"tasks": [{"id": 1}, {"id": 2}]}}
        self.assertEqual(sn.dig(blob, "data.tasks.1.id"), 2)
        self.assertEqual(sn.dig(blob, "data.nope", "fallback"), "fallback")
        self.assertEqual(sn.dig(blob, ""), blob)

    def test_missing_index_falls_back(self):
        self.assertEqual(sn.dig({"a": [1]}, "a.9", "x"), "x")


class FetchTests(unittest.TestCase):
    def test_401_raises_auth_expired(self):
        with mock.patch.object(sn, "http_json", return_value=(401, {"error": "nope"})):
            with self.assertRaises(sn.AuthExpired):
                sn.fetch_tasks({"tasks_url": "https://x.example"})

    def test_500_is_transient(self):
        with mock.patch.object(sn, "http_json", return_value=(500, "boom")):
            with self.assertRaises(sn.TransientError):
                sn.fetch_tasks({"tasks_url": "https://x.example"})

    def test_tasks_path_is_honoured(self):
        payload = {"result": {"items": [{"id": "z", "status": "done"}]}}
        with mock.patch.object(sn, "http_json", return_value=(200, payload)):
            got = sn.fetch_tasks({"tasks_url": "https://x.example",
                                  "tasks_path": "result.items"})
        self.assertEqual(got, [{"id": "z", "status": "done"}])

    def test_bad_shape_explains_itself(self):
        with mock.patch.object(sn, "http_json", return_value=(200, {"tasks": "not a list"})):
            with self.assertRaises(sn.TransientError) as ctx:
                sn.fetch_tasks({"tasks_url": "https://x.example", "tasks_path": "tasks"})
        self.assertIn("tasks_path", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
