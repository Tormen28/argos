#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_record.py — Graba video de N segundos de una escena Godot (Movie Maker).

BLOQUE C1. Patrón `argos_gut.py`: un comando, verificación integrada y salida JSON.
Graba **offline (no en tiempo real)** con `--write-movie` a paso fijo
(`--fixed-fps`) y sale solo con `--quit-after` (frames = seconds * fps).

  ⚠️  LA GRABACIÓN ES WINDOWED, JAMÁS `--headless`.
  `--headless` (rendering dummy) hace crashear a Godot 4.7.2 con `signal 11` y
  deja un archivo de 0 B — reproducido incluso en un proyecto mínimo. Movie Maker
  necesita un device de render real + display server (ventana). Por eso se usa el
  ejecutable de CONSOLA con ventana; `--quit-after` cierra la ventana sola.
  Este runner rechaza `--headless` con un error explícito.

Formatos nativos (verificado 4.7.2): `.ogv` (Theora, recomendado), `.avi` (MJPEG,
tope 4 GB), `.png` (secuencia lossless). `.mkv` NO existe nativo.

Uso:
  python tools/argos_record.py <proyecto> --seconds 3 [--fps 30]
      [--format ogv|avi|png] [--name slug] [escena-inicial]

  python tools/argos_record.py scratch/argos-test1 --seconds 3 --format ogv
  python tools/argos_record.py scratch/argos-test1 --seconds 2 --format png
  python tools/argos_record.py scratch/argos-test1 --seconds 3 res://main.tscn

Artefactos: `<proyecto>/.argos/recordings/<slug>_<YYYYmmdd-HHMMSS>.<ext>`
(gitignored). Verificación: exit code + archivo(s) existe(n) + tamaño > 0 +
duración vía ffprobe (si ffprobe falta → aviso y se degrada sin fallar).

Honestidad de contenido (ogv, límite CONOCIDO): la duración sale de
`format=duration` (metadata), así que un archivo puede declarar 2 s sin tener
2 s de contenido. Por eso se cuentan TAMBIÉN los frames realmente decodificables
(`ffprobe -count_frames`) y se exponen como `frames_decoded`. En una escena
**estática** el writer OGV de Godot 4.7.2 emite 1 packet con datos + N vacíos
(delta), de modo que FFmpeg decodifica ~1 frame aunque la duración sea correcta;
si eso pasa se emite un WARNING en `warnings[]` (NO se falla: la grabación es
válida). La contrapartida: `-count_frames` decodifica el archivo entero y es más
caro que leer metadata (aceptable en C1, grabaciones de segundos).

Reproducción fuera de FFmpeg: verificado 2026-09-13 en Godot 4.7.2
`VideoStreamPlayer` (consola, windowed): el `.ogv` estático CARGA y
reproduce HASTA EL FINAL sin errores (`stream_length=2.0`, señal `finished`,
textura presente), pero el frame renderizado es el MISMO en todo el video (hash
idéntico en f5/f40/f75) → coherente con 1 frame real en escena estática. VLC no
está instalado en la máquina → no verificado ahí. El error de decodificación
("Broken file, keyframe not correctly marked") es específico de FFmpeg/PyAV y no
implica que Godot no pueda abrirlo.

SIN tool MCP en C1 (la superficie se mantiene en 27 tools).
"""
import argparse
import dataclasses
import datetime
import json
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

FORMATS = ("ogv", "avi", "png")
# El formato AVI (MJPEG) solo está bien formado por debajo de 4 GB.
AVI_MAX_BYTES = 4 * 1024 ** 3

HEADLESS_MSG = (
    "grabación con --headless NO soportada: Godot 4.7.2 crashea con signal 11 y "
    "produce un archivo de 0 B (reproducido incluso en un proyecto mínimo). "
    "Movie Maker necesita device de render real + display server (ventana). "
    "Vuelve a lanzar SIN --headless: se abrirá una ventana y --quit-after la cierra."
)


class RecordError(Exception):
    """Error de validación/uso del runner (no de Godot)."""


@dataclasses.dataclass
class RecordPlan:
    project: str
    scene: str
    seconds: int
    fps: int
    frames: int
    format: str
    ext: str
    slug: str
    stamp: str
    out_path: str
    command: list


# --------------------------------------------------------------------------- #
# Validaciones puras
# --------------------------------------------------------------------------- #
def sanitize_slug(name) -> str:
    """Convierte un nombre libre en un slug seguro para nombre de archivo.

    Solo [a-z0-9-]; cualquier otro carácter (espacios, '/', '.', unicode...) se
    vuelve '-'; se colapsan separadores y se recortan los extremos. Vacío -> 'record'.
    """
    if not name:
        return "record"
    out = []
    for ch in str(name):
        if ch.isascii() and ch.isalnum():
            out.append(ch.lower())
        else:
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-_")
    return slug or "record"


def normalize_format(fmt) -> str:
    """Normaliza `fmt` a uno de FORMATS (default 'ogv'). RecordError si no se soporta."""
    f = (fmt or "ogv").strip().lower().lstrip(".")
    if f not in FORMATS:
        raise RecordError(
            "formato no soportado: %r (usa uno de: %s)" % (fmt, ", ".join(FORMATS))
        )
    return f


def _as_int(value, what: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise RecordError("%s inválido: %r (se espera un entero)" % (what, value))


def validate_seconds(value) -> int:
    n = _as_int(value, "seconds")
    if n < 1:
        raise RecordError("seconds debe ser >= 1 (recibido %r)" % (value,))
    return n


def validate_fps(value) -> int:
    n = _as_int(value, "fps")
    if n < 1 or n > 240:
        raise RecordError("fps fuera de rango 1..240 (recibido %r)" % (value,))
    return n


def frames_for(seconds: int, fps: int) -> int:
    """Frames a renderizar = seconds * fps (lo que consume --quit-after)."""
    return int(seconds) * int(fps)


def timestamp(now=None) -> str:
    return (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")


def artifact_path(project, slug: str, ext: str, stamp: str):
    return pathlib.Path(project) / ".argos" / "recordings" / ("%s_%s.%s" % (slug, stamp, ext))


_MAIN_SCENE_RE = re.compile(r'(?m)^\s*run/main_scene\s*=\s*"([^"]*)"\s*$')


def parse_main_scene(text) -> "str | None":
    """Extrae `run/main_scene` del texto de project.godot. None si ausente o vacío."""
    if not text:
        return None
    m = _MAIN_SCENE_RE.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def read_main_scene(project) -> "str | None":
    """`run/main_scene` del proyecto, o None si no hay project.godot/escena jugable."""
    p = pathlib.Path(project) / "project.godot"
    if not p.is_file():
        return None
    return parse_main_scene(p.read_text(encoding="utf-8", errors="replace"))


def build_movie_command(godot, project, out_abs, fps: int, frames: int,
                        scene=None, headless: bool = False) -> list:
    """Comando Godot WINDOWED de grabación. Rechaza headless (crash conocido)."""
    if headless:
        raise RecordError(HEADLESS_MSG)
    cmd = [str(godot), "--path", str(project)]
    if scene:
        cmd.append(str(scene))
    cmd += [
        "--write-movie", str(out_abs),
        "--fixed-fps", str(fps),
        "--quit-after", str(frames),
    ]
    return cmd


def plan_record(project, seconds, fps=30, fmt="ogv", name=None, scene=None,
                godot=None, now=None, headless: bool = False) -> RecordPlan:
    """Valida y construye el plan completo (puro, sin tocar disco ni lanzar Godot)."""
    if headless:
        raise RecordError(HEADLESS_MSG)
    secs = validate_seconds(seconds)
    fps_i = validate_fps(fps)
    fmt_n = normalize_format(fmt)
    frames = frames_for(secs, fps_i)
    slug = sanitize_slug(name)
    stamp = timestamp(now)
    out = artifact_path(project, slug, fmt_n, stamp)
    command = build_movie_command(
        godot or GODOT, project, out, fps_i, frames, scene=scene
    )
    return RecordPlan(
        project=str(project),
        scene=scene or "",
        seconds=secs,
        fps=fps_i,
        frames=frames,
        format=fmt_n,
        ext=fmt_n,
        slug=slug,
        stamp=stamp,
        out_path=str(out),
        command=command,
    )


# --------------------------------------------------------------------------- #
# Verificación de artefactos
# --------------------------------------------------------------------------- #
def collect_artifacts(plan: RecordPlan) -> list:
    """Archivos producidos: el archivo único, o la secuencia PNG de ESTE run.

    Para png se acota al prefijo EXACTO `stem` + dígitos (Godot numera
    `<basename><8 dígitos>.png`), de modo que no se cuelan archivos vecinos como
    `<stem>-thumb.png` o de otra grabación con el prefijo parecido.
    """
    out = pathlib.Path(plan.out_path)
    if plan.format == "png":
        parent = out.parent
        if not parent.is_dir():
            return []
        pat = re.compile(r"^%s\d+\.png$" % re.escape(out.stem))
        return sorted(p for p in parent.iterdir()
                      if p.is_file() and pat.match(p.name))
    return [out] if out.is_file() else []


def size_warning(fmt: str, total: int):
    """Aviso por tamaño según el formato, o None. Solo AVI (MJPEG) tiene tope 4 GB."""
    if fmt == "avi" and total > AVI_MAX_BYTES:
        return "AVI supera el tope de 4 GB del formato: %d bytes" % total
    return None



def parse_ffprobe_duration(text):
    """Primera línea de `ffprobe` como float; None si no es numérica."""
    if not text or not text.strip():
        return None
    try:
        return float(text.strip().splitlines()[0].strip())
    except ValueError:
        return None


def parse_ffprobe_frames(text):
    """Primera línea de `ffprobe` como int; None si no es numérica."""
    if not text or not text.strip():
        return None
    try:
        return int(text.strip().splitlines()[0].strip())
    except ValueError:
        return None


def ffprobe_duration(path, ffprobe: str = "ffprobe", timeout: int = 30):
    """Duración en segundos de `path` vía ffprobe. None si falla/ausente."""
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return parse_ffprobe_duration(r.stdout)


def ffprobe_frames(path, ffprobe: str = "ffprobe", timeout: int = 120):
    """Frames de video REALMENTE decodificables de `path`. None si falla/ausente.

    Usa `-count_frames` (decodifica el stream entero), a diferencia de la duración
    que sale de metadata. Es la señal fiable de que el archivo tiene contenido.
    """
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-count_frames", "-show_entries", "stream=nb_read_frames",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return parse_ffprobe_frames(r.stdout)



def verify_recording(plan: RecordPlan, exit_code: int, probe_fn=None,
                     frames_fn=None) -> dict:
    """Verifica el resultado de la corrida: existencias, bytes, duración y frames.

    `probe_fn(path)->float|None` mide duración; `frames_fn(path)->int|None` cuenta
    frames decodificables reales. Ambos son opcionales: pasar `None` = esa sonda
    no está disponible y se degrada con aviso (sin fallar). No hay un flag
    separado que pueda contradecir a los callables, así que el estado
    "declarado ausente pero con sonda" es imposible.

    Honestidad ogv: además de la duración (metadata) se cuenta contenido real
    (`frames_fn`). Si se decodifican < 50 % de los frames esperados se avisa;
    NO se falla (la grabación es válida y el caso es normal en escena estática).
    """
    warnings = []
    files = collect_artifacts(plan)
    total = sum(os.path.getsize(f) for f in files)
    ok = (exit_code == 0) and len(files) > 0 and total > 0

    duration = None
    frames_decoded = None
    expected = plan.frames  # seconds * fps

    if plan.format in ("ogv", "avi"):
        if probe_fn is None:
            warnings.append(
                "ffprobe no disponible: duración no verificada (degradado sin fallar)")
        else:
            duration = probe_fn(plan.out_path)
            if duration is None:
                warnings.append("ffprobe no pudo leer la duración de %s" % plan.out_path)
            elif abs(duration - plan.seconds) > 0.5:
                warnings.append(
                    "duración %s s difiere de lo pedido (%d s)" % (duration, plan.seconds)
                )
        if frames_fn is None:
            warnings.append(
                "ffprobe no disponible: frames decodificables no verificados "
                "(degradado sin fallar)")
        else:
            frames_decoded = frames_fn(plan.out_path)
            if frames_decoded is None:
                warnings.append("ffprobe no pudo contar frames de %s" % plan.out_path)
            elif expected and frames_decoded < 0.5 * expected:
                warnings.append(
                    "%s: %d frames decodificables de %d esperados — normal en "
                    "escena estática (delta vacíos); si esperabas gameplay, revisar"
                    % (plan.format, frames_decoded, expected)
                )
    elif plan.format == "png":
        duration = round(len(files) / float(plan.fps), 3) if plan.fps else None
        frames_decoded = len(files)
        if expected and frames_decoded < 0.5 * expected:
            warnings.append(
                "png: %d frames de %d esperados (¿terminó antes --quit-after?)"
                % (frames_decoded, expected)
            )

    sw = size_warning(plan.format, total)
    if sw:
        warnings.append(sw)

    return {
        "ok": ok,
        "exit": exit_code,
        "artifact": plan.out_path,
        "exists": len(files) > 0,
        "files": len(files),
        "bytes": total,
        "duration_s": duration,
        "frames_decoded": frames_decoded,
        "frames_expected": expected,
        "fps": plan.fps,
        "seconds": plan.seconds,
        "format": plan.format,
        "warnings": warnings,
    }



# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="argos_record.py",
        description="Graba N segundos de una escena Godot (Movie Maker, WINDOWED).",
    )
    ap.add_argument("project", help="ruta del proyecto Godot (con project.godot)")
    ap.add_argument("--seconds", required=True, help="segundos a grabar (>=1)")
    ap.add_argument("--fps", type=int, default=30, help="frames por segundo (default 30)")
    ap.add_argument("--format", default="ogv", choices=list(FORMATS),
                    help="formato de salida (default ogv)")
    ap.add_argument("--name", default=None, help="slug del artefacto (default record)")
    ap.add_argument("--godot", default=GODOT, help="ruta del Godot de consola")
    ap.add_argument("--headless", action="store_true",
                    help="NO SOPORTADO: crashea; se rechaza con error claro")
    ap.add_argument("scene", nargs="?", default=None,
                    help="escena inicial global, p.ej. res://main.tscn")
    # parse_intermixed_args permite el posicional `scene` DESPUÉS de las opciones
    # (p.ej. ... --name demo res://main.tscn), como pide la CLI del diseño §3.
    if hasattr(ap, "parse_intermixed_args"):
        return ap.parse_intermixed_args(argv)
    return ap.parse_args(argv)


def die(msg: str, code: int = 2):
    print("argos_record: ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def main(argv=None):
    args = parse_args(argv)
    if args.headless:
        die(HEADLESS_MSG)

    project = pathlib.Path(args.project).resolve()
    if not (project / "project.godot").exists():
        die("no es proyecto Godot (falta project.godot): %s" % project)
    if not pathlib.Path(args.godot).exists():
        die("Godot no encontrado: %s (usa --godot o ARGOS_GODOT)" % args.godot)
    if not args.scene and not read_main_scene(project):
        die("el proyecto no declara escena jugable (run/main_scene en project.godot) "
            "y no se pasó una escena inicial: %s" % project)

    try:
        plan = plan_record(
            project=project, seconds=args.seconds, fps=args.fps, fmt=args.format,
            name=args.name, scene=args.scene, godot=args.godot,
        )
    except RecordError as e:
        die(str(e))

    pathlib.Path(plan.out_path).parent.mkdir(parents=True, exist_ok=True)

    print("[argos_record] %d s @ %d fps (%d frames) formato %s"
          % (plan.seconds, plan.fps, plan.frames, plan.format))
    print("[argos_record] ventana: es lo esperado (Movie Maker requiere display); "
          "--quit-after la cierra.")
    print("[argos_record] cmd: " + " ".join(plan.command))

    timeout = max(60, plan.seconds * 5 + 60)
    try:
        r = subprocess.run(plan.command, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        exit_code = r.returncode
    except subprocess.TimeoutExpired:
        exit_code = -1
        out = "TIMEOUT tras %d s" % timeout
    except OSError as e:
        die("no se pudo lanzar Godot: %s" % e)

    tail = out.strip()
    if tail:
        print(tail[-2000:])

    has_ffprobe = shutil.which("ffprobe") is not None
    if has_ffprobe:
        probe = lambda p: ffprobe_duration(p)  # noqa: E731
        frames = lambda p: ffprobe_frames(p)  # noqa: E731
    else:
        probe = None
        frames = None
    result = verify_recording(plan, exit_code, probe_fn=probe, frames_fn=frames)

    print("[argos_record] " + ("VERDE" if result["ok"] else "ROJO") + ": "
          + ("%s (%d B, %d fichero(s)%s%s)"
             % (plan.out_path, result["bytes"], result["files"],
                ", %ss" % result["duration_s"] if result["duration_s"] is not None else "",
                ", %s/%s frames" % (result["frames_decoded"], result["frames_expected"])
                if result["frames_decoded"] is not None else "")))
    for w in result["warnings"]:
        print("[argos_record] aviso: " + w, file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
