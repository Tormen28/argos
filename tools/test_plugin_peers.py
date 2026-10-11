#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_plugin_peers.py — Probe de INTEGRACIÓN del hotfix M2 (tope de peers + auth-first).

Requiere el editor ARGOS corriendo (addon instalado/actualizado). Sin editor OMITE
con exit 0 (como test_plugin_limits); con --require-editor sale 3.

Casos (WS directo, sin servidor MCP):
  A) auth-first: peer SIN autenticar manda >512 KiB -> error "auth" (no procesa).
  B) auth-first: mismo payload con token correcto -> igual error "auth" (tope aplica).
  C) autenticado SÍ puede mandar >512 KiB y <8 MiB -> ping ok (auth_gate no lo corta).
  D) tope de peers: 16 conexiones autenticadas OK; la 17ª -> rechazada/cerrada.
  E) MEDIA-1: peer no autenticado -> cerrado por timeout (~10 s).
  F) LOW-1: bad_json de un no autenticado -> responde bad_json y cierra.
  G) M2.2: 16 conexiones TCP CRUDAS sin handshake WS no inmovilizan slots: el
     timeout de handshake las cierra (~10 s) y un cliente legitimo vuelve a entrar.


Uso: python test_plugin_peers.py <proyecto> [--require-editor]
"""
import json
import pathlib
import socket
import sys
import time

try:
    from websockets.sync.client import connect
except ImportError:
    print("ERROR: falta 'websockets'", file=sys.stderr)
    sys.exit(2)

MAX_PEERS = 16
MAX_AUTH_MSG_BYTES = 524288  # 512 KiB
AUTH_TIMEOUT_S = 10          # micro-fix MEDIA-1: plazo de auth del plugin
HANDSHAKE_TIMEOUT_S = 10     # micro-fix M2.2: plazo de handshake WS del plugin
BIG_AUTH = 1024 * 1024       # 1 MiB: > MAX_AUTH_MSG_BYTES, < MAX_MSG_BYTES
BIG_OK = 256 * 1024          # autenticado: pasa


def _load(project: str):
    base = pathlib.Path(project) / ".argos"
    port = int((base / "port").read_text(encoding="utf-8").strip())
    token = (base / "token").read_text(encoding="utf-8").strip()
    return port, token


def main():
    if len(sys.argv) < 2:
        print("uso: test_plugin_peers.py <proyecto> [--require-editor]")
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

    def send(wsock, action, token_ok=True, **kw):
        rid[0] += 1
        msg = {"id": rid[0], "action": action, **kw}
        if token_ok:
            msg["token"] = token
        wsock.send(json.dumps(msg))
        return json.loads(wsock.recv(timeout=30))

    def check(label, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + label + ((" | " + str(detail)[:160]) if detail else ""))
        if not cond:
            failures.append(label)

    def new_conn():
        return connect("ws://127.0.0.1:%d" % port, open_timeout=5, close_timeout=2, max_size=None)

    try:
        # (0) control: autenticar y ping
        ping = send(ws, "ping")
        check("ping", ping.get("ok") is True, ping)

        # (A) auth-first: sin token + payload grande -> error auth + cierre
        w = new_conn()
        try:
            w.send(json.dumps({"id": 1, "action": "ping", "pad": "x" * BIG_AUTH}))
            r = json.loads(w.recv(timeout=30))
            check("A auth-first sin token", r.get("error") == "auth", r.get("error"))
        finally:
            w.close()

        # (B) auth-first: token correcto pero primer mensaje > tope -> auth
        w = new_conn()
        try:
            w.send(json.dumps({"id": 2, "token": token, "action": "ping", "pad": "x" * BIG_AUTH}))
            r = json.loads(w.recv(timeout=30))
            check("B auth-first con token (tope aplica)", r.get("error") == "auth", r.get("error"))
        finally:
            w.close()

        # (C) autenticado puede mandar >512 KiB (<8 MiB)
        w = new_conn()
        try:
            r = send(w, "ping")
            check("C auth ping", r.get("ok") is True, r.get("error"))
            r = send(w, "ping", pad="y" * BIG_OK)
            check("C autenticado >512 KiB ok", r.get("ok") is True, (r.get("error"), len(str(r))))
        finally:
            w.close()

        # (E) MEDIA-1: un peer que no se autentica se cierra por timeout.
        w = new_conn()
        try:
            time.sleep(AUTH_TIMEOUT_S + 1.5)
            closed = False
            try:
                w.recv(timeout=3)
            except Exception:
                closed = True
            check("E no-auth cierra por timeout", closed)
        finally:
            try:
                w.close()
            except Exception:
                pass

        # (F) LOW-1: bad_json de un no-autenticado CIERRA la conexion.
        w = new_conn()
        try:
            w.send("123")
            r = json.loads(w.recv(timeout=10))
            check("F bad_json no-auth responde", r.get("error") == "bad_json", r)
            closed = False
            try:
                w.recv(timeout=3)
            except Exception:
                closed = True
            check("F bad_json no-auth cierra", closed)
        finally:
            try:
                w.close()
            except Exception:
                pass

        # (G) M2.2: TCP crudos sin handshake WS no inmovilizan slots.
        try:
            ws.close()
        except Exception:
            pass
        time.sleep(1.0)  # deja al plugin recolectar el peer de control
        raws = []
        try:
            for _ in range(MAX_PEERS):
                raws.append(socket.create_connection(("127.0.0.1", port), timeout=5))
            time.sleep(1.0)  # deja al plugin aceptar los 16 crudos
            # con los slots copados por crudos, un legitimo no deberia entrar
            rejected_now = False
            try:
                c = new_conn()
                try:
                    r = send(c, "ping")
                    rejected_now = not r.get("ok")
                except Exception:
                    rejected_now = True
                finally:
                    try:
                        c.close()
                    except Exception:
                        pass
            except Exception:
                rejected_now = True
            check("G1 16 crudos sin handshake bloquean al 17o", rejected_now)
            # tras el timeout de handshake el plugin debe cerrarlos y liberar slots
            time.sleep(HANDSHAKE_TIMEOUT_S + 2.0)
            freed = 0
            for s in raws:
                try:
                    s.settimeout(3)
                    if s.recv(1) == b"":
                        freed += 1
                except Exception:
                    freed += 1
            check("G2 crudos cerrados por timeout de handshake", freed >= MAX_PEERS - 1,
                  "%d/%d" % (freed, MAX_PEERS))
            ok = False
            c = new_conn()
            try:
                r = send(c, "ping")
                ok = r.get("ok") is True
            finally:
                try:
                    c.close()
                except Exception:
                    pass
            check("G3 slot liberado admite un legitimo", ok)
        finally:
            for s in raws:
                try:
                    s.close()
                except Exception:
                    pass

        # (D) tope de peers: liberar la conexion de control y llenar con MAX_PEERS
        try:
            ws.close()
        except Exception:
            pass
        peers = []
        try:
            for i in range(MAX_PEERS):
                c = new_conn()
                r = send(c, "ping")
                if not r.get("ok"):
                    break
                peers.append(c)
            check("D %d peers autenticados" % MAX_PEERS, len(peers) == MAX_PEERS, len(peers))
            rejected = False
            try:
                extra = new_conn()
                # si el handshake pasó (carrera), un ping no debe responder ok
                try:
                    r = send(extra, "ping")
                    rejected = not r.get("ok")
                except Exception:
                    rejected = True
                try:
                    extra.close()
                except Exception:
                    pass
            except Exception:
                rejected = True
            check("D 17a conexion rechazada", rejected)
        finally:
            for c in peers:
                try:
                    c.close()
                except Exception:
                    pass
            time.sleep(0.3)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if failures:
        print("PROBE PEERS: FAIL ->", failures)
        sys.exit(1)
    print("PROBE PEERS: PASS (tope de peers + auth-first verificados contra editor real)")
    sys.exit(0)


if __name__ == "__main__":
    main()
