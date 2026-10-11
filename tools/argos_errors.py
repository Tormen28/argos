#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_errors.py — Parser de errores GDScript de Godot 4.x a estructura JSON.

BLOQUE A-3. Convierte la salida de texto de Godot (`--check-only`, runtime) en
errores estructurados (archivo:línea + mensaje) para autocorrección precisa.

Formatos reales soportados (capturados en Godot 4.7.2.stable, ver diseño A-3):

  F1 · parse:      SCRIPT ERROR: Parse Error: <msg>
                       at: GDScript::reload (res://x.gd:5)
  F2/F3 · runtime: SCRIPT ERROR: <msg>
                       at: <fn> (res://x.gd:6)

Reglas:
  - Se recorren bloques `SCRIPT ERROR: [Parse Error: ]<msg>` + su línea de
    localización `at: <fn> (res://<file>:<line>)`. El ruido posterior
    (`ERROR: Failed to load script ...`) se descarta.
  - Errores que apuntan al mismo (file, line) se AGRUPAN en una entrada.
  - `kind`: "parse" si el bloque trae el prefijo `Parse Error:` o su `at:` es
    `GDScript::reload`; "runtime" en otro caso.
  - `col` siempre None: el formato no trae columna — no se inventa.
  - FAIL-LOUD: si el texto contiene indicios de error (`SCRIPT ERROR` / `Parse
    Error`) y hay líneas sospechosas sin reconocer, se devuelven en `unparsed_raw`.
    Jamás se devuelve `errors: []` silenciosamente ante un fallo.
"""
import re

# `SCRIPT ERROR: [Parse Error: ]<mensaje...>`
_HEADER_RE = re.compile(r"^SCRIPT ERROR:\s*(?P<pe>Parse Error:\s*)?(?P<msg>.*)$")

# `   at: <fn> (res://<file>:<line>)`  (solo ubicaciones del proyecto)
_LOC_RE = re.compile(r"^\s+at:\s*(?P<fn>[^\s(]+)\s+\(res://(?P<file>.+?):(?P<line>\d+)\)\s*$")


def _add_error(errors: list, file: str, line: int, kind: str, fn: str, msg: str) -> None:
    """Añade/agrupa un error por (file, line), conservando el orden de aparición."""
    for e in errors:
        if e["file"] == file and e["line"] == line:
            if msg and msg not in e["message"]:
                e["message"] = (e["message"] + " | " + msg) if e["message"] else msg
            if kind == "parse":
                e["kind"] = "parse"  # un mensaje de parse manda sobre runtime
            return
    errors.append({
        "file": file,
        "line": line,
        "col": None,
        "kind": kind,
        "severity": "error",
        "function": fn,
        "message": msg,
    })


def _suspicious_lines(lines) -> str:
    """Líneas con indicios de error, para el modo fail-loud."""
    out = [ln for ln in lines if ("SCRIPT ERROR" in ln or "Parse Error" in ln)]
    return "\n".join(out).strip()


def parse_godot_output(text: str) -> dict:
    """Parsea la salida de Godot y devuelve `{"errors": [...], "unparsed_raw": str|None}`.

    `errors`: lista de entradas con el esquema
      {file, line, col, kind, severity, function, message}, agrupadas por (file, line).
    `unparsed_raw`: líneas sospechosas no reconocidas (fail-loud) o None si no hay.
    """
    text = text or ""
    lines = text.splitlines()
    errors = []
    suspicious = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _HEADER_RE.match(line)
        if not m:
            i += 1
            continue
        msg = (m.group("msg") or "").strip()
        is_parse_prefix = m.group("pe") is not None
        # localización: la siguiente línea no vacía debe ser `at: ... (res://...:N)`
        j = i + 1
        while j < n and lines[j].strip() == "":
            j += 1
        lm = _LOC_RE.match(lines[j]) if j < n else None
        if lm:
            file = "res://" + lm.group("file")
            line_no = int(lm.group("line"))
            fn = lm.group("fn")
            kind = "parse" if (is_parse_prefix or fn == "GDScript::reload") else "runtime"
            _add_error(errors, file, line_no, kind, fn, msg)
            i = j + 1
        else:
            # header de error sin localización reconocible -> constancia (fail-loud)
            suspicious.append(line)
            i += 1

    unparsed_raw = "\n".join(suspicious).strip() or None
    if unparsed_raw is None and not errors:
        # ninguna señal reconocida: si el texto menciona un fallo, no lo silenciamos
        if "SCRIPT ERROR" in text or "Parse Error" in text:
            unparsed_raw = _suspicious_lines(lines) or text.strip() or None
    return {"errors": errors, "unparsed_raw": unparsed_raw}


if __name__ == "__main__":
    import json
    import pathlib
    import sys
    if len(sys.argv) > 1:
        data = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    else:
        data = sys.stdin.read()
    print(json.dumps(parse_godot_output(data), ensure_ascii=False, indent=2))
