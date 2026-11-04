"""Tests: auto-open de escena cuando el editor responde no_scene."""
import os, sys, tempfile, pathlib, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argos_mcp as m


def _resp_err(code):
    return {"ok": False, "error": code, "detail": ""}


class NoSceneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = self.tmp.name
        p = mock.patch.object(m.time, "sleep", lambda *_: None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, body):
        pathlib.Path(self.proj, "project.godot").write_text(body, encoding="utf-8")

    def test_main_scene_parsed(self):
        self._write('config_version=5\n[application]\nconfig/name="x"\nrun/main_scene="res://main.tscn"\n')
        self.assertEqual(m._main_scene(self.proj), "res://main.tscn")

    def test_main_scene_uid_ignored(self):
        self._write('[application]\nrun/main_scene="uid://abc"\n')
        self.assertEqual(m._main_scene(self.proj), "")

    def test_main_scene_missing(self):
        self.assertEqual(m._main_scene(self.proj), "")

    def _run(self, calls, args):
        seq = iter(calls)
        sent = []
        def fake(project, payload, **kw):
            sent.append(payload)
            return next(seq)
        with mock.patch.object(m, "ws_call", side_effect=fake), \
             mock.patch.object(m, "_record_history", lambda *a, **k: None):
            out = m.call_tool("argos_node_create", dict(args, project=self.proj, **{"class": "Node2D"}))
        return out, sent

    def test_auto_opens_main_scene_and_retries(self):
        self._write('[application]\nrun/main_scene="res://main.tscn"\n')
        out, sent = self._run([_resp_err("no_scene"), {"ok": True, "result": {"opened": "res://main.tscn"}},
                               {"ok": True, "result": {"created": "N"}}], {})
        self.assertFalse(out.get("isError"))
        self.assertIn("auto_opened_scene", out["content"][0]["text"])
        self.assertEqual(sent[1], {"action": "scene.open", "path": "res://main.tscn"})

    def test_scene_path_arg_wins(self):
        self._write('[application]\nrun/main_scene="res://main.tscn"\n')
        out, sent = self._run([_resp_err("no_scene"), {"ok": True, "result": {}},
                               {"ok": True, "result": {"created": "N"}}], {"scene_path": "res://lvl.tscn"})
        self.assertEqual(sent[1]["path"], "res://lvl.tscn")

    def test_clear_error_without_any_scene(self):
        out, sent = self._run([_resp_err("no_scene")], {})
        self.assertTrue(out.get("isError"))
        self.assertIn("scene_path", out["content"][0]["text"])
        self.assertEqual(len(sent), 1)

    def test_other_errors_untouched(self):
        out, sent = self._run([_resp_err("no_parent")], {})
        self.assertIn("no_parent", out["content"][0]["text"])
        self.assertEqual(len(sent), 1)

    def test_proj_env_and_cwd(self):
        with mock.patch.object(m, "DEFAULT_PROJECT", "/x/env"):
            self.assertEqual(m._proj({}), "/x/env")
            self.assertEqual(m._proj({"project": "/y"}), "/y")
        self._write("")
        cwd = os.getcwd()
        try:
            os.chdir(self.proj)
            with mock.patch.object(m, "DEFAULT_PROJECT", ""):
                self.assertEqual(os.path.realpath(m._proj({})), os.path.realpath(self.proj))
        finally:
            os.chdir(cwd)

    def test_schema_has_scene_path(self):
        tools = {t["name"]: t for t in m.TOOLS}
        for n in m.SCENE_TOOLS:
            self.assertIn("scene_path", tools[n]["inputSchema"]["properties"], n)


if __name__ == "__main__":
    unittest.main()
