#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Security fixes requested on the Hermes catalog review of e87a02a6.

- plugin.json marks the argos MCP server untrusted
- readOnlyHint only on genuine reads
- tool descriptions are English-first
- launch / check / play stay inside ARGOS_PROJECT or ARGOS_ALLOWED_PROJECTS
- PNG captures stay under project/.argos/captures or res://
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


def _same(a, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


class PluginManifestTest(unittest.TestCase):
    def test_server_trust_untrusted(self):
        data = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        trust = data["extensions"]["com.nousresearch.hermes"]["servers"]["argos"]["trust"]
        self.assertEqual(trust, "untrusted")

    def test_session_token_uses_csprng(self):
        text = (ROOT / "argos" / "addons" / "argos" / "argos_plugin.gd").read_text(encoding="utf-8")
        self.assertNotIn("randi()", text)
        self.assertIn("generate_random_bytes", text)


class ReadOnlyHintTest(unittest.TestCase):
    def test_hint_only_on_genuine_reads(self):
        self.assertEqual(m._READ_ONLY_TOOLS, frozenset({
            "argos_ping", "argos_status", "argos_scene_tree",
            "argos_node_get_prop", "argos_runtime_info", "argos_history",
            "argos_anim_list", "argos_anim_get",
        }))
        seen = set()
        for tool in m.TOOLS:
            hint = bool((tool.get("annotations") or {}).get("readOnlyHint"))
            if tool["name"] in m._READ_ONLY_TOOLS:
                self.assertIs(tool["annotations"]["readOnlyHint"], True, tool["name"])
                seen.add(tool["name"])
            else:
                self.assertFalse(hint, tool["name"])
        self.assertEqual(seen, set(m._READ_ONLY_TOOLS))

    def test_apply_readonly_strips_hint_from_mutators(self):
        tools = [{
            "name": "argos_node_delete",
            "annotations": {"readOnlyHint": True, "title": "keep"},
        }]
        m._apply_readonly(tools)
        self.assertNotIn("readOnlyHint", tools[0]["annotations"])
        self.assertEqual(tools[0]["annotations"]["title"], "keep")


class EnglishDescriptionTest(unittest.TestCase):
    def test_tool_descriptions_are_english_first(self):
        markers = set("áéíóúñÁÉÍÓÚÑ¿¡")
        self.assertGreaterEqual(len(m.TOOLS), 27)
        for tool in m.TOOLS:
            first = tool["description"].split(" / ", 1)[0]
            self.assertRegex(first, r"^[A-Z]", tool["name"])
            self.assertFalse(markers.intersection(first), tool["name"] + ": " + first)


class ProjectAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="argos_allow_"))
        self.allowed = self.tmp / "game"
        self.allowed.mkdir()
        self.other = self.tmp / "other"
        self.other.mkdir()
        self.env = mock.patch.dict(os.environ, {"ARGOS_ALLOWED_PROJECTS": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _allow(self, extra=""):
        env = {"ARGOS_ALLOWED_PROJECTS": extra}
        return mock.patch.dict(os.environ, env)

    def test_refuses_when_allowlist_empty(self):
        with mock.patch.object(m, "DEFAULT_PROJECT", ""):
            got, err = m._enforce_allowed_project(str(self.allowed))
        self.assertIsNone(got)
        self.assertIn("project_not_allowed", _text(err))

    def test_refuses_project_outside_allowlist(self):
        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)):
            got, err = m._enforce_allowed_project(str(self.other))
        self.assertIsNone(got)
        self.assertIn("project_not_allowed", _text(err))
        self.assertFalse((self.other / ".argos").exists())

    def test_allows_configured_project_and_relative_spelling(self):
        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)):
            got, err = m._enforce_allowed_project(str(self.allowed))
            self.assertIsNone(err)
            self.assertTrue(_same(got, self.allowed.resolve()))
            rel = os.path.relpath(self.allowed, os.getcwd())
            got2, err2 = m._enforce_allowed_project(rel)
            self.assertIsNone(err2)
            self.assertTrue(_same(got2, self.allowed.resolve()))
            omitted, err3 = m._enforce_allowed_project("")
            self.assertIsNone(err3)
            self.assertTrue(_same(omitted, self.allowed.resolve()))

    def test_sibling_prefix_subdir_and_dotdot_are_outside(self):
        sibling = self.tmp / "game_extra"
        sibling.mkdir()
        nested = self.allowed / "sub"
        nested.mkdir()
        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)):
            for raw in (sibling, nested, self.allowed / ".." / "other"):
                got, err = m._enforce_allowed_project(str(raw))
                self.assertIsNone(got, raw)
                self.assertIn("project_not_allowed", _text(err))

    def test_extra_allowlist_entry_and_ambiguous_default(self):
        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)), \
             self._allow(str(self.other)):
            got, err = m._enforce_allowed_project(str(self.other))
            self.assertIsNone(err)
            self.assertTrue(_same(got, self.other.resolve()))
            missing, err2 = m._enforce_allowed_project("")
            self.assertIsNone(err2)
            self.assertTrue(_same(missing, self.allowed.resolve()))
        with mock.patch.object(m, "DEFAULT_PROJECT", ""), self._allow(
                os.pathsep.join((str(self.allowed), str(self.other)))):
            got, err = m._enforce_allowed_project("")
            self.assertIsNone(got)
            self.assertIn("project_not_allowed", _text(err))
            only, err3 = m._enforce_allowed_project("")
        with mock.patch.object(m, "DEFAULT_PROJECT", ""), self._allow(str(self.other)):
            only, err3 = m._enforce_allowed_project(None)
            self.assertIsNone(err3)
            self.assertTrue(_same(only, self.other.resolve()))

    def test_launch_check_play_refuse_without_spawning(self):
        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)), \
             mock.patch.object(m.subprocess, "run") as run, \
             mock.patch.object(m, "ws_call") as ws, \
             mock.patch.object(m.argos_check, "check_one") as check_one:
            for name in ("argos_editor_launch", "argos_check", "argos_run_play"):
                out = m.call_tool(name, {"project": str(self.other), "paths": ["res://a.gd"]})
                self.assertTrue(out.get("isError"), name)
                self.assertIn("project_not_allowed", _text(out))
            run.assert_not_called()
            ws.assert_not_called()
            check_one.assert_not_called()
            self.assertFalse((self.other / ".argos").exists())

    def test_launch_check_play_use_allowlisted_project(self):
        sent = []

        def fake_ws(project, payload, **kw):
            sent.append((project, payload.get("action")))
            return {"ok": True, "result": {"playing": True}}

        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)), \
             mock.patch.object(m.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="ok", stderr="")) as run, \
             mock.patch.object(m, "ws_call", side_effect=fake_ws), \
             mock.patch.object(m.argos_check, "check_one", return_value={"ok": True, "errors": []}) as check_one:
            launched = m.call_tool("argos_editor_launch", {})
            self.assertFalse(launched.get("isError"), _text(launched))
            cmd = run.call_args[0][0]
            self.assertIn(str(self.allowed.resolve()), cmd)
            played = m.call_tool("argos_run_play", {"project": str(self.allowed)})
            self.assertFalse(played.get("isError"), _text(played))
            self.assertTrue(_same(sent[0][0], self.allowed.resolve()))
            self.assertIn(sent[0][1], ("run.play_main", "run.play_scene"))
            checked = m.call_tool("argos_check", {"paths": ["res://a.gd"]})
            self.assertFalse(checked.get("isError"), _text(checked))
            self.assertTrue(_same(check_one.call_args[0][0], self.allowed.resolve()))

    def test_raw_play_is_allowlisted_but_raw_read_is_not(self):
        def fake_ws(project, payload, **kw):
            return {"ok": True, "result": {"project": project, "action": payload.get("action")}}

        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)), \
             mock.patch.object(m, "ws_call", side_effect=fake_ws) as ws:
            refused = m.call_tool("argos_raw", {
                "project": str(self.other), "action": "run.play_main",
            })
            self.assertIn("project_not_allowed", _text(refused))
            ws.assert_not_called()
            read = m.call_tool("argos_raw", {
                "project": str(self.other), "action": "ping",
            })
            self.assertFalse(read.get("isError"), _text(read))
            played = m.call_tool("argos_raw", {
                "project": str(self.allowed), "action": "run.play_scene",
                "payload": {"path": "res://main.tscn"},
            })
            self.assertFalse(played.get("isError"), _text(played))

    def test_other_tools_still_accept_an_unlisted_project(self):
        def fake_ws(project, payload, **kw):
            return {"ok": True, "result": {"echo": project}}

        with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)), \
             mock.patch.object(m, "ws_call", side_effect=fake_ws):
            out = m.call_tool("argos_scene_tree", {"project": str(self.other)})
        self.assertFalse(out.get("isError"))
        self.assertIn(str(self.other), _text(out))

    def test_omitted_play_does_not_fall_back_to_cwd(self):
        cwd_proj = self.tmp / "cwdproj"
        cwd_proj.mkdir()
        (cwd_proj / "project.godot").write_text("", encoding="utf-8")
        sent = []

        def fake_ws(project, payload, **kw):
            sent.append(project)
            return {"ok": True, "result": {}}

        old = os.getcwd()
        os.chdir(cwd_proj)
        try:
            with mock.patch.object(m, "DEFAULT_PROJECT", str(self.allowed)), \
                 mock.patch.object(m, "ws_call", side_effect=fake_ws):
                out = m.call_tool("argos_run_play", {})
                refused = m.call_tool("argos_run_play", {"project": str(cwd_proj)})
        finally:
            os.chdir(old)
        self.assertFalse(out.get("isError"), _text(out))
        self.assertTrue(_same(sent[0], self.allowed.resolve()))
        self.assertIn("project_not_allowed", _text(refused))


class CapturePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="argos_cap_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _err(self, out, project=None):
        return m._validate_payload(
            "capture.scene", {"action": "capture.scene", "out": out},
            "" if project is None else str(project))

    def test_rejects_absolute_dotdot_and_outside(self):
        for out in ("C:/tmp/x.png", "/tmp/x.png", "res://../x.png",
                    ".argos/captures/../../x.png", "shots/x.png", "x.png", ""):
            err = self._err(out, self.tmp)
            self.assertIsNotNone(err, out)
            self.assertEqual(err[0], "bad_out", out)

    def test_allows_res_and_captures_and_refuses_overwrite(self):
        self.assertIsNone(self._err("res://shots/new.png", self.tmp))
        self.assertIsNone(self._err(".argos/captures/new.png", self.tmp))
        self.assertIsNone(self._err("./.argos/captures/nested/new.PNG", self.tmp))
        shot = self.tmp / "shots"
        shot.mkdir()
        (shot / "new.png").write_bytes(b"png")
        err = self._err("res://shots/new.png", self.tmp)
        self.assertEqual(err[0], "file_exists")
        cap = self.tmp / ".argos" / "captures"
        cap.mkdir(parents=True)
        (cap / "new.png").write_bytes(b"png")
        err = self._err(".argos/captures/new.png", self.tmp)
        self.assertEqual(err[0], "file_exists")
        self.assertIn("overwrite", err[1])

    def test_symlink_escape_is_rejected(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        link_parent = self.tmp / "proj" / ".argos"
        link_parent.mkdir(parents=True)
        try:
            (link_parent / "captures").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are not available")
        err = self._err(".argos/captures/a.png", self.tmp / "proj")
        self.assertEqual(err[0], "bad_out")

    def test_editor_and_runtime_capture_keys(self):
        err = m._validate_payload(
            "capture.editor_2d", {"path": "/tmp/a.png"}, str(self.tmp))
        self.assertEqual(err[0], "bad_out")
        err = m._validate_payload(
            "runtime.capture", {"out": "res://ok.png"}, str(self.tmp))
        self.assertIsNone(err)

    def test_call_tool_does_not_write_outside(self):
        out = m.call_tool("argos_capture_scene", {
            "project": str(self.tmp), "out": "/tmp/argos-escape.png", "w": 32, "h": 32,
        })
        self.assertTrue(out.get("isError"))
        self.assertIn("bad_out", _text(out))
        self.assertFalse(pathlib.Path("/tmp/argos-escape.png").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
