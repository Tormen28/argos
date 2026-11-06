#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ARGOS 1.2 Leap: batch tool, animation tools, capture sessions, allowlist errors.

Godot is not required. Bridge calls are mocked; path and batch gates run for real.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import argos_mcp as m  # noqa: E402


def _text(out) -> str:
    return out["content"][0]["text"]


def _data(out) -> dict:
    return json.loads(_text(out))


def _refusal(out) -> dict:
    raw = _text(out)
    marker = "project_not_allowed"
    self_idx = raw.find(marker)
    if self_idx < 0:
        raise AssertionError(raw)
    return json.loads(raw[self_idx + len(marker):].strip())


def _same(a, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


class LeapSurfaceTest(unittest.TestCase):
    NEW_READS = frozenset({"argos_anim_list", "argos_anim_get"})
    NEW_WRITERS = frozenset({
        "argos_action_batch",
        "argos_anim_play",
        "argos_anim_stop",
        "argos_capture_editor_2d",
    })

    def test_version_is_1_2_0_everywhere(self):
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], "1.2.0")
        trust = plugin["extensions"]["com.nousresearch.hermes"]["servers"]["argos"]["trust"]
        self.assertEqual(trust, "untrusted")
        cfg = (ROOT / "argos" / "addons" / "argos" / "plugin.cfg").read_text(encoding="utf-8")
        self.assertIn('version="1.2.0"', cfg)
        mcp = (HERE / "argos_mcp.py").read_text(encoding="utf-8")
        self.assertIn('"version": "1.2.0"', mcp)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("1.2.0", readme)
        self.assertIn("argos_action_batch", readme)
        self.assertIn("argos_capture_editor_2d", readme)

    def test_new_tools_are_listed_and_hints_match_purity(self):
        names = {t["name"] for t in m.TOOLS}
        self.assertTrue(self.NEW_READS | self.NEW_WRITERS <= names)
        self.assertEqual(len(m.TOOLS), 33)
        selftest = (HERE / "argos_mcp_selftest.py").read_text(encoding="utf-8")
        self.assertIn("EXPECTED_TOOLS = 33", selftest)
        by_name = {t["name"]: t for t in m.TOOLS}
        for name in self.NEW_READS:
            self.assertIn(name, m._READ_ONLY_TOOLS)
            self.assertIs(by_name[name]["annotations"]["readOnlyHint"], True)
            first = by_name[name]["description"].split(" / ", 1)[0]
            self.assertRegex(first, r"^[A-Z]")
        for name in self.NEW_WRITERS:
            self.assertNotIn(name, m._READ_ONLY_TOOLS)
            self.assertNotIn("readOnlyHint", by_name[name].get("annotations") or {})
            first = by_name[name]["description"].split(" / ", 1)[0]
            self.assertRegex(first, r"^[A-Z]")
        for name in ("argos_capture_scene", "argos_runtime_capture", "argos_capture_editor_2d"):
            self.assertIn("session", by_name[name]["inputSchema"]["properties"])

    def test_addon_exposes_animation_and_capture_sessions(self):
        plugin = (ROOT / "argos" / "addons" / "argos" / "argos_plugin.gd").read_text(encoding="utf-8")
        codec = (ROOT / "argos" / "addons" / "argos" / "argos_codec.gd").read_text(encoding="utf-8")
        runtime = (ROOT / "argos" / "addons" / "argos" / "argos_runtime.gd").read_text(encoding="utf-8")
        for action in ("anim.list", "anim.get", "anim.play", "anim.stop"):
            self.assertIn('"%s"' % action, plugin)
        self.assertIn("func check_capture_request", codec)
        self.assertIn("check_capture_request", plugin)
        self.assertIn("check_capture_request", runtime)
        self.assertNotIn("ffmpeg", plugin.lower())
        self.assertIn("127.0.0.1", plugin)

    def test_skill_teaches_the_safe_workflow(self):
        text = (ROOT / "skills" / "argos" / "SKILL.md").read_text(encoding="utf-8")
        for needle in (
            "ARGOS_ALLOWED_PROJECTS",
            "project_not_allowed",
            "argos_action_batch",
            "argos_anim_list",
            "argos_anim_play",
            "argos_anim_stop",
            "argos_capture_editor_2d",
            "frame_NNN.png",
            "no_scene",
            "trust",
            "argos_raw",
            "only projects you trust",
        ):
            self.assertIn(needle, text, needle)
        self.assertIn("do not", text.lower())


class ActionBatchToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="argos_batch_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_allowlist_matches_the_addon(self):
        text = (ROOT / "argos" / "addons" / "argos" / "argos_plugin.gd").read_text(encoding="utf-8")
        self.assertIn("const MAX_BATCH_OPS := 64", text)
        for op in m.BATCH_ALLOWED:
            self.assertIn('"%s"' % op, text)
        self.assertEqual(m.MAX_BATCH_OPS, 64)
        self.assertEqual(m.BATCH_ALLOWED, (
            "node.create", "node.instantiate", "node.get_prop",
            "node.set_prop", "node.delete", "node.reparent",
        ))

    def test_happy_path_is_one_named_undo_group(self):
        sent = {}

        def fake_ws(project, payload, **kw):
            sent["payload"] = payload
            sent["project"] = project
            return {"ok": True, "result": {
                "undo_action": "ARGOS: build hero",
                "ops": [{"action": "node.create", "ok": True}],
                "stopped_at": None,
            }}

        with mock.patch.object(m, "ws_call", side_effect=fake_ws):
            out = m.call_tool("argos_action_batch", {
                "project": str(self.tmp),
                "name": "build hero",
                "on_error": "abort",
                "ops": [{"action": "node.create", "parent": ".", "class": "Node2D", "name": "Hero"}],
            })
        self.assertFalse(out.get("isError"), _text(out))
        self.assertEqual(sent["payload"]["action"], "action.batch")
        self.assertEqual(sent["payload"]["name"], "build hero")
        self.assertEqual(sent["payload"]["on_error"], "abort")
        self.assertEqual(sent["payload"]["ops"][0]["action"], "node.create")
        self.assertEqual(_data(out)["undo_action"], "ARGOS: build hero")
        hist = json.loads((self.tmp / ".argos" / "history.json").read_text(encoding="utf-8"))
        entry = hist["entries"][-1]
        self.assertEqual(entry["tool"], "argos_action_batch")
        self.assertEqual(entry["action"], "action.batch")
        self.assertEqual(entry["undo_group"], "build hero")

    def test_too_many_ops_rejected_before_the_editor(self):
        with mock.patch.object(m, "ws_call") as ws:
            out = m.call_tool("argos_action_batch", {
                "project": str(self.tmp),
                "ops": [{"action": "node.create", "class": "Node2D", "name": "N%d" % i} for i in range(65)],
            })
        ws.assert_not_called()
        self.assertTrue(out.get("isError"))
        self.assertIn("too_many_ops", _text(out))

    def test_nested_batch_rejected(self):
        with mock.patch.object(m, "ws_call") as ws:
            out = m.call_tool("argos_action_batch", {
                "project": str(self.tmp),
                "ops": [{"action": "action.batch", "ops": []}],
            })
        ws.assert_not_called()
        self.assertIn("busy_batch", _text(out))
        undo = m.call_tool("argos_action_batch", {
            "project": str(self.tmp),
            "ops": [{"action": "undo.undo"}],
        })
        self.assertIn("busy_batch", _text(undo))

    def test_disallowed_op_is_not_batchable(self):
        with mock.patch.object(m, "ws_call") as ws:
            out = m.call_tool("argos_action_batch", {
                "project": str(self.tmp),
                "on_error": "abort",
                "ops": [{"action": "scene.new", "path": "res://nope.tscn"}],
            })
        ws.assert_not_called()
        self.assertIn("not_batchable", _text(out))

    def test_continue_still_reaches_the_editor(self):
        sent = {}

        def fake_ws(project, payload, **kw):
            sent["payload"] = payload
            return {"ok": True, "result": {"undo_action": "ARGOS: partial", "ops": [], "stopped_at": 0}}

        with mock.patch.object(m, "ws_call", side_effect=fake_ws):
            out = m.call_tool("argos_action_batch", {
                "project": str(self.tmp),
                "on_error": "continue",
                "ops": [
                    {"action": "node.create", "class": "Node2D", "name": "Ok"},
                    {"action": "scene.new", "path": "res://nope.tscn"},
                ],
            })
        self.assertFalse(out.get("isError"), _text(out))
        self.assertEqual(sent["payload"]["on_error"], "continue")
        self.assertEqual(len(sent["payload"]["ops"]), 2)


class AnimationToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="argos_anim_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _fake(self, result=None, error=None, detail=""):
        sent = {}

        def fake_ws(project, payload, **kw):
            sent["payload"] = payload
            if error:
                return {"ok": False, "error": error, "detail": detail}
            return {"ok": True, "result": result or {}}

        return sent, fake_ws

    def test_list_and_get_forward_reads(self):
        sent, fake = self._fake({"nodes": [{"path": "Hero", "class": "AnimationPlayer"}]})
        with mock.patch.object(m, "ws_call", side_effect=fake):
            listed = m.call_tool("argos_anim_list", {"project": str(self.tmp), "path": "Hero"})
        self.assertFalse(listed.get("isError"), _text(listed))
        self.assertEqual(sent["payload"]["action"], "anim.list")
        self.assertEqual(sent["payload"]["path"], "Hero")
        sent, fake = self._fake({"class": "AnimationPlayer", "animations": []})
        with mock.patch.object(m, "ws_call", side_effect=fake):
            got = m.call_tool("argos_anim_get", {"project": str(self.tmp), "path": "Hero"})
        self.assertEqual(sent["payload"]["action"], "anim.get")
        self.assertEqual(_data(got)["class"], "AnimationPlayer")

    def test_play_and_stop_forward_writers(self):
        sent, fake = self._fake({"playing": "walk", "custom_speed": 0.5, "from_end": True})
        with mock.patch.object(m, "ws_call", side_effect=fake):
            played = m.call_tool("argos_anim_play", {
                "project": str(self.tmp),
                "path": "Hero",
                "name": "walk",
                "custom_speed": 0.5,
                "from_end": True,
            })
        self.assertFalse(played.get("isError"), _text(played))
        self.assertEqual(sent["payload"]["action"], "anim.play")
        self.assertEqual(sent["payload"]["name"], "walk")
        self.assertEqual(sent["payload"]["custom_speed"], 0.5)
        self.assertIs(sent["payload"]["from_end"], True)
        sent, fake = self._fake({"stopped": True, "seek_zero": True})
        with mock.patch.object(m, "ws_call", side_effect=fake):
            stopped = m.call_tool("argos_anim_stop", {
                "project": str(self.tmp), "path": "Hero", "seek_zero": False,
            })
        self.assertEqual(sent["payload"]["action"], "anim.stop")
        self.assertIs(sent["payload"]["seek_zero"], False)
        self.assertFalse(stopped.get("isError"))

    def test_missing_or_wrong_node_is_surfaced(self):
        sent, fake = self._fake(error="no_node", detail="Missing")
        with mock.patch.object(m, "ws_call", side_effect=fake):
            missing = m.call_tool("argos_anim_get", {"project": str(self.tmp), "path": "Missing"})
        self.assertIn("no_node", _text(missing))
        sent, fake = self._fake(error="bad_type", detail="Sprite2D")
        with mock.patch.object(m, "ws_call", side_effect=fake):
            wrong = m.call_tool("argos_anim_play", {
                "project": str(self.tmp), "path": "Sprite", "name": "walk",
            })
        self.assertIn("bad_type", _text(wrong))

    def test_play_rejects_a_non_numeric_speed_before_the_editor(self):
        with mock.patch.object(m, "ws_call") as ws:
            out = m.call_tool("argos_anim_play", {
                "project": str(self.tmp), "path": "Hero", "name": "walk", "custom_speed": "fast",
            })
        ws.assert_not_called()
        self.assertIn("bad_speed", _text(out))


class CaptureSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="argos_sess_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_editor_capture_uses_addon_action_and_path_gates(self):
        sent = {}

        def fake_ws(project, payload, **kw):
            sent["payload"] = payload
            return {"ok": True, "result": {"path": payload.get("path"), "w": 1, "h": 1}}

        with mock.patch.object(m, "ws_call", side_effect=fake_ws):
            out = m.call_tool("argos_capture_editor_2d", {
                "project": str(self.tmp),
                "out": ".argos/captures/view.png",
            })
        self.assertFalse(out.get("isError"), _text(out))
        self.assertEqual(sent["payload"]["action"], "capture.editor_2d")
        self.assertEqual(sent["payload"]["path"], ".argos/captures/view.png")
        with mock.patch.object(m, "ws_call") as ws:
            refused = m.call_tool("argos_capture_editor_2d", {
                "project": str(self.tmp), "out": "/tmp/escape.png",
            })
        ws.assert_not_called()
        self.assertIn("bad_out", _text(refused))
        self.assertFalse(pathlib.Path("/tmp/escape.png").exists())

    def test_session_rewrites_under_captures_and_rejects_bad_names(self):
        sent = {}

        def fake_ws(project, payload, **kw):
            sent["payload"] = payload
            return {"ok": True, "result": {"path": payload.get("path")}}

        with mock.patch.object(m, "ws_call", side_effect=fake_ws):
            out = m.call_tool("argos_capture_editor_2d", {
                "project": str(self.tmp), "session": "qa_run-1", "out": "shot.png",
            })
        self.assertFalse(out.get("isError"), _text(out))
        self.assertEqual(sent["payload"]["path"], ".argos/captures/qa_run-1/shot.png")
        for bad in ("../x", "a/b", "has space", "x" * 65, ".", "..", "café"):
            err = m._normalize_capture_request("shot.png", bad, str(self.tmp))
            self.assertEqual(err[0], "bad_session", bad)
        mixed = m._normalize_capture_request("res://shots/a.png", "qa", str(self.tmp))
        self.assertEqual(mixed[0], "bad_session")
        escaped = m._normalize_capture_request(".argos/captures/../../x.png", "qa", str(self.tmp))
        self.assertEqual(escaped[0], "bad_out")

    def test_omitted_leaf_allocates_the_next_free_frame(self):
        session_dir = self.tmp / ".argos" / "captures" / "qa"
        session_dir.mkdir(parents=True)
        (session_dir / "frame_001.png").write_bytes(b"png")
        (session_dir / "frame_002.png").write_bytes(b"png")
        sent = {}

        def fake_ws(project, payload, **kw):
            sent["payload"] = payload
            return {"ok": True, "result": {"path": payload.get("out") or payload.get("path")}}

        with mock.patch.object(m, "ws_call", side_effect=fake_ws):
            out = m.call_tool("argos_capture_scene", {
                "project": str(self.tmp), "session": "qa", "w": 32, "h": 32,
            })
        self.assertFalse(out.get("isError"), _text(out))
        self.assertEqual(sent["payload"]["out"], ".argos/captures/qa/frame_003.png")
        self.assertNotEqual(sent["payload"]["out"], ".argos/captures/qa/frame_001.png")

    def test_explicit_name_never_overwrites(self):
        dest = self.tmp / ".argos" / "captures" / "qa"
        dest.mkdir(parents=True)
        (dest / "shot.png").write_bytes(b"png")
        with mock.patch.object(m, "ws_call") as ws:
            out = m.call_tool("argos_runtime_capture", {
                "project": str(self.tmp), "session": "qa", "out": "shot.png",
            })
        ws.assert_not_called()
        self.assertIn("file_exists", _text(out))
        self.assertEqual((dest / "shot.png").read_bytes(), b"png")


class AllowlistErrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="argos_allow12_"))
        self.allowed = self.tmp / "game"
        self.allowed.mkdir()
        self.other = self.tmp / "other"
        self.other.mkdir()
        (self.tmp / "unrelated").mkdir()
        self.env = mock.patch.dict(os.environ, {"ARGOS_ALLOWED_PROJECTS": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_refusal_is_structured_and_does_not_list_the_disk(self):
        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)):
            got, err = m._enforce_allowed_project(str(self.other))
        self.assertIsNone(got)
        body = _refusal(err)
        self.assertEqual(body["code"], "project_not_allowed")
        self.assertTrue(_same(body["project"], self.other))
        self.assertIsInstance(body["allowed"], list)
        self.assertTrue(any(_same(p, self.allowed.resolve()) for p in body["allowed"]))
        self.assertFalse(any(_same(p, self.other) for p in body["allowed"]))
        self.assertFalse(any("unrelated" in str(p) for p in body["allowed"]))
        self.assertIn("ARGOS_PROJECT", body["hint"])
        self.assertIn("ARGOS_ALLOWED_PROJECTS", body["hint"])
        self.assertIn(" / ", body["hint"])
        self.assertLess(body["hint"].find("ARGOS_PROJECT"), body["hint"].find(" / "))

    def test_empty_allowlist_names_only_the_env_vars(self):
        with mock.patch.object(m, "DEFAULT_PROJECT", ""):
            got, err = m._enforce_allowed_project(str(self.other))
            self.assertIsNone(got)
            body = _refusal(err)
            self.assertEqual(body["code"], "project_not_allowed")
            self.assertEqual(body["allowed"], "only ARGOS_PROJECT")
            self.assertNotIn(str(self.tmp), json.dumps(body["allowed"]))
            launched = m.call_tool("argos_editor_launch", {"project": str(self.other)})
        self.assertEqual(_refusal(launched)["code"], "project_not_allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
