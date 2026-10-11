#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_cli.py — Cliente del portal ARGOS (Hermes <-> Godot).

Ejemplos:
  python argos_cli.py --project C:/path/proj ping
  python argos_cli.py --project C:/path/proj tree
  python argos_cli.py --project C:/path/proj node-create . Label MiLabel '{"text":"Hola"}'
  python argos_cli.py --project C:/path/proj node-set MiLabel text Hola
  python argos_cli.py --project C:/path/proj capture C:/tmp/op.png
"""
import argparse
import json
import pathlib
import sys

try:
    from websockets.sync.client import connect
except ImportError:
    print("ERROR: falta 'websockets' (pip install websockets)", file=sys.stderr)
    sys.exit(2)

MAP = {
    "history": "history",  # local: lee .argos/history.json (no usa WS)
    "ping": "ping",
    "status": "status",
    "tree": "scene.get_tree",
    "open": "scene.open",
    "save": "scene.save",
    "reload": "scene.reload",
    "play": "run.play_main",
    "play-scene": "run.play_scene",
    "stop": "run.stop",
    "playing": "run.is_playing",
    "capture": "capture.editor_2d",
    "capture-scene": "capture.scene",
    "fs-scan": "fs.scan",
    "fs-update": "fs.update_file",
    "scene-new": "scene.new",
    "instantiate": "node.instantiate",
    "reparent": "node.reparent",
    "node-create": "node.create",
    "node-get": "node.get_prop",
    "node-set": "node.set_prop",
    "node-del": "node.delete",
    "undo": "undo.undo",
    "redo": "undo.redo",
    "quit": "editor.quit",
    "rt-ping": "runtime.ping",
    "rt-info": "runtime.info",
    "rt-capture": "runtime.capture",
    "raw": "raw",
}


def load_cred(project, port, token):
    if project:
        base = pathlib.Path(project) / ".argos"
        try:
            if port is None and (base / "port").exists():
                port = int((base / "port").read_text(encoding="utf-8").strip())
            if token is None and (base / "token").exists():
                token = (base / "token").read_text(encoding="utf-8").strip()
        except Exception as e:  # pragma: no cover
            print(f"WARN leyendo .argos: {e}", file=sys.stderr)
    return port or 9080, token or ""


def make_message(cmd, rest):
    if cmd == "raw":
        return json.loads(rest[0]) if rest else {}
    msg = {"action": MAP[cmd], "id": 1}
    r = list(rest)
    if cmd == "open":
        msg["path"] = r[0]
    elif cmd in ("play-scene", "capture", "fs-update"):
        msg["path"] = r[0]
    elif cmd == "capture-scene":
        msg["out"] = r[0]
        if len(r) > 1: msg["path"] = r[1]
        if len(r) > 2: msg["w"] = int(r[2])
        if len(r) > 3: msg["h"] = int(r[3])
    elif cmd == "scene-new":
        msg["path"] = r[0]
        if len(r) > 1: msg["root_class"] = r[1]
        if len(r) > 2: msg["name"] = r[2]
    elif cmd == "instantiate":
        msg["scene"] = r[0]
        msg["parent"] = r[1]
        if len(r) > 2: msg["name"] = r[2]
    elif cmd == "reparent":
        msg["path"] = r[0]
        msg["parent"] = r[1]
        if len(r) > 2: msg["keep_transform"] = (r[2].lower() == "true")
    elif cmd == "node-create":
        msg["parent"] = r[0]
        msg["class"] = r[1]
        msg["name"] = r[2]
        if len(r) > 3:
            msg["props"] = json.loads(r[3])
    elif cmd == "node-get":
        msg["path"] = r[0]
        msg["prop"] = r[1]
    elif cmd == "node-set":
        msg["path"] = r[0]
        msg["prop"] = r[1]
        raw = r[2]
        try:
            msg["value"] = json.loads(raw)
        except Exception:
            msg["value"] = raw
    elif cmd == "node-del":
        msg["path"] = r[0]
    elif cmd == "quit":
        if len(r) > 0 and r[0].lower() == "force":
            msg["force"] = True
    elif cmd == "rt-capture":
        msg["out"] = r[0]
        if len(r) > 1:
            msg["wait_frames"] = int(r[1])
    return msg


def cmd_history(args) -> int:
    """`history`: lee .argos/history.json (BLOQUE B2; la CLI SOLO lee)."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import argos_history
    res = argos_history.read(args.project, limit=args.limit, since=args.since,
                             tool=args.tool, status=args.status)
    if args.as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        print("ARGOS history: ERROR %s" % res.get("error"), file=sys.stderr)
        return 1
    print("history: %d entrada(s)" % res.get("count", 0))
    for e in res.get("entries", []):
        print("  [%s] %-5s %-22s %-16s %s ms%s" % (
            e.get("ts"), e.get("status"), e.get("tool"), e.get("action"),
            e.get("duration_ms"),
            (" | error=%s" % e.get("error")) if e.get("error") else ""))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Cliente ARGOS (Hermes <-> Godot)")
    ap.add_argument("--project", default=None, help="dir del proyecto Godot (lee .argos/token y .argos/port)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--limit", type=int, default=50, help="history: max entradas (default 50)")
    ap.add_argument("--since", default=None, help="history: ISO8601; solo posteriores")
    ap.add_argument("--tool", default=None, help="history: filtra por tool")
    ap.add_argument("--status", default=None, choices=["ok", "error"], help="history: filtra por status")
    ap.add_argument("--json", dest="as_json", action="store_true", help="history: salida JSON")
    ap.add_argument("command", choices=sorted(MAP.keys()))
    ap.add_argument("rest", nargs="*")
    args = ap.parse_args()

    if args.command == "history":
        sys.exit(cmd_history(args))

    if args.command == "raw" and not args.rest:
        print("ERROR: raw requiere el JSON como argumento", file=sys.stderr)
        sys.exit(2)

    port, token = load_cred(args.project, args.port, args.token)
    if args.command.startswith("rt-") and args.port is None and args.project:
        rp = pathlib.Path(args.project) / ".argos" / "runtime_port"
        port = int(rp.read_text(encoding="utf-8").strip()) if rp.exists() else 9091
    msg = make_message(args.command, args.rest)
    msg["token"] = token

    uri = f"ws://127.0.0.1:{port}"
    timeout = 30 if args.command in ("capture", "capture-scene", "rt-capture") else 15
    try:
        with connect(uri, open_timeout=5, close_timeout=2) as ws:
            ws.send(json.dumps(msg))
            resp = json.loads(ws.recv(timeout=timeout))
    except Exception as e:
        print(json.dumps({"ok": False, "error": "connection", "detail": f"{type(e).__name__}: {e}"}, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    sys.exit(0 if resp.get("ok") else 1)


if __name__ == "__main__":
    main()
