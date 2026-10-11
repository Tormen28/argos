#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_retry_backoff.py — Verifica el retry con backoff exponencial de argos_mcp.ws_call.

BLOQUE A-1. Casos:
  (a) 2 fallos transitorios y luego OK -> la operación se completa; delays 500ms y
      1000ms (patrón 500 -> 1000 -> 2000, jitter ±20%); puerto y token se releen
      de <proyecto>/.argos/ en cada reintento (el token rota por sesión de editor).
  (b) error lógico (JSON inválido del plugin, o respuesta de error de protocolo /
      token inválido) -> NO se reintenta: falla o retorna en el primer intento.
  (c) 3 fallos transitorios -> OK en el 4º intento (delay 2000ms verificado).
  (d) 4 fallos transitorios -> agota los 3 reintentos y propaga el último error.

Ejecutar (con un intérprete que tenga 'websockets', como argos_mcp.py):
  python tools/test_retry_backoff.py
"""
import asyncio
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

import argos_mcp  # noqa: E402  (importa el módulo bajo test; requiere 'websockets')

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK  # noqa: E402


# ---------------------------- fakes (solo stdlib) ----------------------------

class FakeConn:
    """WS falso: send()/recv() devuelven o lanzan lo guionizado (fallo en plena operación)."""

    def __init__(self, recv_outcome=None, send_exc=None, recv_exc=None):
        self.recv_outcome = recv_outcome
        self.send_exc = send_exc
        self.recv_exc = recv_exc
        self.sent = []

    def send(self, data):
        if self.send_exc is not None:
            raise self.send_exc
        self.sent.append(json.loads(data))

    def recv(self, timeout=None):
        if self.recv_exc is not None:
            raise self.recv_exc
        return self.recv_outcome

    def close(self):
        pass


class FakeConnect:
    """Sustituye argos_mcp.connect.

    Cada elemento de `outcomes` guioniza un intento:
      - BaseException        -> connect() la lanza (p.ej. ConnectionRefusedError)
      - str                  -> connect OK; recv() devuelve esa respuesta JSON
      - ("send", exc)        -> connect OK; send() lanza exc (fallo de envío)
      - ("recv", exc)        -> connect OK; send OK; recv() lanza exc (fallo de recepción)
    `rotate(n)` se invoca al entrar a cada connect para simular que el editor
    reinició: reescribe .argos/port y .argos/token (rotación por sesión).
    Como ws_call lee puerto/token ANTES de conectar, cada intento usa los valores
    escritos por el giro anterior -> verifica la relectura por intento.
    """

    def __init__(self, outcomes, rotate=None):
        self.outcomes = list(outcomes)
        self.rotate = rotate
        self.times = []  # time.monotonic() por intento (para medir delays)
        self.uris = []
        self.conns = []

    def __call__(self, uri, open_timeout=None, close_timeout=None):
        n = len(self.times)
        if n >= len(self.outcomes):
            raise AssertionError("connect() llamado más veces de lo guionizado")
        self.times.append(time.monotonic())
        self.uris.append(uri)
        if self.rotate:
            self.rotate(n)
        out = self.outcomes[n]
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, tuple):
            where, exc = out
            conn = FakeConn(send_exc=exc) if where == "send" else FakeConn(recv_exc=exc)
        else:
            conn = FakeConn(recv_outcome=out)
        self.conns.append(conn)
        return conn


class HangingConn:
    """WS falso que ACEPTA la conexión pero NUNCA responde: recv() se bloquea."""

    def __init__(self):
        self.timeouts = []

    def send(self, data):
        pass

    def recv(self, timeout=None):
        self.timeouts.append(timeout)
        time.sleep(timeout or 0.0)  # endpoint colgado: agota el timeout recibido
        raise TimeoutError("recv timeout (endpoint colgado)")

    def close(self):
        pass


class HangingConnect:
    """Sustituye argos_mcp.connect: cada intento devuelve un HangingConn."""

    def __init__(self):
        self.times = []
        self.uris = []
        self.conns = []

    def __call__(self, uri, open_timeout=None, close_timeout=None):
        self.times.append(time.monotonic())
        self.uris.append(uri)
        conn = HangingConn()
        self.conns.append(conn)
        return conn


class CaptureHandler(logging.Handler):
    """Captura los registros del logger 'argos_mcp' sin escribir a stderr."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []  # [(levelno, mensaje)]

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))


# --------------------------------- tests -------------------------------------

class RetryBackoffTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argos_retry_")
        self.proj = pathlib.Path(self.tmp)
        (self.proj / ".argos").mkdir()
        self._write_endpoint(9181, "tok-1")
        # aislamiento del estado global del módulo
        self._orig_conn, self._orig_key = argos_mcp._CONN, argos_mcp._CONN_KEY
        self._orig_connect = argos_mcp.connect
        self._orig_read_endpoint = argos_mcp._read_endpoint
        self._orig_budget = argos_mcp.WS_CALL_MAX_TOTAL_S
        argos_mcp._CONN, argos_mcp._CONN_KEY = None, None
        # captura de logs del retry
        self.handler = CaptureHandler()
        argos_mcp._log.addHandler(self.handler)
        self._orig_propagate = argos_mcp._log.propagate
        argos_mcp._log.propagate = False

    def tearDown(self):
        argos_mcp.connect = self._orig_connect
        argos_mcp._read_endpoint = self._orig_read_endpoint
        argos_mcp.WS_CALL_MAX_TOTAL_S = self._orig_budget
        argos_mcp._CONN, argos_mcp._CONN_KEY = self._orig_conn, self._orig_key
        argos_mcp._log.removeHandler(self.handler)
        argos_mcp._log.propagate = self._orig_propagate
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_endpoint(self, port, token):
        (self.proj / ".argos" / "port").write_text(str(port), encoding="utf-8")
        (self.proj / ".argos" / "token").write_text(token, encoding="utf-8")

    def _install(self, outcomes, rotate=True):
        def rot(n):  # el editor "reinicia" entre intentos: puerto y token nuevos
            self._write_endpoint(9182 + n, "tok-%d" % (n + 2))
        fake = FakeConnect(outcomes, rotate=rot if rotate else None)
        argos_mcp.connect = fake
        return fake

    def _warnings(self):
        return [m for lvl, m in self.handler.records if lvl >= logging.WARNING]

    def _infos(self):
        return [m for lvl, m in self.handler.records if lvl == logging.INFO]

    # (a) — requerido por la especificación
    def test_a_dos_fallos_transitorios_luego_ok(self):
        """(a) 2 fallos transitorios -> OK: delays 500/1000ms y puerto+token releídos."""
        ok_resp = json.dumps({"ok": True, "result": {"echo": 1}})
        fake = self._install([
            ConnectionRefusedError("editor reimportando (sin listener)"),  # fallo al conectar
            ("recv", ConnectionClosedError(None, None)),  # cierre anómalo a mitad de la operación
            ok_resp,
        ])
        t0 = time.monotonic()
        resp = argos_mcp.ws_call(str(self.proj), {"action": "ping"})
        elapsed = time.monotonic() - t0

        self.assertEqual(resp, {"ok": True, "result": {"echo": 1}})
        self.assertEqual(len(fake.times), 3)  # 1 intento + 2 reintentos
        # delays entre intentos: 500ms y 1000ms (±20% jitter + holgura de medición)
        d1 = fake.times[1] - fake.times[0]
        d2 = fake.times[2] - fake.times[1]
        self.assertTrue(0.38 <= d1 <= 0.70, "delay 1 fuera de patrón: %.3fs" % d1)
        self.assertTrue(0.78 <= d2 <= 1.30, "delay 2 fuera de patrón: %.3fs" % d2)
        self.assertGreaterEqual(elapsed, 1.2)  # esperó el backoff, no fue instantáneo
        # puerto y token releídos de .argos/ en cada reintento (rotación por sesión)
        self.assertEqual(fake.uris, [
            "ws://127.0.0.1:9181", "ws://127.0.0.1:9182", "ws://127.0.0.1:9183",
        ])
        self.assertEqual([c.sent[0]["token"] for c in fake.conns], ["tok-2", "tok-3"])
        # logging: WARNING por reintento («intento N/3, error X») + INFO al éxito
        warns = self._warnings()
        self.assertEqual(len(warns), 2, warns)
        self.assertIn("intento 1/3", warns[0])
        self.assertIn("ConnectionRefusedError", warns[0])
        self.assertIn("intento 2/3", warns[1])
        self.assertIn("ConnectionClosedError", warns[1])
        self.assertTrue(any("reintento" in m for m in self._infos()), self._infos())

    # (b) — requerido por la especificación
    def test_b1_error_logico_json_invalido_sin_reintento(self):
        """(b) JSON inválido del plugin -> JSONDecodeError en el 1er intento, sin retry."""
        fake = self._install(['{"ok": true,,'])  # respuesta no-JSON
        t0 = time.monotonic()
        with self.assertRaises(json.JSONDecodeError):
            argos_mcp.ws_call(str(self.proj), {"action": "ping"})
        elapsed = time.monotonic() - t0
        self.assertEqual(len(fake.times), 1)  # un solo intento
        self.assertLess(elapsed, 0.3)  # sin delays de backoff
        self.assertEqual(self._warnings(), [])  # sin WARNING de reintento

    def test_b2_error_de_protocolo_token_invalido_sin_reintento(self):
        """(b) Error lógico del plugin (token inválido) se devuelve tal cual, 1 intento."""
        auth_err = json.dumps({"ok": False, "error": "auth", "detail": "token invalido"})
        fake = self._install([auth_err])
        resp = argos_mcp.ws_call(str(self.proj), {"action": "ping"})
        self.assertEqual(resp, {"ok": False, "error": "auth", "detail": "token invalido"})
        self.assertEqual(len(fake.times), 1)
        self.assertEqual(self._warnings(), [])

    # extras — verifican el escalón completo 500 -> 1000 -> 2000 y el agotamiento
    def test_c_tres_fallos_ok_en_cuarto_intento(self):
        """3 fallos transitorios -> OK en el intento 4; delay de 2000ms verificado."""
        ok_resp = json.dumps({"ok": True, "result": None})
        fake = self._install([
            ConnectionRefusedError("a"),
            ("send", BrokenPipeError("b")),      # fallo de envío en plena operación
            ("recv", asyncio.TimeoutError()),    # timeout de recepción (== TimeoutError en Py >= 3.11)
            ok_resp,
        ])
        resp = argos_mcp.ws_call(str(self.proj), {"action": "status"})
        self.assertEqual(resp, {"ok": True, "result": None})
        self.assertEqual(len(fake.times), 4)  # 4 intentos totales
        d1 = fake.times[1] - fake.times[0]
        d2 = fake.times[2] - fake.times[1]
        d3 = fake.times[3] - fake.times[2]
        for d, lo, hi in ((d1, 0.38, 0.70), (d2, 0.78, 1.30), (d3, 1.55, 2.55)):
            self.assertTrue(lo <= d <= hi, "delay fuera de patrón: %.3fs (rango %s-%s)" % (d, lo, hi))
        warns = self._warnings()
        self.assertEqual(len(warns), 3, warns)
        self.assertIn("intento 3/3", warns[2])
        self.assertTrue(any("reintento" in m for m in self._infos()), self._infos())

    def test_d_agota_reintentos_y_propaga(self):
        """4 fallos transitorios -> propaga el último error tras 3 reintentos."""
        fake = self._install([ConnectionRefusedError("editor ausente")] * 4)
        with self.assertRaises(ConnectionRefusedError):
            argos_mcp.ws_call(str(self.proj), {"action": "ping"})
        self.assertEqual(len(fake.times), 4)
        self.assertEqual(len(self._warnings()), 3)
        self.assertEqual(self._infos(), [])  # nunca llegó a éxito

    def test_clasificacion_de_errores_transitorios(self):
        """_is_transient: solo transporte caído/refused/reset, timeouts y cierres WS."""
        transitorios = [
            ConnectionRefusedError(),
            ConnectionResetError(),
            ConnectionAbortedError(),
            BrokenPipeError(),
            TimeoutError(),
            asyncio.TimeoutError(),
            ConnectionClosedError(None, None),
            ConnectionClosedOK(None, None),
        ]
        for e in transitorios:
            self.assertTrue(argos_mcp._is_transient(e), "debería ser transitorio: %s" % type(e).__name__)
        logicos = [
            json.JSONDecodeError("Expecting value", "{", 0),
            ValueError("x"),
            OSError("boom genérico"),
            KeyError("action"),
        ]
        for e in logicos:
            self.assertFalse(argos_mcp._is_transient(e), "NO debería ser transitorio: %s" % type(e).__name__)

    # (e) — [M2] presupuesto total: peor caso acotado
    def test_e_presupuesto_total_acota_el_peor_caso(self):
        """[M2] endpoint que acepta TCP y no responde -> el presupuesto total acota ws_call.

        Se reduce WS_CALL_MAX_TOTAL_S para no esperar 60 s reales. Se comprueba que
        el total queda por debajo del presupuesto (con holgura) y que NO se agotan
        los 4 intentos x 25 s (peor caso previo ≈103 s).
        """
        argos_mcp.WS_CALL_MAX_TOTAL_S = 1.0
        fake = HangingConnect()  # simula un endpoint colgado (recv duerme y expira)
        argos_mcp.connect = fake

        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            argos_mcp.ws_call(str(self.proj), {"action": "ping"})
        elapsed = time.monotonic() - t0

        self.assertLessEqual(elapsed, 1.0 + 0.6, "worst-case no acotado: %.2fs" % elapsed)
        self.assertLess(len(fake.times), 4, "no debería agotar los 4 intentos dentro del presupuesto")
        # los recv nunca esperan más allá del tiempo restante del presupuesto
        self.assertTrue(all((t or 0.0) <= 1.0 + 1e-6 for t in fake.conns[0].timeouts), fake.conns[0].timeouts)

    def test_e2_presupuesto_por_defecto_es_60s(self):
        """[M2] el presupuesto por defecto acota el peor caso: 60 s < 4 x recv(25 s)."""
        self.assertEqual(argos_mcp.WS_CALL_MAX_TOTAL_S, 60.0)
        self.assertLess(argos_mcp.WS_CALL_MAX_TOTAL_S, 4 * 25.0)

    # (f) — [B1] lectura de endpoint: E/S transitoria se reintenta; lógica propaga rápido
    def test_f1_io_transitorio_al_leer_endpoint_se_reintenta(self):
        """[B1] PermissionError al leer token/port (transitorio) -> se reintenta y termina OK."""
        ok_resp = json.dumps({"ok": True, "result": {"echo": 7}})
        fake = self._install([ok_resp])  # un único connect OK

        real_read = argos_mcp._read_endpoint
        calls = {"n": 0}

        def flaky_read(project, port_file, port_default):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("token bloqueado durante la rotación (Windows)")
            return real_read(project, port_file, port_default)

        argos_mcp._read_endpoint = flaky_read
        try:
            resp = argos_mcp.ws_call(str(self.proj), {"action": "status"})
        finally:
            argos_mcp._read_endpoint = real_read

        self.assertEqual(resp, {"ok": True, "result": {"echo": 7}})
        self.assertEqual(calls["n"], 2)         # 1er intento falla al leer, 2º lee bien
        self.assertEqual(len(fake.times), 1)    # el fallo fue antes de conectar
        self.assertTrue(any("PermissionError" in m for m in self._warnings()), self._warnings())

    def test_f2_port_corrupto_propaga_rapido_sin_reintento(self):
        """[B1] `port` corrupto (ValueError lógico) -> propaga rápido, sin retry ni connect."""
        (self.proj / ".argos" / "port").write_text("not-a-port", encoding="utf-8")
        fake = self._install([ConnectionRefusedError("no debería conectar")], rotate=False)

        t0 = time.monotonic()
        with self.assertRaises(ValueError):
            argos_mcp.ws_call(str(self.proj), {"action": "ping"})
        elapsed = time.monotonic() - t0

        self.assertEqual(len(fake.times), 0)    # ni siquiera intenta conectar
        self.assertLess(elapsed, 0.3)           # sin delays de backoff
        self.assertEqual(self._warnings(), [])

    def test_clasificacion_retryable(self):
        """_is_retryable = transitorios de transporte + E/S de archivo; sin errores lógicos."""
        reintentables = [
            ConnectionRefusedError(),
            TimeoutError(),
            ConnectionClosedError(None, None),
            PermissionError("token bloqueado"),   # OSError de archivo (B1)
            FileNotFoundError("race en .argos/port"),
            OSError("E/S transitoria"),
        ]
        for e in reintentables:
            self.assertTrue(argos_mcp._is_retryable(e), "debería reintentarse: %s" % type(e).__name__)
        no_reintentables = [
            json.JSONDecodeError("Expecting value", "{", 0),
            ValueError("invalid literal for int() with base 10: 'not-a-port'"),
            KeyError("action"),
        ]
        for e in no_reintentables:
            self.assertFalse(argos_mcp._is_retryable(e), "NO debería reintentarse: %s" % type(e).__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
