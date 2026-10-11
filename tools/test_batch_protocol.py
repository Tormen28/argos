#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_batch_protocol.py — Probe de INTEGRACIÓN WS de `action.batch` (BLOQUE B3).

Convierte los puntos del spike §1.3 en tests reales contra el editor. Sin editor
OMITE con exit 0; con --require-editor sale 3.

Casos (WS directo, sin servidor MCP):
  A) batch de 3 ops (create + create + set_prop) -> 1 sola accion de undo:
     una undo quita AMBOS nodos; un redo los devuelve; SIN duplicacion (count==1).
  B) on_error=abort a mitad -> el op fallido aborta, se revierte lo aplicado y NO
     se registra accion (un undo posterior revierte la accion PREVIA, no el batch).
  C) on_error=continue -> se saltan los ops que fallan y se commitea lo bueno
     (1 undo revierte los buenos; stopped_at = indice del primer fallo).
  D) too_many_ops (>64) -> error, sin mutar.
  E) op undo.* dentro del batch -> busy_batch (sin mutar).
  F) op no batchable (scene.new) -> not_batchable (sin mutar).

Uso: python test_batch_protocol.py <proyecto> [--require-editor] [--scene res://main.tscn]
"""
import json
import pathlib
import sys

try:
    from websockets.sync.client import connect
except ImportError:
    print("ERROR: falta 'websockets'", file=sys.stderr)
    sys.exit(2)

SCENE_DEFAULT = "res://main.tscn"
LEFTOVERS = ("BatchA", "BatchB", "BatchC", "BatchD", "BatchE", "BatchPre",
             "BatchG1", "BatchG2", "BatchG3", "BatchH")
BATCH_NAME = "spike batch"


def _load(project: str):
    base = pathlib.Path(project) / ".argos"
    port = int((base / "port").read_text(encoding="utf-8").strip())
    token = (base / "token").read_text(encoding="utf-8").strip()
    return port, token


def main():
    if len(sys.argv) < 2:
        print("uso: test_batch_protocol.py <proyecto> [--require-editor] [--scene PATH]")
        sys.exit(2)
    project = sys.argv[1]
    require_editor = "--require-editor" in sys.argv
    scene = SCENE_DEFAULT
    if "--scene" in sys.argv:
        scene = sys.argv[sys.argv.index("--scene") + 1]

    try:
        port, token = _load(project)
        ws = connect("ws://127.0.0.1:%d" % port, open_timeout=5, close_timeout=2, max_size=None)
    except Exception as e:
        print("PROBE OMITIDO: no hay editor ARGOS corriendo (%s)." % type(e).__name__)
        print("  (lanza el editor y repite; pista: argos_editor.py start <proj> --wait 40)")
        sys.exit(0 if not require_editor else 3)

    failures = []
    rid = [0]

    def send(action, **kw):
        rid[0] += 1
        msg = {"id": rid[0], "action": action, "token": token, **kw}
        ws.send(json.dumps(msg))
        return json.loads(ws.recv(timeout=45))

    def check(label, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + label
              + ((" | " + str(detail)[:160]) if detail else ""))
        if not cond:
            failures.append(label)

    def names():
        r = send("scene.get_tree")
        out = []

        def walk(n):
            out.append(str(n.get("rel", n.get("name", ""))))
            for c in (n.get("children") or []):
                walk(c)

        edited = (r.get("result") or {}).get("edited") or {}
        if edited:
            walk(edited.get("tree", {}))
        return out

    def create_op(name, parent="."):
        return {"action": "node.create", "parent": parent, "class": "Node2D", "name": name}

    def vec2(encoded):
        try:
            a, b = str(encoded).replace("@vec2:", "").split(",")
            return (float(a), float(b))
        except Exception:
            return None

    try:
        # Escena determinista + limpieza idempotente de restos.
        send("scene.open", path=scene)
        for nm in LEFTOVERS:
            send("node.delete", path=nm)

        # (A) batch de 3 ops -> 1 undo
        ops = [
            create_op("BatchA"),
            create_op("BatchB"),
            {"action": "node.set_prop", "path": "BatchA", "prop": "position", "value": "@vec2:10,20"},
        ]
        r = send("action.batch", name=BATCH_NAME, on_error="abort", ops=ops)
        res = r.get("result") or {}
        check("A1 batch ok", r.get("ok") is True, r.get("error"))
        check("A2 undo_action", res.get("undo_action") == "ARGOS: " + BATCH_NAME, res.get("undo_action"))
        check("A3 3 ops ok", len(res.get("ops") or []) == 3 and all(o.get("ok") for o in res["ops"]), res.get("ops"))
        check("A4 stopped_at null", res.get("stopped_at") is None, res.get("stopped_at"))
        ns = names()
        check("A5 ambos creados", "BatchA" in ns and "BatchB" in ns, ns)
        check("A6 sin duplicacion", ns.count("BatchA") == 1 and ns.count("BatchB") == 1, ns)

        r = send("undo.undo")
        ns = names()
        check("A7 1 undo revierte TODO", r.get("ok") and "BatchA" not in ns and "BatchB" not in ns, ns)
        r = send("undo.redo")
        ns = names()
        check("A8 1 redo los devuelve", r.get("ok") and "BatchA" in ns and "BatchB" in ns, ns)
        check("A9 redo sin duplicacion", ns.count("BatchA") == 1 and ns.count("BatchB") == 1, ns)
        # limpiar A (1 undo quita ambos)
        send("undo.undo")

        # (B) abort a mitad: no muta y no registra accion
        send("node.create", parent=".", **{"class": "Node2D", "name": "BatchPre"})
        ops = [create_op("BatchC"),
               {"action": "node.create", "parent": "NoExisteXYZ", "class": "Node2D", "name": "BatchC2"}]
        r = send("action.batch", name="abort batch", on_error="abort", ops=ops)
        check("B1 abort ok:false", r.get("ok") is False, r)
        check("B2 error no_parent", r.get("error") == "no_parent", r.get("error"))
        check("B3 results 2", len(r.get("results") or []) == 2, r.get("results"))
        ns = names()
        check("B4 abort no muta (BatchC fuera)", "BatchC" not in ns, ns)
        send("undo.undo")
        ns = names()
        check("B5 abort no registra accion (undo revierte BatchPre)",
              "BatchPre" not in ns, ns)

        # (C) continue: salta el fallido y commitea lo bueno
        ops = [create_op("BatchD"),
               {"action": "node.create", "parent": ".", "class": "NoSuchClassXYZ", "name": "bad"},
               create_op("BatchE")]
        r = send("action.batch", name="continue batch", on_error="continue", ops=ops)
        res = r.get("result") or {}
        opok = [bool(o.get("ok")) for o in (res.get("ops") or [])]
        check("C1 continue ok", r.get("ok") is True, r.get("error"))
        check("C2 patron [ok,err,ok]", opok == [True, False, True], opok)
        check("C3 stopped_at=1", res.get("stopped_at") == 1, res.get("stopped_at"))
        ns = names()
        check("C4 D y E creados", "BatchD" in ns and "BatchE" in ns, ns)
        send("undo.undo")
        ns = names()
        check("C5 1 undo revierte D y E", "BatchD" not in ns and "BatchE" not in ns, ns)

        # (D) too_many_ops
        r = send("action.batch", name="big", on_error="abort", ops=[create_op("X%d" % i) for i in range(65)])
        check("D too_many_ops", r.get("error") == "too_many_ops", r.get("error"))

        # (E) undo dentro de batch -> busy_batch
        r = send("action.batch", name="nested", on_error="abort", ops=[{"action": "undo.undo"}])
        check("E busy_batch", r.get("error") == "busy_batch", r.get("error"))

        # (F) op no batchable -> not_batchable
        r = send("action.batch", name="bad", on_error="abort",
                 ops=[{"action": "scene.new", "path": "res://NO.tscn"}])
        check("F not_batchable", r.get("error") == "not_batchable", r.get("error"))

        # (G) §1.3 CLAVE: 3 node.create -> 1 undo -> estado previo EXACTO.
        before_g = names()
        r = send("action.batch", name="three creates", on_error="abort",
                 ops=[create_op("BatchG1"), create_op("BatchG2"), create_op("BatchG3")])
        res = r.get("result") or {}
        after_batch = names()
        print("G antes      :", before_g)
        print("G tras batch :", after_batch)
        check("G1 batch 3 creates ok",
              r.get("ok") is True and len(res.get("ops") or []) == 3
              and all(o.get("ok") for o in res["ops"]), res.get("ops"))
        check("G2 los 3 aparecen",
              all(n in after_batch for n in ("BatchG1", "BatchG2", "BatchG3")), after_batch)
        check("G3 sin duplicacion",
              all(after_batch.count(n) == 1 for n in ("BatchG1", "BatchG2", "BatchG3")), after_batch)
        new_nodes = [n for n in after_batch if n not in before_g]
        check("G4 solo 3 nodos nuevos", sorted(new_nodes) == ["BatchG1", "BatchG2", "BatchG3"], new_nodes)
        send("undo.undo")
        after_undo = names()
        print("G tras 1 undo:", after_undo)
        check("G5 UN undo -> estado previo EXACTO", after_undo == before_g,
              {"before": before_g, "after_undo": after_undo})

        # (H) B3.1: batch MIXTO (create + set_prop sobre el mismo nodo nuevo).
        # El editor emite en su log un "get_path_to ... (not inside tree)" (ruido
        # cosmetico del callback de live-debug al deshacer una propiedad sobre un
        # nodo recien removido); lo que se afirma aqui es la FUNCION correcta.
        r = send("action.batch", name="mixed", on_error="abort", ops=[
            create_op("BatchH"),
            {"action": "node.set_prop", "path": "BatchH", "prop": "position", "value": "@vec2:11,22"},
        ])
        res = r.get("result") or {}
        check("H1 batch mixto ok", r.get("ok") is True, r.get("error"))
        check("H2 2 ops ok", len(res.get("ops") or []) == 2 and all(o.get("ok") for o in res["ops"]),
              res.get("ops"))
        ns = names()
        check("H3 nodo creado", ns.count("BatchH") == 1, ns)
        prop = (send("node.get_prop", path="BatchH", prop="position").get("result") or {}).get("value")
        check("H4 prop aplicada", vec2(prop) == (11.0, 22.0), prop)
        send("undo.undo")
        ns = names()
        check("H5 1 undo revierte el batch mixto", "BatchH" not in ns, ns)
        send("undo.redo")
        ns = names()
        check("H6 1 redo restaura sin duplicar", ns.count("BatchH") == 1, ns)
        prop = (send("node.get_prop", path="BatchH", prop="position").get("result") or {}).get("value")
        check("H7 prop re-aplicada en redo", vec2(prop) == (11.0, 22.0), prop)
        send("undo.undo")  # limpiar H

        # limpieza final (sin guardar): quita restos
        for nm in LEFTOVERS:
            send("node.delete", path=nm)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if failures:
        print("PROBE BATCH: FAIL ->", failures)
        sys.exit(1)
    print("PROBE BATCH: PASS (1 undo agrupa N ops; abort/continue/no-batchable verificados)")
    sys.exit(0)


if __name__ == "__main__":
    main()
