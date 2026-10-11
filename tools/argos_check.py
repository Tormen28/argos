#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_check.py — Valida scripts GDScript con `godot --check-only` (sin ejecutar).

BLOQUE A-3: además de ok/no-ok, devuelve errores ESTRUCTURADOS (archivo:línea +
mensaje) parseados por `argos_errors`, para autocorrección precisa.

Uso:
  python argos_check.py <proyecto> res://ruta/script.gd [más...]
  python argos_check.py <proyecto> --all     # todos los .gd (excluye addons/ y .godot/)

Salida: JSON {"ok": bool, "resultados": {target: {ok, exit, errors, unparsed_raw, salida?}}}.
`ok` de cada archivo = (exit==0) Y sin errores Y sin salida sin parsear (fail-loud).
exit 0 si todo OK.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import argos_errors  # noqa: E402

GODOT = os.environ.get(
    "ARGOS_GODOT",
    os.path.expanduser("~/tools/godot/4.7.2-stable/Godot_v4.7.2-stable_win64_console.exe"),
)


def collect_targets(project: str, targets, all_flag: bool):
    """Lista de objetivos `res://...gd`: los dados + (si all) todos los .gd del proyecto."""
    out = list(targets or [])
    if all_flag:
        root = pathlib.Path(project)
        for p in sorted(root.rglob("*.gd")):
            rel = p.relative_to(root).as_posix()
            if rel.startswith("addons/") or rel.startswith(".godot/"):
                continue
            t = "res://" + rel
            if t not in out:
                out.append(t)
    return out


def check_one(proj: str, target: str) -> dict:
    """Ejecuta `--check-only` sobre un target y devuelve el resultado estructurado."""
    cmd = [GODOT, "--headless", "--path", proj, "--check-only", "-s", target]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": -1, "errors": [], "unparsed_raw": None, "salida": "TIMEOUT"}
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    parsed = argos_errors.parse_godot_output(out)
    errors = parsed["errors"]
    unparsed = parsed["unparsed_raw"]
    ok = (r.returncode == 0) and not errors and not unparsed
    entry = {"ok": ok, "exit": r.returncode, "errors": errors, "unparsed_raw": unparsed}
    if out:
        entry["salida"] = out[-2000:]
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    targets = collect_targets(args.project, args.targets, args.all)
    if not targets:
        print(json.dumps({"error": "sin objetivos (pasa archivos o --all)"}, indent=2))
        sys.exit(2)

    results = {}
    allok = True
    for t in targets:
        entry = check_one(args.project, t)
        results[t] = entry
        allok = allok and entry["ok"]
    print(json.dumps({"ok": allok, "resultados": results}, ensure_ascii=False, indent=2))
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
