#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_undo_cycles.py — Sondas P2-P4 del ciclo undo/redo (BLOQUE B3 complemento).

Requiere editor ARGOS real. Sin editor OMITE con exit 0; con --require-editor sale 3.

Casos:
  P2 ciclo completo `delete -> undo -> redo -> undo -> accion nueva` (fix B3.1).
  P3 batch con `node.delete` INTERNO -> undo/redo del batch + accion posterior.
  P4 abort con create+delete mezclados -> estado/owner/editor.log/consistencia.
     P4 es el test rojo->verde del MEDIA: `rollback()` debe aplicar los undo en
     orden DIRECTO (como el UndoRedo real) para que `set_owner` corra con el nodo
     YA re-insertado en el arbol (si no: "Invalid owner. Owner must be an ancestor").

Uso: python test_undo_cycles.py <proyecto> [--require-editor] [--scene res://main.tscn]
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
LEFTOVERS = ("P2A", "P2B", "P3X", "P3Y", "P3Z", "P4X", "P4Y", "P4Bad")


def _load(project: str):
    base = pathlib.Path(project) / ".argos"
    port = int((base / "port").read_text(encoding="utf-8").strip())
    token = (base / "token").read_text(encoding="utf-8").strip()
    return port, token


def main():
    if len(sys.argv) < 2:
        print("uso: test_undo_cycles.py <proyecto> [--require-editor] [--scene PATH]")
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
    log_path = pathlib.Path(project) / ".argos" / "editor.log"

    def send(action, **kw):
        rid[0] += 1
        msg = {"id": rid[0], "action": action, "token": token, **kw}
        ws.send(json.dumps(msg))
        return json.loads(ws.recv(timeout=45))

    def check(label, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + label
              + ((" | " + str(detail)[:220]) if detail else ""))
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

    def create(name, parent="."):
        return send("node.create", parent=parent, **{"class": "Node2D", "name": name})

    def owner_of(path):
        """owner del nodo (str) o None si no existe/consulta falla."""
        r = send("node.get_prop", path=path, prop="owner")
        if not r.get("ok"):
            return None
        return (r.get("result") or {}).get("value")

    def vec2(encoded):
        try:
            a, b = str(encoded).replace("@vec2:", "").split(",")
            return (float(a), float(b))
        except Exception:
            return None

    def log_len():
        try:
            return log_path.stat().st_size
        except OSError:
            return 0

    def log_since(off):
        try:
            with open(log_path, "rb") as f:
                f.seek(off)
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    try:
        send("scene.open", path=scene)
        for nm in LEFTOVERS:
            send("node.delete", path=nm)

        # ---------------- P2: ciclo completo delete/undo/redo/undo/nueva -------------
        check("P2.0 create P2A", create("P2A").get("ok") is True)
        send("node.delete", path="P2A")
        check("P2.1 delete P2A lo quita", "P2A" not in names(), names())
        send("undo.undo")
        ns = names()
        check("P2.2 undo restaura P2A", ns.count("P2A") == 1, ns)
        send("undo.redo")
        ns = names()
        check("P2.3 redo borra P2A (sin duplicar)", ns.count("P2A") == 0, ns)
        send("undo.undo")
        ns = names()
        check("P2.4 2o undo restaura P2A otra vez", ns.count("P2A") == 1, ns)
        check("P2.5 create P2B (accion nueva)", create("P2B").get("ok") is True)
        ns = names()
        check("P2.6 P2A SOBREVIVE al ciclo completo (fix B3.1)",
              ns.count("P2A") == 1 and ns.count("P2B") == 1, ns)
        r = send("node.set_prop", path="P2A", prop="position", value="@vec2:5,6")
        v = vec2((send("node.get_prop", path="P2A", prop="position").get("result") or {}).get("value"))
        check("P2.7 P2A sigue usable tras el ciclo", r.get("ok") is True and v == (5.0, 6.0), v)

        # ---------------- P3: batch con node.delete INTERNO --------------------------
        send("node.create", parent=".", **{"class": "Node2D", "name": "P3Y"})
        owner_before = owner_of("P3Y")
        r = send("action.batch", name="p3 delete+create", on_error="abort", ops=[
            {"action": "node.delete", "path": "P3Y"},
            {"action": "node.create", "parent": ".", "class": "Node2D", "name": "P3X"},
        ])
        check("P3.1 batch delete+create ok", r.get("ok") is True, r.get("error"))
        ns = names()
        check("P3.2 tras batch: P3Y fuera y P3X dentro",
              "P3Y" not in ns and ns.count("P3X") == 1, ns)
        send("undo.undo")
        ns = names()
        check("P3.3 undo del batch restaura P3Y y quita P3X",
              ns.count("P3Y") == 1 and "P3X" not in ns, ns)
        check("P3.4 owner de P3Y preservado por undo del batch",
              owner_of("P3Y") == owner_before, {"before": owner_before, "after": owner_of("P3Y")})
        send("undo.redo")
        ns = names()
        check("P3.5 redo del batch: P3Y fuera, P3X dentro",
              "P3Y" not in ns and ns.count("P3X") == 1, ns)
        send("undo.undo")
        ns = names()
        check("P3.6 2o undo del batch vuelve a restaurar P3Y",
              ns.count("P3Y") == 1 and "P3X" not in ns, ns)
        create("P3Z")  # accion nueva -> descarta rama redo
        ns = names()
        check("P3.7 P3Y sobrevive a la accion posterior",
              ns.count("P3Y") == 1 and ns.count("P3Z") == 1 and "P3X" not in ns, ns)
        r = send("node.set_prop", path="P3Y", prop="position", value="@vec2:7,8")
        check("P3.8 P3Y usable tras el ciclo", r.get("ok") is True, r)

        for nm in ("P3X", "P3Y", "P3Z"):
            send("node.delete", path=nm)

        # ---------------- P4: abort con create+delete mezclados ----------------------
        create("P4Y")
        owner_before = owner_of("P4Y")
        baseline = names()
        off = log_len()
        r = send("action.batch", name="p4 abort mixed", on_error="abort", ops=[
            {"action": "node.create", "parent": ".", "class": "Node2D", "name": "P4X"},
            {"action": "node.delete", "path": "P4Y"},
            {"action": "node.create", "parent": "NoExisteXYZ", "class": "Node2D", "name": "P4Bad"},
        ])
        check("P4.1 abort ok:false / no_parent",
              r.get("ok") is False and r.get("error") == "no_parent", {"ok": r.get("ok"), "e": r.get("error")})
        ns = names()
        check("P4.2 rollback: P4X fuera y P4Y restaurado",
              "P4X" not in ns and ns.count("P4Y") == 1, ns)
        owner_after = owner_of("P4Y")
        check("P4.3 owner de P4Y preservado tras rollback (MEDIA)",
              owner_after == owner_before, {"before": owner_before, "after": owner_after})
        check("P4.4 estado identico al inicio (orden exacto)",
              ns == baseline, {"baseline": baseline, "after": ns})
        tail = log_since(off)
        bad = [ln for ln in tail.splitlines()
               if "Invalid owner" in ln or ("ERROR:" in ln and "set_owner" in ln)]
        check("P4.5 editor.log sin 'Invalid owner'", not bad, bad[:2])
        send("undo.undo")
        ns = names()
        check("P4.6 abort NO registra accion (undo revierte el create previo de P4Y)",
              "P4Y" not in ns, ns)
        send("undo.redo")
        # P4.7: tras un abort, el modo batch NO queda pegado (micro-fix): un batch
        # nuevo se ejecuta normal.
        r = send("action.batch", name="p4 after abort", on_error="abort",
                 ops=[{"action": "node.set_prop", "path": "P4Y", "prop": "position",
                       "value": "@vec2:3,4"}])
        check("P4.7 batch nuevo tras abort funciona (modo batch limpio)",
              r.get("ok") is True, {"ok": r.get("ok"), "e": r.get("error")})
        send("undo.undo")

        # limpieza final (sin guardar)
        for nm in LEFTOVERS:
            send("node.delete", path=nm)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if failures:
        print("PROBE UNDO CYCLES: FAIL ->", failures)
        sys.exit(1)
    print("PROBE UNDO CYCLES: PASS (P2 ciclo completo; P3 batch con delete interno; P4 abort consistente)")
    sys.exit(0)


if __name__ == "__main__":
    main()
