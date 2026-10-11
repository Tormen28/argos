#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_heartbeat.py — Verifica el heartbeat del editor (BLOQUE A-2).

El plugin escribe <proyecto>/.argos/heartbeat (JSON {"ts": ms_unix, "pid": N})
cada <=30 s desde _process(); el servidor MCP lo lee para distinguir «editor
reimportando» (heartbeat fresco) de «editor muerto» (viejo/ausente).

Casos:
  (1) _heartbeat_age: None sin archivo o sin proyecto; edad correcta desde ts
      (ms unix); tolera JSON {"ts":..} y número plano; basura/ts<=0 -> None.
  (2) _classify_heartbeat (pura): <=90 s -> «editor ocupado, probablemente
      reimportando»; >90 s o ausente -> «editor no está corriendo».
  (3) argos_ping: campo aditivo heartbeat_age_s (null si no hay archivo) sin
      tocar los campos existentes (pong, ts).
  (4) call_tool: al agotar el retry (excepción transitoria propagada por
      ws_call) loguea ERROR clasificando por heartbeat; error lógico -> no.

Ejecutar (con un intérprete que tenga 'websockets', como argos_mcp.py):
  python tools/test_heartbeat.py
"""
import json
import logging
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import argos_mcp  # noqa: E402  (módulo bajo test; requiere 'websockets')

from test_retry_backoff import CaptureHandler, FakeConnect  # noqa: E402  (fakes del BLOQUE A-1)


def _write_hb(proj: pathlib.Path, age_s: float, pid: int = 4242):
    """Escribe un heartbeat como lo hace el plugin: JSON con ts en ms unix."""
    ts_ms = int((time.time() - age_s) * 1000.0)
    (proj / ".argos" / "heartbeat").write_text(
        json.dumps({"ts": ts_ms, "pid": pid}), encoding="utf-8")


class _ProjTest(unittest.TestCase):
    """Base: proyecto temporal con .argos/ y aislamiento del estado global."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argos_hb_")
        self.proj = pathlib.Path(self.tmp)
        (self.proj / ".argos").mkdir()
        self._orig_conn, self._orig_key = argos_mcp._CONN, argos_mcp._CONN_KEY
        self._orig_connect = argos_mcp.connect
        self._orig_wscall = argos_mcp.ws_call
        self._orig_delay = argos_mcp.RETRY_BASE_DELAY
        argos_mcp._CONN, argos_mcp._CONN_KEY = None, None
        self.handler = CaptureHandler()
        argos_mcp._log.addHandler(self.handler)
        self._orig_propagate = argos_mcp._log.propagate
        argos_mcp._log.propagate = False

    def tearDown(self):
        argos_mcp.connect = self._orig_connect
        argos_mcp.ws_call = self._orig_wscall
        argos_mcp.RETRY_BASE_DELAY = self._orig_delay
        argos_mcp._CONN, argos_mcp._CONN_KEY = self._orig_conn, self._orig_key
        argos_mcp._log.removeHandler(self.handler)
        argos_mcp._log.propagate = self._orig_propagate
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _errors(self):
        return [m for lvl, m in self.handler.records if lvl >= logging.ERROR]


# (1) — lectura del archivo de heartbeat

class HeartbeatAgeTest(_ProjTest):

    def test_none_si_no_hay_archivo(self):
        self.assertIsNone(argos_mcp._heartbeat_age(str(self.proj)))

    def test_none_si_no_hay_proyecto(self):
        self.assertIsNone(argos_mcp._heartbeat_age(""))

    def test_edad_desde_json_ts_ms(self):
        _write_hb(self.proj, age_s=10.0)
        age = argos_mcp._heartbeat_age(str(self.proj))
        self.assertIsNotNone(age)
        self.assertTrue(9.0 <= age <= 12.0, "edad fuera de rango: %r" % age)

    def test_tolerante_a_numero_plano(self):
        ts_ms = int((time.time() - 30.0) * 1000.0)
        (self.proj / ".argos" / "heartbeat").write_text(str(ts_ms), encoding="utf-8")
        age = argos_mcp._heartbeat_age(str(self.proj))
        self.assertIsNotNone(age)
        self.assertTrue(29.0 <= age <= 32.0, "edad fuera de rango: %r" % age)

    def test_basura_devuelve_none(self):
        (self.proj / ".argos" / "heartbeat").write_text("no soy json ni numero", encoding="utf-8")
        self.assertIsNone(argos_mcp._heartbeat_age(str(self.proj)))

    def test_ts_cero_o_negativo_devuelve_none(self):
        (self.proj / ".argos" / "heartbeat").write_text('{"ts": 0, "pid": 1}', encoding="utf-8")
        self.assertIsNone(argos_mcp._heartbeat_age(str(self.proj)))


# (2) — clasificación pura por edad

class HeartbeatClassifyTest(unittest.TestCase):

    def test_fresco_hasta_90s(self):
        for age in (0.0, 45.0, 90.0):  # 90 incluido: umbral <=90 s
            v = argos_mcp._classify_heartbeat(age)
            self.assertIn("ocupado", v, age)
            self.assertIn("reimportando", v, age)

    def test_viejo_mas_de_90s(self):
        for age in (90.1, 300.0, 99999.0):
            v = argos_mcp._classify_heartbeat(age)
            self.assertIn("no está corriendo", v, age)
            self.assertNotIn("reimportando", v, age)

    def test_ausente(self):
        v = argos_mcp._classify_heartbeat(None)
        self.assertIn("no está corriendo", v)
        self.assertIn("sin heartbeat", v)


# (3) — campo aditivo en argos_ping

class PingHeartbeatFieldTest(_ProjTest):

    def _patch_ping(self, result):
        def fake_wscall(project, payload, **kw):
            return {"ok": True, "result": result}
        argos_mcp.ws_call = fake_wscall

    def test_campo_aditivo_con_heartbeat(self):
        _write_hb(self.proj, age_s=8.0)
        self._patch_ping({"pong": True, "ts": 123.5})
        out = argos_mcp.call_tool("argos_ping", {"project": str(self.proj)})
        self.assertFalse(out.get("isError"))
        data = json.loads(out["content"][0]["text"])
        # campos existentes intactos
        self.assertTrue(data["pong"])
        self.assertEqual(data["ts"], 123.5)
        # campo aditivo nuevo
        self.assertIn("heartbeat_age_s", data)
        self.assertTrue(7.0 <= data["heartbeat_age_s"] <= 10.0, data["heartbeat_age_s"])

    def test_campo_null_sin_heartbeat(self):
        self._patch_ping({"pong": True, "ts": 1.0})
        out = argos_mcp.call_tool("argos_ping", {"project": str(self.proj)})
        data = json.loads(out["content"][0]["text"])
        self.assertIsNone(data["heartbeat_age_s"])
        self.assertTrue(data["pong"])


# (4) — clasificación en el log ERROR al agotar el retry

class ExhaustionClassificationTest(_ProjTest):

    def _install(self, outcomes):
        fake = FakeConnect(outcomes)
        argos_mcp.connect = fake
        argos_mcp.RETRY_BASE_DELAY = 0.01  # backoff casi nulo: prueba rápida
        return fake

    def test_agotado_con_heartbeat_fresco_loguea_reimportando(self):
        _write_hb(self.proj, age_s=5.0)
        self._install([ConnectionRefusedError("editor no responde")] * 4)
        out = argos_mcp.call_tool("argos_ping", {"project": str(self.proj)})
        self.assertTrue(out.get("isError"))
        errs = self._errors()
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("ocupado", errs[0])
        self.assertIn("reimportando", errs[0])

    def test_agotado_sin_heartbeat_loguea_editor_muerto(self):
        self._install([ConnectionRefusedError("editor ausente")] * 4)
        out = argos_mcp.call_tool("argos_ping", {"project": str(self.proj)})
        self.assertTrue(out.get("isError"))
        errs = self._errors()
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("no está corriendo", errs[0])
        self.assertIn("sin heartbeat", errs[0])

    def test_agotado_con_heartbeat_viejo_loguea_editor_muerto(self):
        _write_hb(self.proj, age_s=600.0)
        self._install([ConnectionRefusedError("x")] * 4)
        argos_mcp.call_tool("argos_ping", {"project": str(self.proj)})
        errs = self._errors()
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("no está corriendo", errs[0])
        self.assertNotIn("reimportando", errs[0])

    def test_error_logico_no_loguea_clasificacion(self):
        # JSON inválido del plugin: no transitorio -> sin ERROR de clasificación
        self._install(['{"ok": true,,'])
        out = argos_mcp.call_tool("argos_ping", {"project": str(self.proj)})
        self.assertTrue(out.get("isError"))
        self.assertEqual(self._errors(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
