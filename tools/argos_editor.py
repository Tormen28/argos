#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_editor.py — ciclo de vida del editor Godot para ARGOS.

Uso:
  python argos_editor.py start <proyecto> [--windowed] [--wait N]
  python argos_editor.py stop  <proyecto>
  python argos_editor.py status <proyecto>

Estado en <proyecto>/.argos/ : editor.pid, editor.log, port, token

Start/stop works on Windows (tasklist/taskkill) and on Linux/macOS
(os.kill / process groups). Set ARGOS_GODOT to the editor binary; if it is
unset, Windows keeps the historical default path and other platforms use
`godot` on PATH.
"""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time


def default_godot_executable() -> str:
    """Binary used when ARGOS_GODOT is unset."""
    if os.name == "nt":
        return os.path.expanduser(
            "~/tools/godot/4.7.2-stable/Godot_v4.7.2-stable_win64_console.exe")
    return "godot"


GODOT = os.environ.get("ARGOS_GODOT", default_godot_executable())

def _base(project: str) -> pathlib.Path:
    return pathlib.Path(project) / ".argos"

def _alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, errors="replace", timeout=15,
            ).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # A zombie still accepts signal 0. Treat it as gone so stop does not wait.
    stat = pathlib.Path("/proc") / str(pid) / "stat"
    try:
        raw = stat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    mark = raw.rfind(")")
    if mark != -1 and mark + 2 < len(raw) and raw[mark + 2] == "Z":
        return False
    return True


def _stop_pid(pid: int) -> None:
    """Stop a process started by ARGOS. Never signals this interpreter's group."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, text=True, errors="replace",
        )
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    except OSError:
        pgid = pid
    own_group = pgid == os.getpgrp()

    def _signal(sig) -> None:
        try:
            if own_group or pgid != pid:
                os.kill(pid, sig)
            else:
                os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    _signal(signal.SIGTERM)
    deadline = time.time() + 3.0
    while time.time() < deadline and _alive(pid):
        time.sleep(0.1)
    if _alive(pid):
        _signal(signal.SIGKILL)

def _ping(project: str) -> bool:
    try:
        from websockets.sync.client import connect
        base = _base(project)
        port = 9080
        if (base / "port").exists():
            port = int((base / "port").read_text(encoding="utf-8").strip())
        token = ""
        if (base / "token").exists():
            token = (base / "token").read_text(encoding="utf-8").strip()
        with connect(f"ws://127.0.0.1:{port}", open_timeout=3, close_timeout=1) as ws:
            ws.send(json.dumps({"action": "ping", "id": 1, "token": token}))
            r = json.loads(ws.recv(timeout=5))
            return bool(r.get("ok"))
    except Exception:
        return False

def cmd_start(args) -> int:
    proj = args.project
    base = _base(proj)
    base.mkdir(exist_ok=True)
    pidf = base / "editor.pid"
    if pidf.exists():
        try:
            pid = int(pidf.read_text(encoding="utf-8").strip() or 0)
        except Exception:
            pid = 0
        if _alive(pid):
            print(json.dumps({"started": False, "reason": "ya corriendo", "pid": pid}))
            return 1
    logf = open(base / "editor.log", "ab")
    cmd = [GODOT, "--editor", "--path", str(proj)]
    if not args.windowed:
        cmd.insert(1, "--headless")
    popen_kwargs = {
        "stdout": logf,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        p = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError:
        logf.close()
        print(json.dumps({
            "started": False,
            "reason": "Godot executable not found. Set ARGOS_GODOT to the editor binary.",
            "godot": GODOT,
        }))
        return 1
    pidf.write_text(str(p.pid), encoding="utf-8")
    info = {"started": True, "pid": p.pid, "windowed": bool(args.windowed)}
    wait = max(0, int(args.wait))
    if wait > 0:
        t0 = time.time()
        while time.time() - t0 < wait:
            if _ping(proj):
                info["ping_ok_s"] = round(time.time() - t0, 1)
                print(json.dumps(info))
                return 0
            time.sleep(1.5)
        info["warning"] = "sin ping dentro de la espera"
    print(json.dumps(info))
    return 0

def cmd_stop(args) -> int:
    base = _base(args.project)
    pidf = base / "editor.pid"
    if not pidf.exists():
        print(json.dumps({"stopped": False, "reason": "sin editor.pid"}))
        return 1
    try:
        pid = int(pidf.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        pid = 0
    killed = False
    if _alive(pid):
        _stop_pid(pid)
        killed = True
    pidf.unlink(missing_ok=True)
    print(json.dumps({"stopped": True, "pid": pid, "killed": killed}))
    return 0

def cmd_status(args) -> int:
    base = _base(args.project)
    pidf = base / "editor.pid"
    pid = 0
    if pidf.exists():
        try:
            pid = int(pidf.read_text(encoding="utf-8").strip() or 0)
        except Exception:
            pid = 0
    port = None
    if (base / "port").exists():
        try:
            port = int((base / "port").read_text(encoding="utf-8").strip())
        except Exception:
            port = None
    out = {
        "running": _alive(pid),
        "pid": pid,
        "port": port,
        "ping": _ping(args.project),
        "log": str(base / "editor.log"),
    }
    print(json.dumps(out))
    return 0 if out["running"] else 1

def main():
    ap = argparse.ArgumentParser(description="ARGOS editor lifecycle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("project")
    s.add_argument("--windowed", action="store_true")
    s.add_argument("--wait", type=int, default=30)
    st = sub.add_parser("stop")
    st.add_argument("project")
    sg = sub.add_parser("status")
    sg.add_argument("project")
    args = ap.parse_args()
    if args.cmd == "start":
        sys.exit(cmd_start(args))
    if args.cmd == "stop":
        sys.exit(cmd_stop(args))
    if args.cmd == "status":
        sys.exit(cmd_status(args))

if __name__ == "__main__":
    main()
