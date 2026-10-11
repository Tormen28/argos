#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_gut.py — UN COMANDO para correr la suite GDScript (GUT) de ARGOS en headless.

Sincroniza la suite del workspace, asegura el addon GUT + ARGOS en el proyecto de
prueba, importa recursos y ejecuta GUT. Devuelve el exit code de GUT (0 = verde).

Uso:
  python tools/argos_gut.py
  python tools/argos_gut.py --project scratch/argos-test1 --suite tests/gd
  python tools/argos_gut.py --gut-src C:/tmp/gut-9.7.1/addons/gut   # copia GUT si falta

Requisito (una vez): GUT 9.x presente en <project>/addons/gut. Si falta:
  git clone --depth 1 --branch v9.7.1 https://github.com/bitwes/Gut.git C:/tmp/gut-9.7.1
  python tools/argos_gut.py --gut-src C:/tmp/gut-9.7.1/addons/gut
"""
import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GODOT = os.environ.get(
    "ARGOS_GODOT",
    os.path.expanduser("~/tools/godot/4.7.2-stable/Godot_v4.7.2-stable_win64_console.exe"),
)
DEFAULT_PROJECT = ROOT / "scratch" / "argos-test1"
DEFAULT_SUITE = ROOT / "tests" / "gd"


def die(msg: str, code: int = 2):
    print("argos_gut: ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def sync_suite(suite: pathlib.Path, project: pathlib.Path) -> pathlib.Path:
    """Copia el workspace <suite> -> <project>/tests/gd (mirror)."""
    dst = project / "tests" / "gd"
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(suite, dst)
    return dst


def ensure_argos(project: pathlib.Path) -> None:
    """Copia el addon ARGOS fuente + habilita el plugin (idempotente)."""
    installer = ROOT / "tools" / "argos_install.py"
    r = subprocess.run([sys.executable, str(installer), "--project", str(project)],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        die("argos_install.py fallo:\n" + (r.stdout or "") + (r.stderr or ""), r.returncode)
    print("[argos_gut] addon ARGOS sincronizado -> " + str(project / "addons" / "argos"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=str(DEFAULT_PROJECT))
    ap.add_argument("--suite", default=str(DEFAULT_SUITE))
    ap.add_argument("--godot", default=GODOT)
    ap.add_argument("--gut-src", default="", help="copia addons/gut desde aqui si falta")
    args = ap.parse_args()

    project = pathlib.Path(args.project).resolve()
    suite = pathlib.Path(args.suite).resolve()
    if not (project / "project.godot").exists():
        die("no es proyecto Godot (falta project.godot): %s" % project)
    if not suite.is_dir():
        die("suite no encontrada: %s" % suite)
    if not pathlib.Path(args.godot).exists():
        die("Godot no encontrado: %s (usa --godot o ARGOS_GODOT)" % args.godot)

    gut = project / "addons" / "gut"
    if not (gut / "gut_cmdln.gd").exists():
        if args.gut_src and pathlib.Path(args.gut_src).is_dir():
            shutil.copytree(args.gut_src, gut, dirs_exist_ok=True)
            print("[argos_gut] GUT copiado desde " + args.gut_src)
        else:
            die("falta GUT en %s. Instala GUT 9.x (ver docstring) o usa --gut-src." % gut)

    ensure_argos(project)
    sync_suite(suite, project)

    # GUT necesita sus class_names importados (una vez por proyecto / tras cambios).
    # LOW-4(a): el fallo de import NO se ignora (antes se descartaba su exit).
    imp = [args.godot, "--headless", "--path", str(project), "--import", "--quit"]
    print("[argos_gut] import: " + " ".join(imp))
    ir = subprocess.run(imp, capture_output=True, text=True, errors="replace")
    if ir.returncode != 0:
        die("import de Godot fallo (exit %d):\n%s"
            % (ir.returncode, ((ir.stdout or "") + (ir.stderr or ""))[-2000:]), ir.returncode)

    cmd = [args.godot, "--headless", "--path", str(project),
           "-s", "res://addons/gut/gut_cmdln.gd",
           "-gdir=res://tests/gd", "-gexit", "-glog=1"]
    print("[argos_gut] GUT: " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    print(out, end="" if out.endswith("\n") else "\n")

    # LOW-4(b): un exit 0 con 0 tests = suite desaparecida != verde.
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    m = re.search(r"(?m)^Tests\s+(\d+)\s*$", clean)
    tests = int(m.group(1)) if m else 0
    # B3.1: un script de test que NO parsea se omite silenciosamente y GUT puede
    # salir 0 con los demas verdes -> falso verde. Exigimos carga limpia.
    load_fail = ("Parse Error" in clean) or ("Failed to load script" in clean)
    if r.returncode == 0 and (tests <= 0 or load_fail):
        why = "0 tests ejecutados (suite ausente o no recolectada)" if tests <= 0 \
            else "hubo scripts de test que no cargaron (Parse Error / Failed to load)"
        print("[argos_gut] ROJO: " + why, file=sys.stderr)
        sys.exit(1)
    if r.returncode == 0:
        print("[argos_gut] VERDE (exit 0, %d tests)" % tests)
    else:
        print("[argos_gut] ROJO (exit %d, %d tests)" % (r.returncode, tests), file=sys.stderr)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
