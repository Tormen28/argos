#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_plugin_limits.py — Probe de INTEGRACIÓN de los caps del plugin (BLOQUE B1).

Requiere el editor ARGOS corriendo (con el addon instalado y actualizado). Si no
hay editor, OMITE con mensaje claro y exit 0 (como argos_mcp_selftest); con
--require-editor sale 3.

Verifica por WS directo (sin pasar por el servidor MCP, para ejercitar el plugin):
  - 9 MiB de payload  -> payload_too_large   (guarda MAX_MSG_BYTES en el plugin)
  - scene.get_tree max_nodes=2 -> edited.truncated == true
  - capture.scene w=99999 -> bad_size ; out sin .png -> bad_out
  - node.create props >128 claves -> too_many_props
  - node.create props con valor >4096 chars -> prop_too_long
  - scene.open path no res:// -> bad_path

Uso: python test_plugin_limits.py <proyecto> [--require-editor]
"""
import json
import pathlib
import sys

try:
    from websockets.sync.client import connect
except ImportError:
    print("ERROR: falta 'websockets'", file=sys.stderr)
    sys.exit(2)

MAX_MSG_BYTES = 8 * 1024 * 1024


def _load(project: str):
    base = pathlib.Path(project) / ".argos"
    port = int((base / "port").read_text(encoding="utf-8").strip())
    token = (base / "token").read_text(encoding="utf-8").strip()
    return port, token


def main():
    if len(sys.argv) < 2:
        print("uso: test_plugin_limits.py <proyecto> [--require-editor]")
        sys.exit(2)
    project = sys.argv[1]
    require_editor = "--require-editor" in sys.argv

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
        msg = {"id": rid[0], "token": token, "action": action, **kw}
        ws.send(json.dumps(msg))
        return json.loads(ws.recv(timeout=60))

    def check(label, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + label + ((" | " + str(detail)[:160]) if detail else ""))
        if not cond:
            failures.append(label)

    try:
        # (0) auth/ping: confirma que hablamos con el plugin
        ping = send("ping")
        check("ping", ping.get("ok") is True, ping)

        # (1) payload gigante -> payload_too_large (antes de JSON.parse)
        rid[0] += 1
        big = {"id": rid[0], "token": token, "action": "ping", "pad": "x" * (MAX_MSG_BYTES + 1024)}
        ws.send(json.dumps(big))
        r = json.loads(ws.recv(timeout=60))
        check("payload_too_large", r.get("error") == "payload_too_large", r.get("error"))

        # (2) scene.get_tree recortado
        r = send("scene.get_tree", max_nodes=2)
        edited = (r.get("result") or {}).get("edited") or {}
        check("tree truncated", r.get("ok") and edited.get("truncated") is True, edited.get("truncated"))

        # (3) capture: tamaño fuera de rango y out no .png (validación antes de headless)
        r = send("capture.scene", out="C:/tmp/argos_probe.png", w=99999, h=648)
        check("capture bad_size", r.get("error") == "bad_size", r.get("error"))
        r = send("capture.scene", out="C:/tmp/argos_probe.txt", w=100, h=100)
        check("capture bad_out", r.get("error") == "bad_out", r.get("error"))

        # (4) node.create: demasiadas props / valor demasiado largo
        many = {("k%d" % i): 1 for i in range(129)}
        r = send("node.create", parent=".", **{"class": "Label", "name": "ProbeCaps", "props": many})
        check("too_many_props", r.get("error") == "too_many_props", r.get("error"))
        r = send("node.create", parent=".", **{"class": "Label", "name": "ProbeCaps",
                                               "props": {"text": "a" * 4097}})
        check("prop_too_long", r.get("error") == "prop_too_long", r.get("error"))
        big_props = {("k%d" % i): "a" * 4000 for i in range(100)}
        r = send("node.create", parent=".", **{"class": "Label", "name": "ProbeCaps",
                                               "props": big_props})
        check("props_too_large", r.get("error") == "props_too_large", r.get("error"))

        # (5) paths no res://
        r = send("scene.open", path="C:/tmp/nope.tscn")
        check("scene.open bad_path", r.get("error") == "bad_path", r.get("error"))
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if failures:
        print("PROBE LIMITS: FAIL ->", failures)
        sys.exit(1)
    print("PROBE LIMITS: PASS (caps del plugin verificados contra editor real)")
    sys.exit(0)


if __name__ == "__main__":
    main()
