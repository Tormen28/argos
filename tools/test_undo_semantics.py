#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_undo_semantics.py — Caracterizacion + regresion del undo (BLOQUE B3.1).

Bug PREEXISTENTE (a): `node.create` -> `node.delete` -> `undo` -> nueva accion =>
el nodo borrado DESAPARECE aunque el undo lo habia restaurado.

Causa (evidencia: core/object/undo_redo.cpp de Godot):
  UndoRedo::discard_redo() libera las referencias de los `do_ops` de la rama redo
  descartada via Operation::delete_reference() -> memdelete(obj) para Nodes.
  `_node_delete` registraba `add_do_reference(n)`: al hacer undo, n vuelve al arbol,
  pero la referencia sigue en los do_ops de la accion de borrado; la siguiente
  accion descarta el redo -> memdelete(n) -> el nodo (que estaba en el arbol) se va.
  El idioma canonico del editor (SceneTreeDock::_delete_nodes) usa
  `add_undo_reference(n)`: la referencia vive en los undo_ops y sobrevive al
  descarte del redo.

Uso: python test_undo_semantics.py <proyecto> [--require-editor] [--scene res://main.tscn]
Sin editor OMITE con exit 0; con --require-editor sale 3.
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
LEFTOVERS = ("U1", "U2", "U3", "N1", "N2", "N3", "N4")


def _load(project: str):
    base = pathlib.Path(project) / ".argos"
    port = int((base / "port").read_text(encoding="utf-8").strip())
    token = (base / "token").read_text(encoding="utf-8").strip()
    return port, token


def main():
    if len(sys.argv) < 2:
        print("uso: test_undo_semantics.py <proyecto> [--require-editor] [--scene PATH]")
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
              + ((" | " + str(detail)[:200]) if detail else ""))
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

    def create(name):
        return send("node.create", parent=".", **{"class": "Node2D", "name": name})

    try:
        send("scene.open", path=scene)
        for nm in LEFTOVERS:
            send("node.delete", path=nm)

        # (A) Bug preexistente: create -> delete -> undo -> NUEVA ACCION.
        check("A0 create U1 ok", create("U1").get("ok") is True)
        send("node.delete", path="U1")
        send("undo.undo")
        ns = names()
        check("A1 undo restaura U1", "U1" in ns, ns)
        r = create("N1")
        ns = names()
        check("A2 nueva accion ok", r.get("ok") is True, r)
        # ESTA es la asercion que FALLA con el bug (add_do_reference):
        check("A3 U1 SOBREVIVE a la nueva accion (no se pierde)", "U1" in ns, ns)
        check("A4 N1 presente y sin duplicar", ns.count("N1") == 1, ns)
        check("A5 U1 sin duplicar", ns.count("U1") == 1, ns)

        # (B) El nodo restaurado sigue siendo utilizable: borrar de nuevo + undo.
        check("B0 create U2 ok", create("U2").get("ok") is True)
        send("node.delete", path="U2")
        send("undo.undo")
        check("B1 create N2 ok", create("N2").get("ok") is True)
        ns = names()
        check("B2 U2 sobrevive tras nueva accion", "U2" in ns, ns)
        r = send("node.delete", path="U2")
        ns = names()
        check("B3 U2 borrable de nuevo", r.get("ok") is True and "U2" not in ns, {"r": r, "ns": ns})
        send("undo.undo")
        ns = names()
        check("B4 undo lo devuelve", "U2" in ns, ns)

        # (C) Ciclo repetido (referencias no liberan nodos vivos).
        create("U3")
        send("node.delete", path="U3")
        send("undo.undo")
        create("N3")
        send("node.delete", path="U3")
        send("undo.undo")
        create("N4")
        ns = names()
        check("C1 U3/N3/N4 vivos tras ciclos", all(n in ns for n in ("U3", "N3", "N4")), ns)

        # limpieza final (sin guardar)
        for nm in LEFTOVERS:
            send("node.delete", path=nm)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if failures:
        print("PROBE UNDO SEMANTICS: FAIL ->", failures)
        sys.exit(1)
    print("PROBE UNDO SEMANTICS: PASS (delete->undo->nueva accion preserva el nodo)")
    sys.exit(0)


if __name__ == "__main__":
    main()
