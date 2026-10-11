#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_mcp_selftest.py — Test del servidor MCP ARGOS (protocolo + integración).

Fases:
  1) PROTOCOLO (siempre salvo --resume con fase ya OK): initialize + tools/list.
     Si falla -> exit 2.
  2) INTEGRACIÓN (requiere editor ARGOS corriendo): si no hay editor, OMITE con
     mensaje claro y exit 0 (no es fallo de protocolo). Si hay editor:
     ping/status/tree/create/undo/redo/error_path/limpieza. Si algo falla -> exit 3.

BLOQUE B4 (resume): persiste phase + resultado por check en
<proyecto>/.argos/selftest_mcp_state.json (escritura atómica). `--resume` no repite
initialize/tools/list si el protocolo ya pasó y salta el prefijo de checks OK.
Antes de retomar se ejecuta un pre-clean idempotente (node-del FromMCP) UNA vez.

Uso: python argos_mcp_selftest.py <proyecto> [--require-editor] [--resume] [--reset] [--state PATH]
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from argos_selftest import load_state, save_state  # noqa: E402 (reuso del estado §4.1)

PY = sys.executable
STATE_FILENAME = "selftest_mcp_state.json"
EXPECTED_TOOLS = 27  # 26 + argos_history (BLOQUE B2)


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("servidor cerro stdout")
    return json.loads(line)


def main():
    ap = argparse.ArgumentParser(description="Selftest del servidor MCP ARGOS (con resume).")
    ap.add_argument("project")
    ap.add_argument("--require-editor", action="store_true")
    ap.add_argument("--resume", action="store_true", help="no repite protocolo ni checks ya OK")
    ap.add_argument("--reset", action="store_true", help="borra el estado antes de correr")
    ap.add_argument("--state", default=None, help="ruta del JSON de estado")
    args = ap.parse_args()

    state_path = args.state or str(pathlib.Path(args.project) / ".argos" / STATE_FILENAME)
    if args.reset:
        try:
            pathlib.Path(state_path).unlink()
        except OSError:
            pass
    state = load_state(state_path)
    state["suite"] = "mcp"
    state["project"] = args.project

    env = {**os.environ, "ARGOS_PROJECT": args.project}
    proc = subprocess.Popen(
        [PY, os.path.join(HERE, "argos_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def call(name, arguments, rid):
        send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}})
        return recv(proc)

    def text_of(resp):
        try:
            return resp["result"]["content"][0]["text"]
        except Exception:
            return json.dumps(resp)

    def is_err(resp):
        try:
            return bool(resp["result"].get("isError"))
        except Exception:
            return True

    def record(sid, ok):
        state["steps"][sid] = {"ok": bool(ok), "ts_ms": int(time.time() * 1000)}
        save_state(state_path, state)

    failures = []

    try:
        # ---- FASE 1: PROTOCOLO
        if args.resume and state["steps"].get("protocol", {}).get("ok"):
            print("PROTO initialize: OK (resume)")
            print("PROTO tools/list: OK (resume)")
        else:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "selftest", "version": "1"}}})
            r = recv(proc)
            info = r.get("result", {}).get("serverInfo", {})
            proto_ok = info.get("name") == "argos"
            print("PROTO initialize:", "OK" if proto_ok else "FAIL", info)
            if not proto_ok:
                failures.append("initialize")
            send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            r = recv(proc)
            tools = [t["name"] for t in r["result"]["tools"]]
            print("PROTO tools/list: OK n=%d" % len(tools))
            if len(tools) != EXPECTED_TOOLS:
                failures.append("tools/list(n=%d, esperado %d)" % (len(tools), EXPECTED_TOOLS))
            if "argos_history" not in tools:
                failures.append("tools/list(sin argos_history)")
            # B2: argos_history responde (lee .argos/history.json; no requiere editor)
            hr = call("argos_history", {"limit": 5}, 3)
            htext = text_of(hr)
            hist_ok = (not is_err(hr)) and '"ok": true' in htext
            print("PROTO argos_history:", "OK" if hist_ok else "FAIL", htext[:80].replace("\n", " "))
            if not hist_ok:
                failures.append("argos_history")
            record("protocol", not failures)
            if failures:
                print("SELFTEST: FAIL PROTOCOLO ->", failures)
                sys.exit(2)

        # ---- FASE 2: INTEGRACIÓN
        # El ping (barato) siempre comprueba que hay editor, aunque se reanude.
        r = call("argos_ping", {}, 10)
        if is_err(r) or "pong" not in text_of(r):
            print("INTEGRACIÓN OMITIDA: no hay editor ARGOS corriendo (protocolo OK).")
            print("  (para integración completa: lanza el editor y repite; pista: argos_editor.py start <proj> --wait 40)")
            sys.exit(0 if not args.require_editor else 3)
        print("INTEG ping: OK")
        state["phase"] = "integration"

        # ---- Checks de integración: (id, fn(rid) -> (ok, extra))
        def c_status(rid):
            r = call("argos_status", {}, rid)
            return (not is_err(r)), ""

        def c_tree(rid):
            r = call("argos_scene_tree", {}, rid)
            return (not is_err(r) and '"edited"' in text_of(r)), ""

        def c_create(rid):
            r = call("argos_node_create",
                     {"parent": ".", "class": "Label", "name": "FromMCP",
                      "props": {"text": "Hola MCP", "esta_no_existe": 1}}, rid)
            t = text_of(r)
            return (not is_err(r) and '"created"' in t), \
                "skipped-veraz: " + ("OK" if '"esta_no_existe"' in t else "FAIL")

        def c_undo(rid):
            call("argos_undo", {}, rid)
            r = call("argos_scene_tree", {}, rid + 1)
            return ("FromMCP" not in text_of(r)), ""

        def c_redo(rid):
            call("argos_redo", {}, rid)
            r = call("argos_scene_tree", {}, rid + 1)
            return ("FromMCP" in text_of(r)), ""

        def c_error_path(rid):
            r2 = call("argos_node_set_prop",
                      {"path": "FromMCP", "prop": "no_existe_xyz", "value": 1}, rid)
            t = text_of(r2)
            return ("no_prop" in t or not is_err(r2)), t[:80]

        def c_save(rid):
            call("argos_node_delete", {"path": "FromMCP"}, rid)
            r = call("argos_scene_save", {}, rid + 1)
            return (not is_err(r)), ""

        CHECKS = [("status", c_status), ("tree", c_tree), ("node_create", c_create),
                  ("undo", c_undo), ("redo", c_redo), ("error_path", c_error_path),
                  ("save", c_save)]

        # resume: salta el PREFIJO de checks ya OK (evita reanudar a mitad frágil)
        start = 0
        if args.resume:
            for i, (sid, _) in enumerate(CHECKS):
                if state["steps"].get(sid, {}).get("ok"):
                    start = i + 1
                else:
                    break

        if start >= len(CHECKS):
            save_state(state_path, state)
            print("INTEG: 0 pendientes (todos los checks ya OK)")
            print("SELFTEST: PASS (protocolo + integración con editor)")
            sys.exit(0)

        # Pre-clean idempotente UNA vez antes de continuar
        call("argos_node_delete", {"path": "FromMCP"}, 9)

        rid = 100
        for sid, fn in CHECKS[start:]:
            ok, extra = fn(rid)
            rid += 10
            print("INTEG %s: %s%s" % (sid, "OK" if ok else "FAIL", (" | " + extra) if extra else ""))
            record(sid, ok)
            if not ok:
                failures.append(sid)

        if failures:
            print("SELFTEST: FAIL INTEGRACIÓN ->", failures)
            sys.exit(3)
        print("SELFTEST: PASS (protocolo + integración con editor)")
        sys.exit(0)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        err = proc.stderr.read()
        if err.strip():
            print("--- stderr servidor ---")
            print(err.strip())


if __name__ == "__main__":
    main()
