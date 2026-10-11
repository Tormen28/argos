#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_selftest.py — Test de integración del portal ARGOS vía CLI (requiere editor).

Uso:
  python argos_selftest.py <proyecto> [--resume] [--reset] [--from <step>] [--state PATH]

Verifica sobre la escena editada (nodos T1/T2 desechables):
  status -> create T1/T2 -> tree -> undo LIFO -> redo -> delete + undo -> limpieza + save.
  BLOQUE B3: action.batch de 2 create (B1/B2) -> 1 undo los revierte -> redo -> limpieza.

BLOQUE B4 (resume): la secuencia es STEPS = [(id, fn), …] con `call` inyectable
(test sin editor). El estado se persiste en <proyecto>/.argos/selftest_state.json
(§4.1), con escritura atómica y arranque limpio ante JSON corrupto.
  --resume salta pasos ya OK; --reset borra el estado; --from <step> arranca ahí.
Antes de retomar se ejecuta un pre-clean idempotente (node-del T1/T2) UNA vez.

Salida: línea por check (PASS/FAIL) + resumen. Exit 0 si todo pasa (1 si falla).
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

CLI = pathlib.Path(__file__).with_name("argos_cli.py")
STATE_FILENAME = "selftest_state.json"
PRECLEAN_NODES = ("T1", "T2", "B1", "B2")
STATE_VERSION = 1


def call(project, *args):
    r = subprocess.run(
        [sys.executable, str(CLI), "--project", project, *args],
        capture_output=True, text=True, errors="replace", timeout=60,
    )
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"ok": False, "raw": ((r.stdout or "") + (r.stderr or ""))[:500]}


def node_names(tree_resp):
    out = []

    def walk(n):
        out.append(n.get("rel", n.get("name", "")))
        for c in (n.get("children") or []):
            walk(c)

    edited = (tree_resp.get("result") or {}).get("edited") or {}
    if edited:
        walk(edited.get("tree", {}))
    return out


# ---- estado de resume (§4.1) -------------------------------------------------

def _empty_state(project=""):
    return {"version": STATE_VERSION, "suite": "cli", "project": project,
            "updated_ms": 0, "steps": {}, "phase": "integration"}


def load_state(path):
    """Carga el estado; ausente o JSON corrupto -> estado limpio (fail-safe)."""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("steps"), dict):
            data.setdefault("project", "")
            data.setdefault("phase", "integration")
            return data
    except (OSError, ValueError):
        pass
    return _empty_state()


def save_state(path, state):
    """Escritura atómica (.tmp + os.replace) e idempotente."""
    state["updated_ms"] = int(time.time() * 1000)
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ---- pasos -------------------------------------------------------------------

def _step_status(project, call):
    r = call(project, "status")
    return bool(r.get("ok")), (r.get("result") or {}).get("project", "")


def _step_create_T1(project, call):
    r = call(project, "node-create", ".", "Label", "T1", '{"text":"uno"}')
    return bool(r.get("ok")), r.get("result")


def _step_create_T2(project, call):
    r = call(project, "node-create", ".", "Label", "T2", '{"text":"dos"}')
    return bool(r.get("ok")), r.get("result")


def _step_tree_has_both(project, call):
    t = node_names(call(project, "tree"))
    return ("T1" in t and "T2" in t), t


def _step_undo_lifo(project, call):
    r = call(project, "undo")
    t = node_names(call(project, "tree"))
    return (bool(r.get("ok")) and "T1" in t and "T2" not in t), t


def _step_redo_t2(project, call):
    r = call(project, "redo")
    t = node_names(call(project, "tree"))
    return (bool(r.get("ok")) and "T1" in t and "T2" in t), t


def _step_delete_T1(project, call):
    r = call(project, "node-del", "T1")
    t = node_names(call(project, "tree"))
    return (bool(r.get("ok")) and "T1" not in t and "T2" in t), t


def _step_undo_restore_T1(project, call):
    call(project, "undo")
    t = node_names(call(project, "tree"))
    return ("T1" in t and "T2" in t), t


def _step_cleanup(project, call):
    call(project, "node-del", "T1")
    call(project, "node-del", "T2")
    r = call(project, "save")
    t = node_names(call(project, "tree"))
    return (bool(r.get("ok")) and "T1" not in t and "T2" not in t), t


# --- BLOQUE B3: undo grouping (action.batch) ---------------------------------

def _step_batch_create(project, call):
    """action.batch de 2 create -> 1 sola accion de undo (§1.5)."""
    ops = [{"action": "node.create", "parent": ".", "class": "Label", "name": "B1"},
           {"action": "node.create", "parent": ".", "class": "Label", "name": "B2"}]
    payload = {"action": "action.batch", "name": "selftest batch",
               "on_error": "abort", "ops": ops}
    r = call(project, "raw", json.dumps(payload))
    t = node_names(call(project, "tree"))
    return (bool(r.get("ok")) and "B1" in t and "B2" in t), r.get("result")


def _step_batch_undo_once(project, call):
    """UN undo revierte AMBOS create del batch (demuestra agrupacion)."""
    r = call(project, "undo")
    t = node_names(call(project, "tree"))
    return (bool(r.get("ok")) and "B1" not in t and "B2" not in t), t


def _step_batch_redo_once(project, call):
    """UN redo los devuelve (agrupados)."""
    r = call(project, "redo")
    t = node_names(call(project, "tree"))
    return (bool(r.get("ok")) and "B1" in t and "B2" in t), t


def _step_batch_cleanup(project, call):
    call(project, "node-del", "B1")
    call(project, "node-del", "B2")
    t = node_names(call(project, "tree"))
    return ("B1" not in t and "B2" not in t), t


# Orden: los `batch_*` van ANTES de `cleanup` porque `cleanup` es el paso
# TERMINAL (borra T1/T2 y hace `save`), de modo que `--from cleanup` reanuda
# exactamente 1 paso y el estado final queda persistido. Encadenar
# `undo_restore_T1 -> batch_*` es semánticamente válido: la escena tiene T1/T2 y el
# batch crea/revierte/reaplica B1/B2 sobre ella; `batch_cleanup` los quita y
# `cleanup` cierra con save.
STEPS = [
    ("status", _step_status),
    ("create_T1", _step_create_T1),
    ("create_T2", _step_create_T2),
    ("tree_T1_T2", _step_tree_has_both),
    ("undo_LIFO", _step_undo_lifo),
    ("redo_T2", _step_redo_t2),
    ("delete_T1", _step_delete_T1),
    ("undo_restore_T1", _step_undo_restore_T1),
    ("batch_create", _step_batch_create),
    ("batch_undo_once", _step_batch_undo_once),
    ("batch_redo_once", _step_batch_redo_once),
    ("batch_cleanup", _step_batch_cleanup),
    ("cleanup", _step_cleanup),
]


def run_suite(project, call, state_path, resume=False, reset=False, from_step=None, log=print):
    """Ejecuta los pasos pendientes; devuelve dict con fails/ran/preclean_ran."""
    if reset:
        try:
            pathlib.Path(state_path).unlink()
        except OSError:
            pass
    state = load_state(state_path)
    state["project"] = state.get("project") or project

    start = 0
    if from_step:
        ids = [sid for sid, _ in STEPS]
        if from_step not in ids:
            log("WARN: --from '%s' no existe; arranco desde el inicio" % from_step)
        else:
            start = ids.index(from_step)

    pending = [(sid, fn) for i, (sid, fn) in enumerate(STEPS)
               if i >= start and not (resume and state["steps"].get(sid, {}).get("ok"))]

    if not pending:
        log("0 pendientes (%d/%d pasos ya OK)" % (len(STEPS), len(STEPS)))
        return {"fails": [], "ran": 0, "preclean_ran": False}

    # Pre-clean idempotente UNA vez antes de continuar (determinista al reanudar).
    for nm in PRECLEAN_NODES:
        call(project, "node-del", nm)

    fails = []
    ran = 0
    for sid, fn in pending:
        ok, extra = fn(project, call)
        ran += 1
        log(("PASS " if ok else "FAIL ") + sid + ((" | " + str(extra)) if extra else ""))
        state["steps"][sid] = {"ok": bool(ok), "ts_ms": int(time.time() * 1000)}
        state["phase"] = "integration"
        save_state(state_path, state)
        if not ok:
            fails.append(sid)
    return {"fails": fails, "ran": ran, "preclean_ran": True}


def main():
    ap = argparse.ArgumentParser(description="Selftest CLI de ARGOS (con resume).")
    ap.add_argument("project")
    ap.add_argument("--resume", action="store_true", help="salta los pasos ya OK")
    ap.add_argument("--reset", action="store_true", help="borra el estado antes de correr")
    ap.add_argument("--from", dest="from_step", default=None, help="arranca en el paso indicado")
    ap.add_argument("--state", default=None, help="ruta del JSON de estado")
    args = ap.parse_args()

    if not pathlib.Path(args.project).is_dir():
        print("ERROR: proyecto no encontrado:", args.project)
        sys.exit(2)
    state_path = args.state or str(pathlib.Path(args.project) / ".argos" / STATE_FILENAME)

    res = run_suite(args.project, call, state_path,
                    resume=args.resume, reset=args.reset, from_step=args.from_step)
    if res["fails"]:
        print("SELFTEST CLARO: FAIL -> " + repr(res["fails"]))
        sys.exit(1)
    if res["ran"] == 0:
        print("SELFTEST CLARO: PASS (0 pendientes)")
    else:
        print("SELFTEST CLARO: PASS (%d pasos)" % res["ran"])
    sys.exit(0)


if __name__ == "__main__":
    main()
