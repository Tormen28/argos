#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_install.py — Instala el addon ARGOS en un proyecto Godot y lo habilita.

Uso:
  python argos_install.py --project <dir-proyecto> [--src <dir addon>]

- Copia el addon a <proyecto>/addons/argos/
- Habilita el plugin en project.godot ([editor_plugins])
- Asegura .gitignore con /.argos/ (lo crea si no existe)
"""
import argparse
import pathlib
import re
import shutil
import sys

def addon_default_src() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "argos" / "addons" / "argos"

def enable_plugin(pg: pathlib.Path) -> str:
    text = pg.read_text(encoding="utf-8")
    entry = '"res://addons/argos/plugin.cfg"'
    if "addons/argos/plugin.cfg" in text:
        return "ya habilitado"
    m = re.search(r"\[editor_plugins\](.*?)(?=\n\[|\Z)", text, re.S)
    if not m:
        text = text.rstrip() + "\n\n[editor_plugins]\nenabled=PackedStringArray(%s)\n" % entry
        pg.write_text(text, encoding="utf-8", newline="\n")
        return "seccion [editor_plugins] creada"
    sec = m.group(1)
    def repl(mm):
        inside = mm.group(1).strip()
        if not inside:
            return "enabled=PackedStringArray(%s)" % entry
        return "enabled=PackedStringArray(%s, %s)" % (entry, inside)
    if "enabled=PackedStringArray(" in sec:
        newsec = re.sub(r"enabled=PackedStringArray\(([^)]*)\)", repl, sec, count=1)
    else:
        newsec = sec.rstrip() + "\nenabled=PackedStringArray(%s)\n" % entry
    text = text[:m.start(1)] + newsec + text[m.end(1):]
    pg.write_text(text, encoding="utf-8", newline="\n")
    return "entrada anadida"

def enable_autoload(pg: pathlib.Path) -> str:
    text = pg.read_text(encoding="utf-8")
    entry = 'ArgosRuntime="*res://addons/argos/argos_runtime.gd"'
    if "argos_runtime.gd" in text:
        return "ya registrado"
    m = re.search(r"\[autoload\](.*?)(?=\n\[|\Z)", text, re.S)
    if not m:
        text = text.rstrip() + "\n\n[autoload]\n%s\n" % entry
        pg.write_text(text, encoding="utf-8", newline="\n")
        return "seccion [autoload] creada"
    sec = m.group(1)
    newsec = sec.rstrip() + "\n%s\n" % entry
    text = text[:m.start(1)] + newsec + text[m.end(1):]
    pg.write_text(text, encoding="utf-8", newline="\n")
    return "entrada anadida"

def ensure_gitignore(proj: pathlib.Path) -> str:
    gi = proj / ".gitignore"
    if gi.exists():
        t = gi.read_text(encoding="utf-8")
        if ".argos/" in t:
            return "ya listado"
        gi.write_text(t.rstrip() + "\n/.argos/\n", encoding="utf-8")
        return "agregado"
    gi.write_text("# estado local de herramientas (no versionar)\n/.argos/\n", encoding="utf-8")
    return "creado"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--src", default=str(addon_default_src()))
    args = ap.parse_args()
    proj = pathlib.Path(args.project)
    src = pathlib.Path(args.src)
    if not (proj / "project.godot").exists():
        print(f"ERROR: {proj} no es proyecto Godot (falta project.godot)")
        sys.exit(1)
    if not (src / "plugin.cfg").exists():
        print(f"ERROR: addon fuente no encontrado en {src}")
        sys.exit(1)
    dst = proj / "addons" / "argos"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    print(f"addon copiado -> {dst}")
    print("estado plugin:", enable_plugin(proj / "project.godot"))
    print("autoload runtime:", enable_autoload(proj / "project.godot"))
    print(".gitignore:", ensure_gitignore(proj))
    print("LISTO.")

if __name__ == "__main__":
    main()
