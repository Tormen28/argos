#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_selftest_resume.py — Verifica el resume del selftest CLI (BLOQUE B4).

`argos_selftest.run_suite` recibe un `call` inyectable, así que probamos sin editor:
  (1) fallo en un paso k -> el estado persiste los pasos OK y el fallido no;
      re-run con --resume sólo re-ejecuta desde k (no repite los OK) y el
      pre-clean idempotente corre UNA vez por corrida con pasos pendientes.
  (2) JSON corrupto en el estado -> arranque limpio (corre todo) y reescribe válido.
  (3) corrida completa + --resume -> "0 pendientes" (no llama nada).
  (4) escritura atómica: no queda *.tmp.
  (5) --from <step> arranca en ese paso.

Ejecutar: python tools/test_selftest_resume.py
"""
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import argos_selftest  # noqa: E402


class FakeCall:
    """Modela mínimamente la escena (nodos + pila undo/redo) para el selftest."""

    def __init__(self):
        self.calls = []
        self.nodes = []
        self._undo = []
        self._redo = []
        self.fail_create = None   # nombre de T que debe fallar al crear
        self.fail_save = False

    # --- modelo ---
    @staticmethod
    def _inv(act):
        k = act[0]
        if k == "del":
            return ("add", act[1])
        if k == "add":
            return ("del", act[1])
        if k == "batch_del":
            return ("batch_add", act[1])
        if k == "batch_add":
            return ("batch_del", act[1])
        return act

    def _apply(self, act):
        kind, name = act
        if kind == "del" and name in self.nodes:
            self.nodes.remove(name)
        elif kind == "add" and name not in self.nodes:
            self.nodes.append(name)
        elif kind == "batch_del":
            for n in name:
                if n in self.nodes:
                    self.nodes.remove(n)
        elif kind == "batch_add":
            for n in name:
                if n not in self.nodes:
                    self.nodes.append(n)

    def __call__(self, project, *args):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "status":
            return {"ok": True, "result": {"project": project}}
        if cmd == "node-create":
            name = args[3]
            if name == self.fail_create:
                return {"ok": False, "error": "boom"}
            self.nodes.append(name)
            act = ("del", name)
            self._undo.append(act)
            self._redo.clear()
            return {"ok": True, "result": {"created": name}}
        if cmd == "node-del":
            name = args[1]
            if name in self.nodes:
                self.nodes.remove(name)
                self._undo.append(("add", name))
                self._redo.clear()
                return {"ok": True, "result": {"deleted": name}}
            return {"ok": False, "error": "no_node"}
        if cmd == "undo":
            if self._undo:
                act = self._undo.pop()
                self._apply(act)
                self._redo.append(self._inv(act))
            return {"ok": True, "result": {"undone": True}}
        if cmd == "redo":
            if self._redo:
                act = self._redo.pop()
                self._apply(act)
                self._undo.append(self._inv(act))
            return {"ok": True, "result": {"redone": True}}
        if cmd == "tree":
            kids = [{"rel": n, "name": n} for n in self.nodes]
            return {"ok": True, "result": {"edited": {"tree": {"rel": ".", "children": kids}}}}
        if cmd == "save":
            if self.fail_save:
                return {"ok": False, "error": "save_failed"}
            return {"ok": True, "result": {"saved": True}}
        if cmd == "raw":
            # BLOQUE B3: modela action.batch de create como UNA accion de undo.
            try:
                m = json.loads(args[1])
            except Exception:
                return {"ok": False, "error": "bad_json"}
            if m.get("action") != "action.batch":
                return {"ok": False, "error": "unknown_raw"}
            names = [op.get("name") for op in (m.get("ops") or [])
                     if op.get("action") == "node.create"]
            for n in names:
                if n not in self.nodes:
                    self.nodes.append(n)
            self._undo.append(("batch_del", names))
            self._redo.clear()
            return {"ok": True, "result": {
                "undo_action": "ARGOS: " + str(m.get("name", "")),
                "ops": [{"action": "node.create", "ok": True} for _ in names],
                "stopped_at": None}}
        return {"ok": False, "error": "unknown"}

    # --- helpers de test ---
    def reset_log(self):
        self.calls = []

    def count(self, cmd):
        return len([c for c in self.calls if c and c[0] == cmd])


class ResumeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argos_resume_")
        self.proj = self.tmp
        self.state = str(pathlib.Path(self.tmp) / ".argos" / "selftest_state.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # (1) resume reanuda sin repetir los OK
    def test_resume_salta_pasos_ok(self):
        fake = FakeCall()
        fake.fail_save = True
        res1 = argos_selftest.run_suite(self.proj, fake, self.state, resume=False, log=lambda *a: None)
        self.assertEqual(res1["fails"], ["cleanup"])
        self.assertEqual(res1["ran"], len(argos_selftest.STEPS))
        self.assertTrue(res1["preclean_ran"])
        state1 = argos_selftest.load_state(self.state)
        self.assertTrue(state1["steps"]["create_T1"]["ok"])
        self.assertFalse(state1["steps"]["cleanup"]["ok"])

        # re-run con resume: ahora save OK -> sólo corre cleanup
        fake.fail_save = False
        fake.reset_log()
        res2 = argos_selftest.run_suite(self.proj, fake, self.state, resume=True, log=lambda *a: None)
        self.assertEqual(res2["fails"], [])
        self.assertEqual(res2["ran"], 1)
        self.assertTrue(res2["preclean_ran"])
        self.assertEqual(fake.count("node-create"), 0)   # no repite create_T1/T2
        # pre-clean UNA vez al inicio (4 node-del: T1/T2/B1/B2), luego cleanup (2) = 6
        self.assertEqual(fake.count("node-del"), 6)
        self.assertEqual(fake.calls[0], ("node-del", "T1"))
        self.assertEqual(fake.calls[1], ("node-del", "T2"))

    # (2) estado corrupto -> arranque limpio
    def test_estado_corrupto_arranque_limpio(self):
        pathlib.Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(self.state).write_text("{esto no es json", encoding="utf-8")
        fake = FakeCall()
        res = argos_selftest.run_suite(self.proj, fake, self.state, resume=True, log=lambda *a: None)
        self.assertEqual(res["fails"], [])
        self.assertEqual(res["ran"], len(argos_selftest.STEPS))  # corrió todo
        # el archivo quedó reescrito válido
        data = json.loads(pathlib.Path(self.state).read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertTrue(data["steps"]["cleanup"]["ok"])

    # (3) corrida completa + resume -> 0 pendientes, sin llamadas
    def test_resume_sin_pendientes(self):
        fake = FakeCall()
        res1 = argos_selftest.run_suite(self.proj, fake, self.state, resume=False, log=lambda *a: None)
        self.assertEqual(res1["fails"], [])
        fake.reset_log()
        res2 = argos_selftest.run_suite(self.proj, fake, self.state, resume=True, log=lambda *a: None)
        self.assertEqual(res2["ran"], 0)
        self.assertEqual(res2["fails"], [])
        self.assertFalse(res2["preclean_ran"])
        self.assertEqual(fake.calls, [])  # 0 pendientes: no toca nada

    # (4) escritura atómica: sin *.tmp
    def test_escritura_atomica(self):
        fake = FakeCall()
        argos_selftest.run_suite(self.proj, fake, self.state, resume=False, log=lambda *a: None)
        leftover = list(pathlib.Path(self.proj).rglob("*.tmp"))
        self.assertEqual(leftover, [])
        self.assertTrue(pathlib.Path(self.state).exists())

    # (5) --from arranca en el paso indicado
    def test_from_arranca_en_paso(self):
        fake = FakeCall()
        res = argos_selftest.run_suite(self.proj, fake, self.state, resume=False,
                                       from_step="cleanup", log=lambda *a: None)
        self.assertEqual(res["fails"], [])
        self.assertEqual(res["ran"], 1)                 # sólo cleanup
        self.assertEqual(fake.count("node-create"), 0)  # no corre create
        self.assertNotIn("status", [c[0] for c in fake.calls])

    # (6) --reset borra el estado
    def test_reset_borra_estado(self):
        fake = FakeCall()
        argos_selftest.run_suite(self.proj, fake, self.state, resume=False, log=lambda *a: None)
        argos_selftest.run_suite(self.proj, fake, self.state, resume=True, reset=True, log=lambda *a: None)
        data = argos_selftest.load_state(self.state)
        # tras reset corrió todo de nuevo, así que todos los pasos están OK
        self.assertEqual(len(data["steps"]), len(argos_selftest.STEPS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
