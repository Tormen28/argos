#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_record_args.py — Validaciones PURAS del runner de grabacion (BLOQUE C1).

Cubre la parte determinista y baratísima de `argos_record.py` (sin lanzar Godot):
slug/nombre, formato, calculo de frames, construccion del comando windowed,
guard anti-headless, ruta de artefactos, timestamp, parseo de ffprobe y la
verificacion de artefactos sobre un directorio temporal.

La grabacion real (proceso Godot + ffprobe sobre archivo real) es integracion y
se verifica a mano segun el diseno §4.

Ejecutar: python tools/test_record_args.py
"""
import datetime
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import argos_record as rec  # noqa: E402


class TestSlug(unittest.TestCase):
    def test_basico(self):
        self.assertEqual(rec.sanitize_slug("Replay Final"), "replay-final")

    def test_limpia_caracteres_invalidos(self):
        self.assertEqual(rec.sanitize_slug("a/b\\c:d*e?f"), "a-b-c-d-e-f")

    def test_colapsa_separadores_repetidos(self):
        self.assertEqual(rec.sanitize_slug("A  --  B"), "a-b")

    def test_sin_puntos_ni_espacios(self):
        self.assertEqual(rec.sanitize_slug("v1.2.3"), "v1-2-3")

    def test_vacio_o_solo_ruido_usa_fallback(self):
        self.assertEqual(rec.sanitize_slug(""), "record")
        self.assertEqual(rec.sanitize_slug("!!!"), "record")
        self.assertEqual(rec.sanitize_slug(None), "record")

    def test_limites_no_terminan_en_separador(self):
        self.assertEqual(rec.sanitize_slug("--hola--"), "hola")


class TestFormato(unittest.TestCase):
    def test_normaliza_a_minusculas_y_sin_punto(self):
        self.assertEqual(rec.normalize_format("OGV"), "ogv")
        self.assertEqual(rec.normalize_format(".AVI"), "avi")
        self.assertEqual(rec.normalize_format("PNG"), "png")

    def test_default_si_vacio(self):
        self.assertEqual(rec.normalize_format(""), "ogv")
        self.assertEqual(rec.normalize_format(None), "ogv")

    def test_rechaza_no_soportado(self):
        for bad in ("mkv", "mp4", "gif", "webm"):
            with self.assertRaises(rec.RecordError):
                rec.normalize_format(bad)


class TestNumeros(unittest.TestCase):
    def test_seconds_valido(self):
        self.assertEqual(rec.validate_seconds(3), 3)
        self.assertEqual(rec.validate_seconds("5"), 5)

    def test_seconds_invalido(self):
        for bad in (0, -1, "0", "abc", 1.5, None):
            with self.assertRaises(rec.RecordError):
                rec.validate_seconds(bad)

    def test_fps_valido(self):
        self.assertEqual(rec.validate_fps(30), 30)
        self.assertEqual(rec.validate_fps("60"), 60)

    def test_fps_invalido(self):
        for bad in (0, -30, 0.5, "x", 500, None):
            with self.assertRaises(rec.RecordError):
                rec.validate_fps(bad)

    def test_frames(self):
        self.assertEqual(rec.frames_for(3, 30), 90)
        self.assertEqual(rec.frames_for(2, 60), 120)


class TestTimestampYArtefacto(unittest.TestCase):
    def test_timestamp_formato(self):
        when = datetime.datetime(2026, 9, 13, 14, 5, 6)
        self.assertEqual(rec.timestamp(when), "20260913-140506")

    def test_artifact_path(self):
        p = rec.artifact_path("/tmp/proj", "replay", "ogv", "20260913-140506")
        self.assertEqual(
            pathlib.Path(p).as_posix(),
            "/tmp/proj/.argos/recordings/replay_20260913-140506.ogv",
        )


class TestComando(unittest.TestCase):
    def test_windowed_incluye_flags_clave(self):
        cmd = rec.build_movie_command(
            godot="godot.exe",
            project="/tmp/proj",
            out_abs="/tmp/proj/.argos/recordings/r_1.ogv",
            fps=30,
            frames=90,
        )
        self.assertEqual(cmd[0], "godot.exe")
        self.assertIn("--path", cmd)
        self.assertIn("--write-movie", cmd)
        self.assertIn("--fixed-fps", cmd)
        self.assertIn("--quit-after", cmd)
        self.assertEqual(cmd[cmd.index("--quit-after") + 1], "90")
        self.assertEqual(cmd[cmd.index("--fixed-fps") + 1], "30")
        # JAMAS headless: la grabacion necesita ventana (crash conocido)
        self.assertNotIn("--headless", cmd)

    def test_escena_va_tras_project(self):
        cmd = rec.build_movie_command(
            godot="godot.exe",
            project="/tmp/proj",
            out_abs="/tmp/out.ogv",
            fps=30,
            frames=60,
            scene="res://main.tscn",
        )
        i = cmd.index("--path")
        self.assertEqual(cmd[i + 1], "/tmp/proj")
        self.assertEqual(cmd[i + 2], "res://main.tscn")

    def test_guard_headless_lanza(self):
        with self.assertRaises(rec.RecordError) as ctx:
            rec.build_movie_command(
                godot="godot.exe", project="/tmp/p", out_abs="/tmp/o.ogv",
                fps=30, frames=60, headless=True,
            )
        self.assertIn("headless", str(ctx.exception).lower())


class TestPlan(unittest.TestCase):
    def test_plan_campos(self):
        when = datetime.datetime(2026, 9, 13, 14, 5, 6)
        plan = rec.plan_record(
            project="/tmp/proj", seconds=3, fps=30, fmt="ogv",
            name="Replay Final", scene=None, godot="godot.exe", now=when,
        )
        self.assertEqual(plan.slug, "replay-final")
        self.assertEqual(plan.frames, 90)
        self.assertEqual(plan.format, "ogv")
        self.assertEqual(
            pathlib.Path(plan.out_path).name, "replay-final_20260913-140506.ogv"
        )
        self.assertNotIn("--headless", plan.command)

    def test_plan_headless_rechazado(self):
        with self.assertRaises(rec.RecordError):
            rec.plan_record(
                project="/tmp/proj", seconds=1, fps=30, fmt="ogv",
                name=None, scene=None, godot="godot.exe", headless=True,
            )


class TestParseArgs(unittest.TestCase):
    def test_minimo(self):
        ns = rec.parse_args(["mi-proyecto", "--seconds", "2"])
        self.assertEqual(ns.project, "mi-proyecto")
        self.assertEqual(ns.seconds, "2")
        self.assertEqual(ns.fps, 30)
        self.assertEqual(ns.format, "ogv")
        self.assertIsNone(ns.name)
        self.assertIsNone(ns.scene)
        self.assertFalse(ns.headless)

    def test_con_escena_y_nombre_y_formato(self):
        ns = rec.parse_args(
            ["proj", "--seconds", "3", "--fps", "60", "--format", "png",
             "--name", "demo", "res://main.tscn"]
        )
        self.assertEqual(ns.scene, "res://main.tscn")
        self.assertEqual(ns.name, "demo")
        self.assertEqual(ns.format, "png")
        self.assertEqual(ns.fps, 60)

    def test_headless_flag_existe(self):
        ns = rec.parse_args(["proj", "--seconds", "1", "--headless"])
        self.assertTrue(ns.headless)


class TestMainScene(unittest.TestCase):
    def test_parsea_main_scene(self):
        text = 'config_version=5\n\n[application]\n\nrun/main_scene="res://main.tscn"\n'
        self.assertEqual(rec.parse_main_scene(text), "res://main.tscn")

    def test_ausente_es_none(self):
        self.assertIsNone(rec.parse_main_scene('[application]\nconfig/name="x"\n'))
        self.assertIsNone(rec.parse_main_scene(""))

    def test_vacio_es_none(self):
        self.assertIsNone(rec.parse_main_scene('run/main_scene=""\n'))

    def test_read_main_scene_desde_disco(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "project.godot"
            p.write_text('[application]\nrun/main_scene="res://main.tscn"\n',
                         encoding="utf-8")
            self.assertEqual(rec.read_main_scene(tmp), "res://main.tscn")

    def test_read_main_scene_sin_archivo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(rec.read_main_scene(tmp))


class TestFfprobeParse(unittest.TestCase):
    def test_parsea_duracion(self):
        self.assertAlmostEqual(rec.parse_ffprobe_duration("2.000000\n"), 2.0)
        self.assertAlmostEqual(rec.parse_ffprobe_duration("2.5"), 2.5)

    def test_no_numerico_es_none(self):
        self.assertIsNone(rec.parse_ffprobe_duration("N/A"))
        self.assertIsNone(rec.parse_ffprobe_duration(""))

    def test_parsea_frames(self):
        self.assertEqual(rec.parse_ffprobe_frames("60\n"), 60)
        self.assertEqual(rec.parse_ffprobe_frames("1"), 1)

    def test_frames_no_numerico_es_none(self):
        self.assertIsNone(rec.parse_ffprobe_frames("N/A"))
        self.assertIsNone(rec.parse_ffprobe_frames(""))
        self.assertIsNone(rec.parse_ffprobe_frames("2.5"))


class TestSizeWarning(unittest.TestCase):
    def test_solo_avi_tiene_tope(self):
        big = rec.AVI_MAX_BYTES + 1
        self.assertIsNotNone(rec.size_warning("avi", big))
        # Otros formatos NO heredan el texto del tope de AVI.
        self.assertIsNone(rec.size_warning("ogv", big))
        self.assertIsNone(rec.size_warning("png", big))

    def test_avi_pequeno_sin_aviso(self):
        self.assertIsNone(rec.size_warning("avi", 1024))



class TestVerify(unittest.TestCase):
    def _plan(self, tmp, fmt):
        when = datetime.datetime(2026, 9, 13, 14, 5, 6)
        return rec.plan_record(
            project=tmp, seconds=2, fps=30, fmt=fmt,
            name="t", scene=None, godot="godot.exe", now=when,
        )

    @staticmethod
    def _touch(plan, n=32):
        pathlib.Path(plan.out_path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(plan.out_path).write_bytes(b"\x00" * n)

    def test_falta_archivo_es_rojo(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "ogv")
            res = rec.verify_recording(plan, exit_code=0, probe_fn=lambda p: 2.0,
                                       frames_fn=lambda p: 60)
            self.assertFalse(res["ok"])

    def test_archivo_valido_es_verde_con_duracion(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "ogv")
            self._touch(plan)
            res = rec.verify_recording(plan, exit_code=0, probe_fn=lambda p: 2.0,
                                       frames_fn=lambda p: 60)
            self.assertTrue(res["ok"])
            self.assertEqual(res["bytes"], 32)
            self.assertAlmostEqual(res["duration_s"], 2.0)
            self.assertEqual(res["frames_decoded"], 60)
            self.assertEqual(res["frames_expected"], 60)
            self.assertEqual(res["warnings"], [])

    def test_exit_no_cero_es_rojo(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "ogv")
            self._touch(plan)
            res = rec.verify_recording(plan, exit_code=1, probe_fn=lambda p: 2.0,
                                       frames_fn=lambda p: 60)
            self.assertFalse(res["ok"])

    def test_ogv_pocos_frames_avisa_sin_fallar(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "ogv")
            self._touch(plan)
            res = rec.verify_recording(plan, exit_code=0, probe_fn=lambda p: 2.0,
                                       frames_fn=lambda p: 1)
            self.assertTrue(res["ok"])  # grabación válida: solo se avisa
            self.assertEqual(res["frames_decoded"], 1)
            self.assertTrue(any("frames decodificables" in w and "esperados" in w
                                for w in res["warnings"]),
                            res["warnings"])

    def test_frames_exactamente_50pct_no_avisa(self):
        # 30 de 60 = exactamente el 50 % -> NO debe avisar (umbral estricto < 50 %).
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "ogv")
            self._touch(plan)
            res = rec.verify_recording(plan, exit_code=0, probe_fn=lambda p: 2.0,
                                       frames_fn=lambda p: 30)
            self.assertFalse(any("frames decodificables" in w for w in res["warnings"]),
                             res["warnings"])

    def test_avi_frames_completos_sin_aviso(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "avi")
            self._touch(plan)
            res = rec.verify_recording(plan, exit_code=0, probe_fn=lambda p: 2.0,
                                       frames_fn=lambda p: 60)
            self.assertEqual(res["frames_decoded"], 60)
            self.assertEqual(res["warnings"], [])

    def test_duracion_difiere_avisa(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "ogv")
            self._touch(plan)
            res = rec.verify_recording(plan, exit_code=0, probe_fn=lambda p: 9.0,
                                       frames_fn=lambda p: 60)
            self.assertTrue(any("difiere" in w for w in res["warnings"]), res["warnings"])

    def test_sin_frames_fn_degrada_sin_fallar(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "ogv")
            self._touch(plan)
            res = rec.verify_recording(plan, exit_code=0, probe_fn=lambda p: 2.0)
            self.assertTrue(res["ok"])
            self.assertIsNone(res["frames_decoded"])
            self.assertTrue(any("frames decodificables" in w for w in res["warnings"]),
                            res["warnings"])

    def test_secuencia_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "png")
            parent = pathlib.Path(plan.out_path).parent
            parent.mkdir(parents=True, exist_ok=True)
            stem = pathlib.Path(plan.out_path).stem
            for i in range(3):
                (parent / ("%s%08d.png" % (stem, i))).write_bytes(b"\x00" * 16)
            res = rec.verify_recording(plan, exit_code=0)
            self.assertTrue(res["ok"])
            self.assertEqual(res["files"], 3)
            self.assertEqual(res["frames_decoded"], 3)
            self.assertEqual(res["bytes"], 48)

    def test_png_ignora_prefijo_parecido(self):
        # El conteo va anclado al prefijo EXACTO: un `<stem>thumb.png` o un archivo
        # con sufijo no numérico NO cuenta; solo `<stem><dígitos>.png`.
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "png")
            parent = pathlib.Path(plan.out_path).parent
            parent.mkdir(parents=True, exist_ok=True)
            stem = pathlib.Path(plan.out_path).stem
            (parent / ("%sthumb.png" % stem)).write_bytes(b"\x00" * 16)
            (parent / ("%s-extra.png" % stem)).write_bytes(b"\x00" * 16)
            (parent / ("%s00000000.png" % stem)).write_bytes(b"\x00" * 16)
            res = rec.verify_recording(plan, exit_code=0)
            self.assertTrue(res["ok"])
            self.assertEqual(res["files"], 1)
            self.assertEqual(res["bytes"], 16)

    def test_png_sin_secuencia_es_rojo(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "png")
            res = rec.verify_recording(plan, exit_code=0)
            self.assertFalse(res["ok"])

    def test_sin_ffprobe_degrada_sin_fallar(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, "ogv")
            self._touch(plan, 8)
            res = rec.verify_recording(plan, exit_code=0, probe_fn=None,
                                       frames_fn=None)
            self.assertTrue(res["ok"])
            self.assertIsNone(res["duration_s"])
            self.assertIsNone(res["frames_decoded"])
            self.assertTrue(res["warnings"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
