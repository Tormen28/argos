#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_limits.py — Verifica límites/timeouts/rate del servidor MCP (BLOQUE B1).

Casos (§3.1–§3.3 del diseño):
  (1) _validate_payload: acepta/rechaza en las fronteras EXACTAS de tamaño,
      paths, props y captura. Códigos exactos: payload_too_large, too_many_props,
      prop_too_long, props_too_large, bad_out, bad_size, bad_path.
  (2) _rate_check: token bucket por proyecto; mutantes limitadas, lecturas exentas;
      ARGOS_RATE_LIMIT_PER_MIN=0 => off.
  (3) _action_timeout: default 25 s; capture.*/runtime.capture 30 s;
      editor.quit 15 s; action.batch min(55, 10 + 2 x len(ops)).
  (4) _result: MAX_RESULT_BYTES => isError result_too_large (no reenvía).
  (5) call_tool: la 3ª mutación con limit=2 => rate_limited; las lecturas pasan.

Ejecutar (intérprete con 'websockets', como argos_mcp.py):
  python tools/test_limits.py
"""
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import argos_mcp  # noqa: E402  (requiere 'websockets')


class ConstantsTest(unittest.TestCase):
    """Los valores de la tabla §3.1 son los especificados."""

    def test_valores_exactos(self):
        self.assertEqual(argos_mcp.MAX_MSG_BYTES, 8 * 1024 * 1024)
        self.assertEqual(argos_mcp.MAX_PROPS, 128)
        self.assertEqual(argos_mcp.MAX_PROP_STR, 4096)
        self.assertEqual(argos_mcp.MAX_PROPS_BYTES, 262144)
        self.assertEqual(argos_mcp.CAPTURE_MIN, 16)
        self.assertEqual(argos_mcp.CAPTURE_MAX, 4096)
        self.assertEqual(argos_mcp.MAX_RESULT_BYTES, 4 * 1024 * 1024)


class ValidatePayloadTest(unittest.TestCase):

    def test_valido_devuelve_none(self):
        self.assertIsNone(argos_mcp._validate_payload("ping", {"action": "ping"}))
        self.assertIsNone(argos_mcp._validate_payload(
            "scene.get_tree", {"action": "scene.get_tree", "max_nodes": 2}))

    def test_payload_too_large_frontera(self):
        orig = argos_mcp.MAX_MSG_BYTES
        argos_mcp.MAX_MSG_BYTES = 100
        try:
            # justo por debajo del tope: válido
            self.assertIsNone(argos_mcp._validate_payload("ping", {"action": "ping", "x": "a" * 60}))
            # justo por encima: payload_too_large
            err = argos_mcp._validate_payload("ping", {"action": "ping", "x": "a" * 200})
            self.assertEqual(err[0], "payload_too_large")
        finally:
            argos_mcp.MAX_MSG_BYTES = orig

    def test_path_res_obligatorio(self):
        self.assertEqual(
            argos_mcp._validate_payload("scene.open", {"action": "scene.open", "path": "C:/x.tscn"})[0],
            "bad_path")
        self.assertEqual(
            argos_mcp._validate_payload("fs.update_file", {"action": "fs.update_file", "path": "x.gd"})[0],
            "bad_path")
        self.assertIsNone(argos_mcp._validate_payload(
            "scene.open", {"action": "scene.open", "path": "res://main.tscn"}))
        self.assertIsNone(argos_mcp._validate_payload(
            "fs.update_file", {"action": "fs.update_file", "path": "res://main.gd"}))

    def test_capture_out_png(self):
        base = {"action": "capture.scene", "out": "C:/tmp/x.txt"}
        self.assertEqual(argos_mcp._validate_payload("capture.scene", base)[0], "bad_out")
        self.assertEqual(argos_mcp._validate_payload(
            "capture.scene", {"action": "capture.scene", "out": ""})[0], "bad_out")
        self.assertIsNone(argos_mcp._validate_payload(
            "capture.scene", {"action": "capture.scene", "out": "C:/tmp/x.PNG"}))
        self.assertEqual(argos_mcp._validate_payload(
            "runtime.capture", {"action": "runtime.capture", "out": "x.jpg"})[0], "bad_out")

    def test_capture_size_frontera(self):
        def chk(w, h):
            return argos_mcp._validate_payload(
                "capture.scene", {"action": "capture.scene", "out": "x.png", "w": w, "h": h})
        self.assertEqual(chk(15, 648)[0], "bad_size")
        self.assertEqual(chk(4097, 648)[0], "bad_size")
        self.assertEqual(chk(1152, 15)[0], "bad_size")
        self.assertEqual(chk(1152, 4097)[0], "bad_size")
        self.assertIsNone(chk(16, 648))       # frontera inferior incluida
        self.assertIsNone(chk(4096, 648))     # frontera superior incluida
        self.assertIsNone(chk(1152, 16))
        self.assertIsNone(chk(1152, 4096))

    def test_props_count_frontera(self):
        ok = {("k%d" % i): 1 for i in range(argos_mcp.MAX_PROPS)}
        self.assertIsNone(argos_mcp._validate_payload(
            "node.create", {"action": "node.create", "props": ok}))
        over = dict(ok)
        over["extra"] = 1
        self.assertEqual(argos_mcp._validate_payload(
            "node.create", {"action": "node.create", "props": over})[0], "too_many_props")

    def test_prop_str_frontera(self):
        exact = {"text": "a" * argos_mcp.MAX_PROP_STR}
        self.assertIsNone(argos_mcp._validate_payload(
            "node.create", {"action": "node.create", "props": exact}))
        over = {"text": "a" * (argos_mcp.MAX_PROP_STR + 1)}
        err = argos_mcp._validate_payload("node.create", {"action": "node.create", "props": over})
        self.assertEqual(err[0], "prop_too_long")
        self.assertEqual(err[1], "text")

    def test_props_bytes_frontera(self):
        # ~200 KiB < 256 KiB: válido (<=128 claves, cada valor <=4096 chars)
        small = {("k%d" % i): "a" * 2000 for i in range(100)}
        self.assertIsNone(argos_mcp._validate_payload(
            "node.create", {"action": "node.create", "props": small}))
        # ~400 KiB > 256 KiB: props_too_large
        big = {("k%d" % i): "a" * 4000 for i in range(100)}
        self.assertEqual(argos_mcp._validate_payload(
            "node.create", {"action": "node.create", "props": big})[0], "props_too_large")


class RateCheckTest(unittest.TestCase):

    def setUp(self):
        self._orig = argos_mcp.RATE_LIMIT_PER_MIN
        argos_mcp.RATE_LIMIT_PER_MIN = 2
        argos_mcp._RATE_BUCKETS.clear()

    def tearDown(self):
        argos_mcp.RATE_LIMIT_PER_MIN = self._orig
        argos_mcp._RATE_BUCKETS.clear()

    def test_dos_pasan_tercera_retry(self):
        self.assertIsNone(argos_mcp._rate_check("p", "node.create"))
        self.assertIsNone(argos_mcp._rate_check("p", "node.delete"))
        wait = argos_mcp._rate_check("p", "scene.new")
        self.assertIsInstance(wait, int)
        self.assertGreater(wait, 0)

    def test_lecturas_exentas(self):
        for _ in range(5):
            self.assertIsNone(argos_mcp._rate_check("p", "scene.get_tree"))
            self.assertIsNone(argos_mcp._rate_check("p", "ping"))
        # el bucket de un proyecto no se consume con lecturas
        self.assertIsNone(argos_mcp._rate_check("p", "node.create"))

    def test_off_por_defecto(self):
        argos_mcp.RATE_LIMIT_PER_MIN = 0
        argos_mcp._RATE_BUCKETS.clear()
        for _ in range(50):
            self.assertIsNone(argos_mcp._rate_check("p", "node.create"))


class TimeoutTest(unittest.TestCase):

    def test_default_y_tabla(self):
        self.assertEqual(argos_mcp._action_timeout("ping", {}), 25.0)
        self.assertEqual(argos_mcp._action_timeout("node.create", {}), 25.0)
        self.assertEqual(argos_mcp._action_timeout("capture.scene", {}), 30.0)
        self.assertEqual(argos_mcp._action_timeout("capture.editor_2d", {}), 30.0)
        self.assertEqual(argos_mcp._action_timeout("runtime.capture", {}), 30.0)
        self.assertEqual(argos_mcp._action_timeout("editor.quit", {}), 15.0)

    def test_batch_escala_con_ops(self):
        self.assertEqual(argos_mcp._action_timeout("action.batch", {"ops": []}), 10.0)
        self.assertEqual(argos_mcp._action_timeout("action.batch", {"ops": [1, 2, 3]}), 16.0)
        # cap duro: 10 + 2*30 = 70 -> 55
        self.assertEqual(argos_mcp._action_timeout("action.batch", {"ops": [0] * 30}), 55.0)

    def test_is_mutating(self):
        for a in ("node.create", "node.delete", "scene.new", "action.batch"):
            self.assertTrue(argos_mcp._is_mutating(a), a)
        for a in ("ping", "status", "scene.get_tree", "capture.scene", "editor.quit"):
            self.assertFalse(argos_mcp._is_mutating(a), a)


class ResultTooLargeTest(unittest.TestCase):

    def test_limite_result(self):
        orig = argos_mcp.MAX_RESULT_BYTES
        argos_mcp.MAX_RESULT_BYTES = 100
        try:
            out = argos_mcp._result({"x": "a" * 500})
            self.assertTrue(out.get("isError"))
            self.assertIn("result_too_large", out["content"][0]["text"])
            ok = argos_mcp._result({"x": 1})
            self.assertFalse(ok.get("isError"))
        finally:
            argos_mcp.MAX_RESULT_BYTES = orig


class CallToolRateTest(unittest.TestCase):
    """Integración ligera: el rate guard vive en call_tool (ws_call falso)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argos_limits_")
        self._orig_rl = argos_mcp.RATE_LIMIT_PER_MIN
        self._orig_wscall = argos_mcp.ws_call
        argos_mcp.RATE_LIMIT_PER_MIN = 2
        argos_mcp._RATE_BUCKETS.clear()

        def fake_wscall(project, payload, **kw):
            return {"ok": True, "result": {"echo": payload.get("action")}}
        argos_mcp.ws_call = fake_wscall

    def tearDown(self):
        argos_mcp.ws_call = self._orig_wscall
        argos_mcp.RATE_LIMIT_PER_MIN = self._orig_rl
        argos_mcp._RATE_BUCKETS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _err_text(self, out):
        return out["content"][0]["text"]

    def test_tercera_mutacion_rate_limited(self):
        a = {"project": self.tmp, "class": "Label"}
        for _ in range(2):
            self.assertFalse(argos_mcp.call_tool("argos_node_create", a).get("isError"))
        third = argos_mcp.call_tool("argos_node_create", a)
        self.assertTrue(third.get("isError"))
        self.assertIn("rate_limited", self._err_text(third))
        self.assertIn("retry_after_ms", self._err_text(third))

    def test_lecturas_pasan_con_bucket_vacio(self):
        argos_mcp.RATE_LIMIT_PER_MIN = 1
        argos_mcp._RATE_BUCKETS.clear()
        self.assertFalse(argos_mcp.call_tool(
            "argos_node_create", {"project": self.tmp, "class": "Label"}).get("isError"))
        # lectura: exenta aunque el bucket esté vacío
        for _ in range(3):
            self.assertFalse(argos_mcp.call_tool(
                "argos_scene_tree", {"project": self.tmp}).get("isError"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
