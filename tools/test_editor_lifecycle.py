#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Editor start/stop is not Windows-only: alive/stop use os.kill off Windows."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import argos_editor  # noqa: E402


class EditorLifecycleTest(unittest.TestCase):
    def test_default_godot_is_cross_platform(self):
        binary = argos_editor.default_godot_executable()
        if os.name == "nt":
            self.assertTrue(binary.lower().endswith(".exe"))
        else:
            self.assertEqual(binary, "godot")

    def test_dead_pid_is_not_alive(self):
        self.assertFalse(argos_editor._alive(0))
        self.assertFalse(argos_editor._alive(-1))
        self.assertFalse(argos_editor._alive(True))  # bool must not count as 1
        self.assertFalse(argos_editor._alive(2 ** 30))

    def test_stop_recorded_pid(self):
        proj = pathlib.Path(tempfile.mkdtemp(prefix="argos_editor_"))
        base = proj / ".argos"
        base.mkdir()
        kwargs = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **kwargs,
        )
        (base / "editor.pid").write_text(str(proc.pid), encoding="utf-8")
        try:
            self.assertTrue(argos_editor._alive(proc.pid))
            rc = argos_editor.cmd_stop(type("A", (), {"project": str(proj)})())
            self.assertEqual(rc, 0)
            proc.wait(timeout=5)
            self.assertFalse(argos_editor._alive(proc.pid))
            self.assertFalse((base / "editor.pid").exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
