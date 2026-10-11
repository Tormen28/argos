#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_history.py — Tests de tools/argos_history.py (BLOQUE B2, §2.5).

Casos:
  (1) append: formato de campos, id incremental, redaccion de token, params <=1 KiB.
  (2) rotacion FIFO por entries y por bytes (cap del documento).
  (3) escritura atomica: no deja *.tmp.
  (4) read: filtros limit/tool/status/since y tope de limit.
  (5) JSON corrupto -> read = history_corrupt sin excepcion; append reinicia.
  (6) fail-silent: OSError de escritura -> None, sin excepcion.

Ejecutar: python tools/test_history.py
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


class HistoryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argos_hist_")
        self._me = argos_history.HISTORY_MAX_ENTRIES
        self._mb = argos_history.HISTORY_MAX_BYTES

    def tearDown(self):
        argos_history.HISTORY_MAX_ENTRIES = self._me
        argos_history.HISTORY_MAX_BYTES = self._mb
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, **kw):
        base = {"tool": "argos_node_create", "action": "node.create",
                "status": "ok", "duration_ms": 5}
        base.update(kw)
        return base

    def _file(self):
        return pathlib.Path(self.tmp) / ".argos" / "history.json"

    # (1) formato
    def test_append_formato_de_campos(self):
        entry = argos_history.append(
            self.tmp, self._rec(params={"class": "Sprite2D", "token": "SECRETO"}))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], 1)
        self.assertEqual(entry["source"], "mcp")
        self.assertEqual(entry["action"], "node.create")
        self.assertEqual(entry["status"], "ok")
        self.assertIsNone(entry["error"])
        self.assertIsNone(entry["undo_action"])
        self.assertIsNone(entry["undo_group"])
        self.assertEqual(entry["duration_ms"], 5)
        self.assertIn("T", entry["ts"])
        self.assertTrue(entry["ts"].endswith("Z"))
        self.assertGreater(entry["ts_ms"], 0)
        # token nunca se guarda
        self.assertNotIn("token", entry["params"])
        self.assertNotIn("SECRETO", json.dumps(entry["params"]))

        data = json.loads(self._file().read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertGreater(data["updated_ms"], 0)
        self.assertEqual(len(data["entries"]), 1)

    def test_append_ids_incrementales(self):
        argos_history.append(self.tmp, self._rec())
        e2 = argos_history.append(self.tmp, self._rec())
        self.assertEqual(e2["id"], 2)

    def test_params_string_se_recorta_a_1kib(self):
        entry = argos_history.append(
            self.tmp, self._rec(params={"value": "a" * 5000}))
        v = entry["params"]["value"]
        self.assertTrue(v.endswith("<truncado>"))
        self.assertLessEqual(len(v), 1024 + len("...<truncado>"))

    # (2) rotacion
    def test_fifo_por_entradas(self):
        argos_history.HISTORY_MAX_ENTRIES = 3
        for i in range(5):
            argos_history.append(self.tmp, self._rec(tool="t%d" % i))
        res = argos_history.read(self.tmp, limit=200)
        self.assertEqual(res["count"], 3)
        self.assertEqual([e["tool"] for e in res["entries"]], ["t2", "t3", "t4"])
        self.assertEqual([e["id"] for e in res["entries"]], [3, 4, 5])

    def test_fifo_por_bytes(self):
        argos_history.HISTORY_MAX_BYTES = 3000
        for _ in range(20):
            argos_history.append(self.tmp, self._rec(error="x" * 500))
        raw = self._file().read_text(encoding="utf-8")
        self.assertLessEqual(len(raw.encode("utf-8")), 3000)
        entries = json.loads(raw)["entries"]
        self.assertGreater(len(entries), 0)
        self.assertLess(len(entries), 20)
        # FIFO: se conserva la cola (ids mas recientes)
        self.assertEqual(entries[-1]["id"], 20)

    # (3) atomicidad
    def test_escritura_atomica_sin_tmp(self):
        argos_history.append(self.tmp, self._rec())
        leftover = list(pathlib.Path(self.tmp).rglob("*.tmp"))
        self.assertEqual(leftover, [])
        self.assertTrue(self._file().exists())

    # (4) filtros de read
    def test_read_filtros(self):
        argos_history.append(self.tmp, self._rec(tool="a", status="ok"))
        argos_history.append(self.tmp, self._rec(tool="b", status="error", error="no_node"))
        argos_history.append(self.tmp, self._rec(tool="a", status="error", error="rate_limited"))
        self.assertEqual(argos_history.read(self.tmp, tool="a")["count"], 2)
        self.assertEqual(argos_history.read(self.tmp, status="error")["count"], 2)
        self.assertEqual(argos_history.read(self.tmp, tool="a", status="error")["count"], 1)
        self.assertEqual(argos_history.read(self.tmp, limit=1)["count"], 1)

    def test_read_since(self):
        argos_history.append(self.tmp, self._rec())
        argos_history.append(self.tmp, self._rec())
        self.assertEqual(
            argos_history.read(self.tmp, since="2000-01-01T00:00:00Z")["count"], 2)
        self.assertEqual(
            argos_history.read(self.tmp, since="2999-01-01T00:00:00Z")["count"], 0)

    def test_read_limit_tope_200(self):
        for _ in range(5):
            argos_history.append(self.tmp, self._rec())
        self.assertEqual(argos_history.read(self.tmp, limit=999)["count"], 5)

    def test_read_sin_archivo(self):
        res = argos_history.read(self.tmp)
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 0)

    # (5) corrupto
    def test_read_corrupto_y_append_reinicia(self):
        p = self._file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{esto no es json", encoding="utf-8")
        res = argos_history.read(self.tmp)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "history_corrupt")

        entry = argos_history.append(self.tmp, self._rec(tool="post"))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], 1)
        res2 = argos_history.read(self.tmp)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["count"], 1)

    # (6) fail-silent
    def test_fail_silent_oserror(self):
        f = pathlib.Path(self.tmp) / "soy_archivo"
        f.write_text("x", encoding="utf-8")
        self.assertIsNone(argos_history.append(str(f), self._rec()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
