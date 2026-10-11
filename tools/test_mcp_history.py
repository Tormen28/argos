#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mcp_history.py — Tests del puente MCP -> action history (BLOQUE B3).

Cubre:
  (1) micro-fix: las lecturas de `argos_history` NO se auto-registran (sin ruido).
  (2) una operacion normal se registra con action/tool/undo_action correctos.
  (3) `action.batch` (via `argos_raw`) deja `undo_action` Y `undo_group` NO nulos
      (integracion con B2 §2.2).
  (4) el codigo de error se extrae del texto ARGOS.
  (5) `_sanitize_group` sanea nombre de batch (§1.4): espacios/control/`"`/cap 80.

Ejecutar: python tools/test_mcp_history.py
"""
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import argos_history  # noqa: E402
import argos_mcp  # noqa: E402


def _text(data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


class McpHistoryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argos_mcphist_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entries(self):
        res = argos_history.read(self.tmp)
        self.assertTrue(res["ok"], res)
        return res["entries"]

    def _hist_file(self):
        return pathlib.Path(self.tmp) / ".argos" / "history.json"

    # (1) micro-fix: argos_history no se auto-registra
    def test_lectura_history_no_se_auto_registra(self):
        out = _text({"ok": True, "count": 0, "entries": []})
        argos_mcp._record_history(self.tmp, "argos_history", {"limit": 5}, out, 3.0)
        self.assertFalse(self._hist_file().exists(),
                         "una lectura de argos_history no debe crear history.json")

    # (2) operacion normal
    def test_operacion_normal_se_registra(self):
        out = _text({"created": "X", "undo_action": "ARGOS: crear X"})
        argos_mcp._record_history(self.tmp, "argos_node_create",
                                  {"class": "Sprite2D"}, out, 12.0)
        e = self._entries()[0]
        self.assertEqual(e["tool"], "argos_node_create")
        self.assertEqual(e["action"], "node.create")
        self.assertEqual(e["undo_action"], "ARGOS: crear X")
        self.assertIsNone(e["undo_group"])
        self.assertEqual(e["duration_ms"], 12)

    # (3) action.batch -> undo_action + undo_group no nulos (integracion B2)
    def test_batch_deja_undo_action_y_undo_group(self):
        out = _text({"undo_action": "ARGOS: construir enemigo", "ops": [],
                     "stopped_at": None})
        args = {"action": "action.batch",
                "payload": {"name": "construir enemigo", "on_error": "abort", "ops": []}}
        argos_mcp._record_history(self.tmp, "argos_raw", args, out, 20.0)
        e = self._entries()[0]
        self.assertEqual(e["action"], "action.batch")
        self.assertEqual(e["undo_group"], "construir enemigo")
        self.assertEqual(e["undo_action"], "ARGOS: construir enemigo")

    # (4) codigo de error
    def test_error_extrae_codigo(self):
        out = {"isError": True, "content": [{"type": "text", "text": "ARGOS: no_parent x"}]}
        argos_mcp._record_history(self.tmp, "argos_node_create", {}, out, 5.0)
        e = self._entries()[0]
        self.assertEqual(e["status"], "error")
        self.assertEqual(e["error"], "no_parent")

    # (5) saneado de nombre de batch (§1.4)
    def test_sanitize_group(self):
        self.assertEqual(argos_mcp._sanitize_group("  a\nb  "), "a b")
        self.assertEqual(argos_mcp._sanitize_group('x"y'), "xy")
        self.assertEqual(argos_mcp._sanitize_group(""), "operacion")
        self.assertEqual(argos_mcp._sanitize_group("z" * 100), "z" * 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
