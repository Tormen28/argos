#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_argos_errors.py — Parser de errores GDScript estructurado (BLOQUE A-3).

Fixtures tomadas de capturas REALES de Godot 4.7.2.stable en esta máquina:
  F1 — parse/sintaxis (`--check-only -s`, exit 1, todo en STDERR).
  F2 — runtime de escena (`--quit-after`, EXIT=0 pese al error).
  F3 — runtime `-s` (SceneTree script).

Verifica además la regla fail-loud: si hay indicios de error (`SCRIPT ERROR` /
`Parse Error`) y NO se reconoce ningún bloque, `unparsed_raw` debe poblarse —
nunca `errors: []` silencioso (un parser roto que devuelve [] es PEOR que no tenerlo).

Ejecutar (stdlib puro, sin dependencias):
  python tools/test_argos_errors.py
"""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import argos_errors  # noqa: E402


# ------------------------------ fixtures reales ------------------------------

F1 = (
    'SCRIPT ERROR: Parse Error: Expected expression for variable initial value after "=".\n'
    '   at: GDScript::reload (res://__a3probe_syntax.gd:5)\n'
    'ERROR: Failed to load script "res://__a3probe_syntax.gd" with error "Parse error".\n'
    '   at: load (modules/gdscript/gdscript_resource_format.cpp:46)\n'
)

F2 = (
    "SCRIPT ERROR: Out of bounds get index '99' (on base: 'Array')\n"
    '   at: _ready (res://__a3probe_runtime.gd:6)\n'
    '   GDScript backtrace (most recent call first):\n'
    '       [0] _ready (res://__a3probe_runtime.gd:6)\n'
)

F3 = (
    "SCRIPT ERROR: Invalid access to property or key 'clave_inexistente' "
    "on a base object of type 'Dictionary'.\n"
    '   at: _initialize (res://__a3probe_rt.gd:6)\n'
    '   GDScript backtrace (most recent call first):\n'
    '       [0] _initialize (res://__a3probe_rt.gd:6)\n'
)

# Tipo/identificador: dos bloques apuntan a la MISMA línea (5) -> se agrupan.
F_TYPE = (
    'SCRIPT ERROR: Parse Error: Cannot assign a value of type "String" as "int".\n'
    '   at: GDScript::reload (res://__a3probe_type.gd:5)\n'
    'SCRIPT ERROR: Parse Error: Cannot assign a value of type String to variable "x" '
    'with specified type int.\n'
    '   at: GDScript::reload (res://__a3probe_type.gd:5)\n'
    'SCRIPT ERROR: Parse Error: Identifier "in_declarado" not declared in the current scope.\n'
    '   at: GDScript::reload (res://__a3probe_type.gd:6)\n'
)

CLEAN = (
    "Godot Engine v4.7.2.stable.official.ed1daf0bf - https://godotengine.org\n\n"
)

# SCRIPT ERROR con formato irreconocible (sin línea `at: ... (res://...:N)`).
F_UNKNOWN = (
    "SCRIPT ERROR: formato desconocido sin linea de localizacion\n"
    "algo de ruido posterior\n"
)


class ParseFixtureTest(unittest.TestCase):

    def test_f1_parse_error(self):
        out = argos_errors.parse_godot_output(F1)
        self.assertIsNone(out["unparsed_raw"])
        self.assertEqual(len(out["errors"]), 1)
        e = out["errors"][0]
        self.assertEqual(e["file"], "res://__a3probe_syntax.gd")
        self.assertEqual(e["line"], 5)
        self.assertIsNone(e["col"])            # el formato no trae columna: no inventar
        self.assertEqual(e["kind"], "parse")
        self.assertEqual(e["severity"], "error")
        self.assertEqual(e["function"], "GDScript::reload")
        self.assertEqual(e["message"],
                         'Expected expression for variable initial value after "=".')

    def test_f2_runtime_escena(self):
        out = argos_errors.parse_godot_output(F2)
        self.assertIsNone(out["unparsed_raw"])
        self.assertEqual(len(out["errors"]), 1)
        e = out["errors"][0]
        self.assertEqual(e["file"], "res://__a3probe_runtime.gd")
        self.assertEqual(e["line"], 6)
        self.assertEqual(e["kind"], "runtime")
        self.assertEqual(e["function"], "_ready")
        self.assertIn("Out of bounds", e["message"])

    def test_f3_runtime_scene_tree(self):
        out = argos_errors.parse_godot_output(F3)
        self.assertIsNone(out["unparsed_raw"])
        self.assertEqual(len(out["errors"]), 1)
        e = out["errors"][0]
        self.assertEqual(e["file"], "res://__a3probe_rt.gd")
        self.assertEqual(e["line"], 6)
        self.assertEqual(e["kind"], "runtime")
        self.assertEqual(e["function"], "_initialize")
        self.assertIn("clave_inexistente", e["message"])

    def test_agrupacion_por_file_line(self):
        out = argos_errors.parse_godot_output(F_TYPE)
        self.assertIsNone(out["unparsed_raw"])
        # 3 bloques -> 2 entradas (líneas 5 y 6); los dos de la línea 5 se agrupan
        self.assertEqual(len(out["errors"]), 2, out["errors"])
        por_linea = {e["line"]: e for e in out["errors"]}
        self.assertEqual(set(por_linea), {5, 6})
        self.assertIn("Cannot assign a value of type", por_linea[5]["message"])
        # el mensaje agrupado conserva AMBOS mensajes de la línea
        self.assertIn("with specified type int", por_linea[5]["message"])
        self.assertIn("in_declarado", por_linea[6]["message"])


class FailLoudTest(unittest.TestCase):

    def test_texto_limpio(self):
        out = argos_errors.parse_godot_output(CLEAN)
        self.assertEqual(out["errors"], [])
        self.assertIsNone(out["unparsed_raw"])

    def test_script_error_irreconocible_es_fail_loud(self):
        out = argos_errors.parse_godot_output(F_UNKNOWN)
        self.assertEqual(out["errors"], [])
        self.assertIsNotNone(out["unparsed_raw"])
        self.assertIn("formato desconocido", out["unparsed_raw"])

    def test_parse_error_suelto_es_fail_loud(self):
        out = argos_errors.parse_godot_output("Parse Error: algo no contemplado\n")
        self.assertEqual(out["errors"], [])
        self.assertIsNotNone(out["unparsed_raw"])
        self.assertIn("algo no contemplado", out["unparsed_raw"])

    def test_reconocido_mas_desconocido_expone_ambos(self):
        texto = F1 + F_UNKNOWN
        out = argos_errors.parse_godot_output(texto)
        self.assertEqual(len(out["errors"]), 1)       # el conocido no se pierde
        self.assertIsNotNone(out["unparsed_raw"])     # el desconocido se hace constar

    def test_entrada_vacia(self):
        out = argos_errors.parse_godot_output("")
        self.assertEqual(out["errors"], [])
        self.assertIsNone(out["unparsed_raw"])


class EsquemaTest(unittest.TestCase):

    def test_campos_exactos_del_esquema(self):
        e = argos_errors.parse_godot_output(F1)["errors"][0]
        self.assertEqual(
            sorted(e.keys()),
            sorted(["file", "line", "col", "kind", "severity", "function", "message"]),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
