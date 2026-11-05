#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_mcp.py — MCP (stdio) server for the ARGOS portal (Hermes <-> Godot).

Exposes the Godot editor as MCP tools. The ARGOS addon must be enabled and
the editor open (headless or windowed).

Env:
  ARGOS_PROJECT: default Godot project. argos_editor_launch, argos_check and
      argos_run_play refuse any project outside this path or
      ARGOS_ALLOWED_PROJECTS.
  ARGOS_ALLOWED_PROJECTS: os.pathsep-separated extra project directories
      allowed for launch, check and play.
  ARGOS_GODOT: Godot executable used by argos_editor_launch.
"""
import asyncio
import json
import logging
import os
import pathlib
import random
import subprocess
import sys
import time

# BLOQUE A-3: validador GDScript con errores estructurados (módulo hermano en tools/).
try:
    import argos_check
except ImportError:  # pragma: no cover - sólo si se ejecuta fuera de tools/
    argos_check = None

# BLOQUE B2: action history (.argos/history.json). Escritor unico = este server.
try:
    import argos_history
except ImportError:  # pragma: no cover - sólo si se ejecuta fuera de tools/
    argos_history = None

try:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect
except ImportError:
    print("ERROR: falta 'websockets'", file=sys.stderr)
    sys.exit(2)

DEFAULT_PROJECT = os.environ.get("ARGOS_PROJECT", "")
try:
    from argos_editor import default_godot_executable
except ImportError:  # pragma: no cover - sólo si se ejecuta fuera de tools/
    def default_godot_executable() -> str:
        return "godot" if os.name != "nt" else ""
DEFAULT_GODOT = os.environ.get("ARGOS_GODOT", default_godot_executable())

# launch / check / play ejecutan el proyecto (plugins de editor, @tool, GDScript).
# Solo aceptan ARGOS_PROJECT o ARGOS_ALLOWED_PROJECTS.
PROCESS_PROJECT_TOOLS = frozenset({
    "argos_editor_launch",
    "argos_check",
    "argos_run_play",
})
# argos_raw can forward play; that path executes project GDScript too.
_PLAY_ACTIONS = frozenset({"run.play_main", "run.play_scene"})


def _gates_project(name: str, args: dict) -> bool:
    if name in PROCESS_PROJECT_TOOLS:
        return True
    if name == "argos_raw" and str((args or {}).get("action", "")) in _PLAY_ACTIONS:
        return True
    return False


_READ_ONLY_TOOLS = frozenset({
    "argos_ping",
    "argos_status",
    "argos_scene_tree",
    "argos_node_get_prop",
    "argos_runtime_info",
    "argos_history",
})

# --- Retry con backoff (BLOQUE A-1) -------------------------------------------
# Si el editor está reimportando o el WS cae transitoriamente, la operación se
# reintenta reconectando (releyendo puerto y token de .argos/, porque el token
# rota por sesión de editor). SOLO se reintenta lo transitorio; los errores
# lógicos (protocolo, token inválido, JSON inválido) se propagan tal cual.
RETRY_MAX_RETRIES = 3    # reintentos tras el intento inicial (4 intentos totales)
RETRY_BASE_DELAY = 0.5   # s: 0.5 -> 1.0 -> 2.0
RETRY_JITTER = 0.2       # ±20% sobre el delay base

# Presupuesto TOTAL de ws_call (fix M2 del review A-1/A-2): acota el worst-case
# cuando el endpoint acepta TCP pero nunca responde (p. ej. editor reimportando).
# Antes: 4 intentos x recv(25 s) + backoff ≈ 103 s. Ahora: deadline único de 60 s
# repartido entre intentos (cada recv usa min(timeout, tiempo restante)).
WS_CALL_MAX_TOTAL_S = 60.0

# Transitorios: transporte caído/refused/reset/aborted (ConnectionError),
# timeouts (TimeoutError; asyncio.TimeoutError es la builtin desde Py 3.11,
# se lista por compatibilidad con Py anteriores) y cierres del WS durante la
# operación (ConnectionClosed: anómalo o limpio — el editor pudo reiniciar).
_TRANSIENT_EXC = (ConnectionError, TimeoutError, asyncio.TimeoutError, ConnectionClosed)

# --- Heartbeat del editor (BLOQUE A-2) ----------------------------------------
# El plugin escribe <proyecto>/.argos/heartbeat (JSON {"ts": ms_unix, "pid": N})
# cada <=30 s desde _process(). Permite distinguir, cuando el WS no responde,
# «editor reimportando» (proceso vivo, heartbeat fresco) de «editor muerto»
# (heartbeat viejo o ausente).
HEARTBEAT_FRESH_S = 90.0  # umbral de frescura (>=3 heartbeats del plugin)

def _heartbeat_age(project: str):
    """Segundos desde el último heartbeat del editor; None si no hay archivo.

    Tolerante al formato: JSON {"ts": ms, "pid": N} o número plano en ms unix.
    Ante contenido ilegible devuelve None (fail-safe: no inventa edad).
    """
    if not project:
        return None
    hb = pathlib.Path(project) / ".argos" / "heartbeat"
    try:
        raw = hb.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    ts_ms = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            ts_ms = data.get("ts")
        elif isinstance(data, (int, float)):
            ts_ms = data
    except ValueError:
        try:
            ts_ms = float(raw)
        except ValueError:
            return None
    if not isinstance(ts_ms, (int, float)) or isinstance(ts_ms, bool) or ts_ms <= 0:
        return None
    return max(0.0, time.time() - ts_ms / 1000.0)

def _classify_heartbeat(age) -> str:
    """Clasifica el estado del editor según la edad del heartbeat (en s)."""
    if age is None:
        return "editor no está corriendo (sin heartbeat en .argos/)"
    if age <= HEARTBEAT_FRESH_S:
        return "editor ocupado, probablemente reimportando (heartbeat de hace %.0f s)" % age
    return "editor no está corriendo (heartbeat de hace %.0f s)" % age

def _heartbeat_verdict(project: str) -> str:
    """Veredicto legible del estado del editor (para logs de error)."""
    return _classify_heartbeat(_heartbeat_age(project))

# --- Límites / timeouts / rate (BLOQUE B1) ------------------------------------
# Presupuesto de tiempo por acción (el plugin procesa síncrono en _process; una
# captura o un batch tardan más que una lectura). Se pasa a ws_call(timeout=…).
ACTION_TIMEOUT_DEFAULT_S = 25.0
ACTION_TIMEOUTS = {
    "capture.scene": 30.0,
    "capture.editor_2d": 30.0,
    "runtime.capture": 30.0,
    "editor.quit": 15.0,
}
BATCH_TIMEOUT_MAX_S = 55.0  # action.batch: min(55, 10 + 2 x len(ops))

# Tope del texto de un `result` reenviado al cliente MCP (protege al cliente).
MAX_RESULT_BYTES = 4 * 1024 * 1024

# Espejo de los caps del PLUGIN (§3.1) para fallar rápido antes de enviar.
MAX_MSG_BYTES = 8 * 1024 * 1024       # 8 MiB
MAX_PROPS = 128                        # claves por node.create
MAX_PROP_STR = 4096                    # chars por valor string
MAX_PROPS_BYTES = 256 * 1024           # 262144 B de JSON de props
CAPTURE_MIN = 16
CAPTURE_MAX = 4096

# Rate limiting opcional (default OFF). El server MCP es stdio secuencial; el
# riesgo real es un agente en bucle que sature el hilo del editor (síncrono).
RATE_LIMIT_PER_MIN = int(os.environ.get("ARGOS_RATE_LIMIT_PER_MIN", "0") or 0)
_RATE_BUCKETS = {}  # project -> {"tokens": float, "ts": monotonic}


def _is_mutating(action: str) -> bool:
    """True para acciones MUTANTES (sujetas a rate guard); lecturas exentas."""
    return action.startswith("node.") or action in ("scene.new", "action.batch")


def _action_timeout(action: str, payload) -> float:
    """Timeout (s) de una acción; `action.batch` escala con el nº de ops."""
    if action == "action.batch":
        ops = payload.get("ops") if isinstance(payload, dict) else None
        n = len(ops) if isinstance(ops, list) else 0
        return min(BATCH_TIMEOUT_MAX_S, 10.0 + 2.0 * n)
    return ACTION_TIMEOUTS.get(action, ACTION_TIMEOUT_DEFAULT_S)


def _rate_check(project: str, action: str):
    """Token bucket por proyecto para acciones mutantes (§3.3).

    Devuelve None si la acción pasa; o los ms de espera recomendados
    (retry_after_ms) si el bucket está vacío. Con RATE_LIMIT_PER_MIN<=0 no limita.
    """
    limit = RATE_LIMIT_PER_MIN
    if limit <= 0 or not _is_mutating(action):
        return None
    now = time.monotonic()
    rate = limit / 60.0
    b = _RATE_BUCKETS.get(project)
    if b is None:
        b = {"tokens": float(limit), "ts": now}
        _RATE_BUCKETS[project] = b
    else:
        b["tokens"] = min(float(limit), b["tokens"] + (now - b["ts"]) * rate)
        b["ts"] = now
    if b["tokens"] >= 1.0:
        b["tokens"] -= 1.0
        return None
    return int(max(1.0, (1.0 - b["tokens"]) / rate) * 1000.0)


def _capture_segments(path: str):
    return str(path).replace("\\", "/").split("/")


def _capture_has_dotdot(path: str) -> bool:
    return ".." in _capture_segments(path)


def _capture_is_absolute(path: str) -> bool:
    norm = str(path).replace("\\", "/")
    if norm.startswith("/"):
        return True
    if path.startswith("\\\\"):
        return True
    if len(norm) >= 3 and norm[0].isalpha() and norm[1] == ":" and norm[2] == "/":
        return True
    return pathlib.Path(path).is_absolute()


def _collapse_capture_rel(rel: str) -> str:
    kept = []
    for seg in str(rel).split("/"):
        if seg in ("", "."):
            continue
        kept.append(seg)
    return "/".join(kept)


def _capture_inside(project: str, rel: str, under_captures: bool):
    """None if `rel` stays inside the project (and captures, when required)."""
    try:
        root = pathlib.Path(project).expanduser().resolve()
        dest = (root / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        return ("bad_out", "could not resolve capture path")
    try:
        dest.relative_to(root)
    except ValueError:
        return ("bad_out", "PNG must stay inside the project")
    if under_captures:
        try:
            captures = (root / ".argos" / "captures").resolve()
            captures.relative_to(root)
            if dest == captures:
                return ("bad_out", "expected a .png file")
            dest.relative_to(captures)
        except ValueError:
            return ("bad_out", "PNG must stay under project/.argos/captures")
    if dest.exists():
        return ("file_exists", "refusing to overwrite an existing file")
    return None


def _check_out(out_path: str, project: str = ""):
    """PNG output must be res:// or project/.argos/captures/.

    Rejects absolute paths and any `..` segment, and refuses to overwrite.
    """
    raw = str(out_path or "").strip()
    if not raw or not raw.lower().endswith(".png"):
        return ("bad_out", "expected a .png path")
    if "\x00" in raw:
        return ("bad_out", "path contains NUL")
    if _capture_has_dotdot(raw):
        return ("bad_out", "path must not contain '..'")
    norm = raw.replace("\\", "/")
    if norm.startswith("res://"):
        rest = norm[6:]
        if rest.startswith("/") or _capture_is_absolute(rest):
            return ("bad_out", "absolute paths are not allowed")
        rel = _collapse_capture_rel(rest)
        if not rel.lower().endswith(".png"):
            return ("bad_out", "expected a .png file")
        if project:
            err = _capture_inside(project, rel, under_captures=False)
            if err:
                return err
        return None
    if _capture_is_absolute(raw) or _capture_is_absolute(norm):
        return ("bad_out", "absolute paths are not allowed")
    rel = norm
    while rel.startswith("./"):
        rel = rel[2:]
    rel = _collapse_capture_rel(rel)
    if not rel.startswith(".argos/captures/") or rel.endswith("/"):
        return ("bad_out", "PNG must be under project/.argos/captures or res://")
    if not rel.lower().endswith(".png"):
        return ("bad_out", "expected a .png file")
    if project:
        err = _capture_inside(project, rel, under_captures=True)
        if err:
            return err
    return None


def _check_size(w, h):
    for v in (w, h):
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return ("bad_size", "w/h no entero")
        if iv < CAPTURE_MIN or iv > CAPTURE_MAX:
            return ("bad_size", "%d fuera de rango %d..%d" % (iv, CAPTURE_MIN, CAPTURE_MAX))
    return None


def _validate_payload(action: str, payload, project: str = ""):
    """Comprueba tamaño/paths/props ANTES de enviar al plugin (fail-fast).

    Devuelve None si es válido, o (error_code, detail). Espeja los caps del
    plugin; no sustituye su validación, sólo evita round-trips condenados.
    """
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return ("bad_payload", "payload no serializable")
    if size > MAX_MSG_BYTES:
        return ("payload_too_large", "%d > %d bytes" % (size, MAX_MSG_BYTES))
    if action in ("scene.open", "fs.update_file"):
        if not str(payload.get("path", "")).startswith("res://"):
            return ("bad_path", "se espera res://...")
    if action == "capture.scene":
        err = _check_out(str(payload.get("out", "")), project)
        if err:
            return err
        err = _check_size(payload.get("w"), payload.get("h"))
        if err:
            return err
    if action == "capture.editor_2d":
        err = _check_out(str(payload.get("path", "")), project)
        if err:
            return err
    if action == "runtime.capture":
        err = _check_out(str(payload.get("out", "")), project)
        if err:
            return err
    if action == "node.create":
        props = payload.get("props")
        if isinstance(props, dict):
            if len(props) > MAX_PROPS:
                return ("too_many_props", "%d > %d" % (len(props), MAX_PROPS))
            try:
                psize = len(json.dumps(props, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                return ("bad_payload", "props no serializables")
            if psize > MAX_PROPS_BYTES:
                return ("props_too_large", "%d > %d bytes" % (psize, MAX_PROPS_BYTES))
            for k, v in props.items():
                if isinstance(v, str) and len(v) > MAX_PROP_STR:
                    return ("prop_too_long", str(k))
    return None

_log = logging.getLogger("argos_mcp")
_log.setLevel(logging.INFO)
_log.propagate = False  # stdout es JSON-RPC; el handler a stderr se pone en main()

def _setup_log():
    if not _log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("[argos_mcp] %(levelname)s %(message)s"))
        _log.addHandler(h)

def log(*a):
    print("[argos_mcp]", *a, file=sys.stderr, flush=True)

_CONN = None
_CONN_KEY = None

def _is_transient(exc: BaseException) -> bool:
    """True si el error parece transitorio (editor reimportando/reiniciado, WS caído)."""
    return isinstance(exc, _TRANSIENT_EXC)

def _is_retryable(exc: BaseException) -> bool:
    """True si el error merece un reintento en ws_call.

    Incluye lo transitorio de transporte/WS y los fallos transitorios de E/S de
    archivo (fix B1: p. ej. PermissionError al leer el token mientras el editor
    rota/reescribe .argos/token en Windows). Los errores lógicos —JSON inválido,
    `int()` sobre un `port` corrupto (ValueError)— NO son reintentables y se
    propagan rápido.
    """
    return _is_transient(exc) or isinstance(exc, OSError)

def _retry_delay_ms(retry_index: int) -> float:
    """Delay de backoff exponencial para el reintento retry_index (0-based), en ms."""
    base = RETRY_BASE_DELAY * (2 ** retry_index) * 1000.0
    return base * (1.0 + random.uniform(-RETRY_JITTER, RETRY_JITTER))

def _read_endpoint(project: str, port_file: str, port_default: int):
    """Lee (port, token) de <project>/.argos/. Se llama en CADA intento: el token rota."""
    port, token = port_default, ""
    if project:
        base = pathlib.Path(project) / ".argos"
        pf = base / port_file
        if pf.exists():
            port = int(pf.read_text(encoding="utf-8").strip())
        tf = base / "token"
        if tf.exists():
            token = tf.read_text(encoding="utf-8").strip()
    return port, token

def _drop_conn():
    global _CONN, _CONN_KEY
    if _CONN is not None:
        try:
            _CONN.close()
        except Exception:
            pass
    _CONN, _CONN_KEY = None, None

def ws_call(project: str, payload: dict, timeout: float = 25.0, port_file: str = "port", port_default: int = 9080) -> dict:
    """Envía una operación al plugin por WS con retry ante fallos transitorios.

    Hasta 3 reintentos con backoff 500ms -> 1s -> 2s (jitter ±20%), acotados por un
    presupuesto TOTAL de WS_CALL_MAX_TOTAL_S (60 s): si el endpoint acepta TCP y no
    responde (editor reimportando), cada recv espera como mucho el tiempo restante y
    al agotarse el presupuesto ws_call propaga — worst-case acotado ≈60 s (antes ≈103 s).
    Antes de cada reintento reconecta releyendo puerto y token de .argos/ (rotan por
    sesión de editor; la lectura va DENTRO del try para que un E/S transitorio se
    reintente) y reenvía la operación una vez por intento. Los errores lógicos
    (respuestas de error del plugin, token inválido, JSON inválido, `port` corrupto)
    NO se reintentan: se propagan tal cual.
    """
    global _CONN, _CONN_KEY
    # §3.4: valida tamaño/paths/props antes de gastar un round-trip.
    verr = _validate_payload(str(payload.get("action", "")), payload, project)
    if verr:
        return {"ok": False, "id": payload.get("id", 1), "error": verr[0], "detail": verr[1]}
    total = 1 + RETRY_MAX_RETRIES
    deadline = time.monotonic() + WS_CALL_MAX_TOTAL_S
    for attempt in range(1, total + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "presupuesto total de %.0f s agotado en ws_call (endpoint sin responder)"
                % WS_CALL_MAX_TOTAL_S)
        try:
            # B1: dentro del try -> un E/S transitorio al leer token/port se reintenta
            port, token = _read_endpoint(project, port_file, port_default)
            key = (port, token)
            if _CONN is None or _CONN_KEY != key:
                _drop_conn()
                _CONN = connect(f"ws://127.0.0.1:{port}",
                                open_timeout=min(5.0, remaining), close_timeout=1)
                _CONN_KEY = key
            out = dict(payload)
            out.setdefault("id", 1)
            out["token"] = token
            _CONN.send(json.dumps(out))
            recv_timeout = max(0.1, min(timeout, deadline - time.monotonic()))
            resp = json.loads(_CONN.recv(timeout=recv_timeout))
            if attempt > 1:
                _log.info("operación '%s' OK tras %d reintento(s)", out.get("action", "?"), attempt - 1)
            return resp
        except Exception as e:
            _drop_conn()
            if attempt >= total or not _is_retryable(e):
                raise
            delay_s = _retry_delay_ms(attempt - 1) / 1000.0
            if time.monotonic() + delay_s >= deadline:
                raise  # sin presupuesto para otro intento: propaga el error actual
            _log.warning("intento %d/%d, error %s: %s; reintentando en %.0f ms",
                         attempt, RETRY_MAX_RETRIES, type(e).__name__, e, delay_s * 1000.0)
            time.sleep(delay_s)

def _proj(args: dict) -> str:
    """Proyecto: arg `project` > env ARGOS_PROJECT > cwd si contiene project.godot."""
    pj = args.get("project") or DEFAULT_PROJECT
    if not pj and os.path.isfile(os.path.join(os.getcwd(), "project.godot")):
        pj = os.getcwd()
    return str(pj or "")


def _resolve_dir(raw: str):
    try:
        return pathlib.Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _allowlist_entries():
    """Resolved project directories launch/check/play may touch.

    ARGOS_PROJECT (DEFAULT_PROJECT, fixed at import) plus ARGOS_ALLOWED_PROJECTS
    (os.pathsep-separated, read on each call).
    """
    raws = []
    if str(DEFAULT_PROJECT).strip():
        raws.append(str(DEFAULT_PROJECT).strip())
    blob = os.environ.get("ARGOS_ALLOWED_PROJECTS", "")
    for line in str(blob).replace("\r", "\n").split("\n"):
        for part in line.split(os.pathsep):
            if part.strip():
                raws.append(part.strip())
    out = []
    seen = set()
    for raw in raws:
        if "\x00" in raw:
            continue
        path = _resolve_dir(raw)
        if path is None:
            continue
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _match_allowlist(requested: str, entries):
    if "\x00" in str(requested):
        return None
    path = _resolve_dir(str(requested).strip())
    if path is None:
        return None
    key = os.path.normcase(os.path.normpath(str(path)))
    for entry in entries:
        if os.path.normcase(os.path.normpath(str(entry))) == key:
            return entry
    return None


def _project_refused(detail: str) -> dict:
    return _error("ARGOS: project_not_allowed " + detail)


def _enforce_allowed_project(requested):
    """Return (canonical project, None) or (None, MCP error).

    Used by argos_editor_launch, argos_check and argos_run_play. A per-call
    project outside ARGOS_PROJECT / ARGOS_ALLOWED_PROJECTS is refused. When
    the call omits project, ARGOS_PROJECT is used, or the sole allowlist entry.
    """
    entries = _allowlist_entries()
    if not entries:
        return None, _project_refused(
            "Set ARGOS_PROJECT or ARGOS_ALLOWED_PROJECTS before launch, check or play. "
            "These tools refuse every project that is not on that allowlist. "
            "/ Define ARGOS_PROJECT o ARGOS_ALLOWED_PROJECTS; launch, check y play "
            "rechazan cualquier proyecto fuera de esa lista.")
    req = str(requested or "").strip()
    if not req:
        preferred = str(DEFAULT_PROJECT).strip()
        if preferred:
            got = _match_allowlist(preferred, entries)
            if got is not None:
                return str(got), None
        if len(entries) == 1:
            return str(entries[0]), None
        return None, _project_refused(
            "Several projects are allowlisted; pass project set to one of them. "
            "/ Hay varios proyectos en la lista; indica project.")
    got = _match_allowlist(req, entries)
    if got is None:
        return None, _project_refused(
            "That project is outside the allowlist (ARGOS_PROJECT / ARGOS_ALLOWED_PROJECTS). "
            "launch, check and play will not open it. "
            "/ Ese proyecto está fuera de la lista permitida.")
    return str(got), None


# --- Auto-open de escena (no_scene) -------------------------------------------
# Herramientas que necesitan una escena editada. Si el editor responde no_scene,
# ARGOS abre `scene_path` (si se pasa) o la escena principal del proyecto
# (application/run/main_scene) y reintenta UNA vez. Si no hay ninguna, devuelve
# un error claro y accionable.
SCENE_TOOLS = {
    "argos_node_create", "argos_node_instantiate", "argos_node_reparent",
    "argos_node_get_prop", "argos_node_set_prop", "argos_node_delete",
    "argos_scene_save", "argos_scene_reload",
}
NO_SCENE_HELP = ("ARGOS: no_scene - no scene is open in the Godot editor and none could be opened "
                 "automatically. Pass scene_path='res://your_scene.tscn', call argos_scene_open, "
                 "or set application/run/main_scene in project.godot. / No hay escena abierta: "
                 "pasa scene_path, usa argos_scene_open o define la escena principal.")


def _main_scene(project: str) -> str:
    """Lee run/main_scene de project.godot ('' si no existe)."""
    try:
        txt = pathlib.Path(project, "project.godot").read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return ""
    section = ""
    for line in txt.splitlines():
        t = line.strip()
        if t.startswith("[") and t.endswith("]"):
            section = t[1:-1]
            continue
        if section == "application" and t.startswith("run/main_scene"):
            val = t.split("=", 1)[1].strip().strip('"')
            if val.startswith("uid://"):
                return ""  # uid: lo resuelve el editor, no podemos abrirlo por ruta
            return val
    return ""


def _with_scene(name: str, a: dict, project: str, fn):
    out = fn()
    if name not in SCENE_TOOLS or not out.get("isError"):
        return out
    if _error_code(out["content"][0]["text"]) != "no_scene":
        return out
    target = str(a.get("scene_path") or "") or _main_scene(project)
    if not target.startswith("res://"):
        return _error(NO_SCENE_HELP)
    opened = _call(project, "scene.open", path=target)
    if opened.get("isError"):
        return _error(NO_SCENE_HELP + " (open %s failed: %s)" % (target, opened["content"][0]["text"]))
    for delay in (0.2, 0.5, 1.0):
        time.sleep(delay)
        out = fn()
        if not (out.get("isError") and _error_code(out["content"][0]["text"]) == "no_scene"):
            break
    if not out.get("isError"):
        data = _result_data(out)
        if isinstance(data, dict):
            data["auto_opened_scene"] = target
            return _result(data)
    return out

def _result(data) -> dict:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    # §3.1: no reenvía un resultado desmesurado (protege al cliente MCP).
    nbytes = len(text.encode("utf-8"))
    if nbytes > MAX_RESULT_BYTES:
        return _error("ARGOS: result_too_large (%d > %d bytes)" % (nbytes, MAX_RESULT_BYTES))
    return {"content": [{"type": "text", "text": text}]}

def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}

def _call(project, action, **extra):
    resp = ws_call(project, {"action": action, **extra}, timeout=_action_timeout(action, extra))
    if resp.get("ok"):
        return _result(resp.get("result"))
    detail = resp.get("detail", "")
    if resp.get("retry_after_ms") is not None:
        detail = ("%s retry_after_ms=%d" % (detail, resp["retry_after_ms"])).strip()
    return _error(f"ARGOS: {resp.get('error')} {detail}".strip())

def _call_rt(project, action, **extra):
    resp = ws_call(project, {"action": action, **extra}, timeout=_action_timeout(action, extra),
                   port_file="runtime_port", port_default=9091)
    if resp.get("ok"):
        return _result(resp.get("result"))
    return _error(f"ARGOS-RT: {resp.get('error')} {resp.get('detail','')}".strip())

# Tools MCP mutantes -> acción WS (para el rate guard, §3.3).
_TOOL_ACTIONS = {
    "argos_node_create": "node.create",
    "argos_node_instantiate": "node.instantiate",
    "argos_node_reparent": "node.reparent",
    "argos_node_set_prop": "node.set_prop",
    "argos_node_delete": "node.delete",
    "argos_scene_new": "scene.new",
}


def _action_of(name: str, a: dict) -> str:
    if name == "argos_raw":
        return str(a.get("action", ""))
    return _TOOL_ACTIONS.get(name, "")

def _helper() -> str:
    return str(pathlib.Path(__file__).with_name("argos_editor.py"))

def _check_tool(project: str, a: dict):
    """Tool MCP `argos_check` (BLOQUE A-3): valida .gd con errores estructurados.

    No usa el WS del editor: invoca Godot `--check-only` (como el CLI). Devuelve
    `{ok, results, count_errors}`; `results[i]` lleva `target`, `ok`, `exit`,
    `errors` (archivo:línea + mensaje) y `unparsed_raw` (fail-loud).
    """
    if argos_check is None:
        return _error("argos_check no disponible (módulo argos_check.py no importable)")
    if not project:
        return _error("argos_check: falta 'project' (o define ARGOS_PROJECT)")
    targets = argos_check.collect_targets(project, a.get("paths") or [], bool(a.get("all", False)))
    if not targets:
        return _error("argos_check: sin objetivos (pasa 'paths' o all=true)")
    results = []
    count_errors = 0
    allok = True
    for t in targets:
        entry = argos_check.check_one(project, t)
        entry["target"] = t
        results.append(entry)
        allok = allok and entry["ok"]
        count_errors += len(entry.get("errors") or [])
    return _result({"ok": allok, "results": results, "count_errors": count_errors})


# --- B2: action history (escritor unico) --------------------------------------

def _error_code(text: str) -> str:
    """Extrae el codigo ARGOS del texto de un `_error` (p. ej. no_prop)."""
    t = (text or "").strip()
    for pref in ("ARGOS-RT: ", "ARGOS: ", "ARGOS excepción:"):
        if t.startswith(pref):
            t = t[len(pref):].strip()
            break
    return t.split()[0] if t else "error"


def _result_data(out):
    """Resultado JSON decodificado del content, o None (error / no JSON)."""
    if out.get("isError"):
        return None
    try:
        return json.loads(out["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _sanitize_group(name) -> str:
    """Saneado del `name` de un batch (§1.4): sin control, `"`, <=80 chars."""
    s = " ".join(str(name or "").strip().split())
    s = "".join(ch for ch in s if ord(ch) >= 32 and ch != '"')
    s = s[:80]
    return s or "operacion"


def _history_tool(project: str, a: dict):
    """Tool MCP `argos_history` (§2.4): lee .argos/history.json con filtros."""
    if argos_history is None:
        return _error("argos_history no disponible (módulo argos_history.py no importable)")
    if not project:
        return _error("argos_history: falta 'project' (o define ARGOS_PROJECT)")
    res = argos_history.read(project, limit=a.get("limit", 50),
                             since=a.get("since"), tool=a.get("tool"), status=a.get("status"))
    return _result(res)


def _record_history(project: str, tool: str, args: dict, out: dict, duration_ms: float):
    """Registra una operacion MCP en el historial (§2.2). Fail-silent."""
    # micro-fix B3: leer el historial no debe auto-registrarse (solo ruido). El
    # escritor unico es este server, asi que el filtro vive aqui.
    if tool == "argos_history":
        return
    if not project or argos_history is None:
        return
    action = _action_of(tool, args) or tool
    is_err = bool(out.get("isError"))
    text = ""
    try:
        text = out["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        pass
    data = _result_data(out)
    undo_action = data.get("undo_action") if isinstance(data, dict) else None
    undo_group = None
    if action == "action.batch":
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        undo_group = _sanitize_group(args.get("name") or payload.get("name"))
    argos_history.append(project, {
        "source": "mcp",
        "tool": tool,
        "action": action,
        "params": args,
        "status": "error" if is_err else "ok",
        "error": _error_code(text) if is_err else None,
        "duration_ms": int(duration_ms),
        "undo_action": undo_action,
        "undo_group": undo_group,
    })


def call_tool(name: str, args: dict):
    """Punto de entrada de una tool MCP: registra en el historial (B2) y ejecuta.

    El servidor MCP es el ESCRITOR UNICO de `.argos/history.json` (§2.1): se
    registra justo antes de responder, con duracion y resultado. El registro es
    fail-silent: nunca rompe la operacion MCP.

    launch / check / play se rechazan ANTES de escribir historial si el proyecto
    no está en la allowlist: no se crea `.argos/` en un directorio ajeno.
    """
    a = dict(args or {})
    if _gates_project(name, a):
        allowed, err = _enforce_allowed_project(a.get("project"))
        if err is not None:
            return err
        a["project"] = allowed
    project = _proj(a)
    t0 = time.monotonic()
    out = _with_scene(name, a, project, lambda: _call_tool_impl(name, a, project))
    try:
        _record_history(project, name, a, out, (time.monotonic() - t0) * 1000.0)
    except Exception as e:  # pragma: no cover - defensa extra
        log("history: fallo inesperado %s: %s" % (type(e).__name__, e))
    return out


def _call_tool_impl(name: str, a: dict, project: str):
    try:
        if _gates_project(name, a):
            allowed, err = _enforce_allowed_project(a.get("project") or project)
            if err is not None:
                return err
            project = allowed
            a["project"] = allowed
        # §3.3: rate guard opcional sobre acciones mutantes (lecturas exentas).
        mut_action = _action_of(name, a)
        if _is_mutating(mut_action):
            wait_ms = _rate_check(project, mut_action)
            if wait_ms is not None:
                return _error("ARGOS: rate_limited retry_after_ms=%d" % wait_ms)
        if name == "argos_history":
            return _history_tool(project, a)
        if name == "argos_ping":
            resp = ws_call(project, {"action": "ping"}, timeout=_action_timeout("ping", {}))
            if resp.get("ok"):
                result = dict(resp.get("result") or {})
                age = _heartbeat_age(project)
                # BLOQUE A-2: campo aditivo; null si el plugin aún no escribe heartbeat
                result["heartbeat_age_s"] = (round(age, 1) if age is not None else None)
                return _result(result)
            return _error(f"ARGOS: {resp.get('error')} {resp.get('detail','')}".strip())
        if name == "argos_status":
            return _call(project, "status")
        if name == "argos_scene_tree":
            extra = {}
            if a.get("max_nodes") is not None:
                extra["max_nodes"] = int(a["max_nodes"])
            if a.get("max_depth") is not None:
                extra["max_depth"] = int(a["max_depth"])
            return _call(project, "scene.get_tree", **extra)
        if name == "argos_scene_open":
            return _call(project, "scene.open", path=a.get("path", ""))
        if name == "argos_scene_save":
            return _call(project, "scene.save")
        if name == "argos_scene_reload":
            return _call(project, "scene.reload")
        if name == "argos_scene_new":
            extra = {"path": a.get("path", "")}
            if a.get("root_class"):
                extra["root_class"] = a["root_class"]
            if a.get("name"):
                extra["name"] = a["name"]
            return _call(project, "scene.new", **extra)
        if name == "argos_node_create":
            extra = {"parent": a.get("parent", ""), "class": a.get("class", ""), "name": a.get("name", "")}
            if a.get("props"):
                extra["props"] = a["props"]
            return _call(project, "node.create", **extra)
        if name == "argos_node_instantiate":
            extra = {"scene": a.get("scene", ""), "parent": a.get("parent", "")}
            if a.get("name"):
                extra["name"] = a["name"]
            return _call(project, "node.instantiate", **extra)
        if name == "argos_node_reparent":
            extra = {"path": a.get("path", ""), "parent": a.get("parent", "")}
            if "keep_transform" in a:
                extra["keep_transform"] = bool(a["keep_transform"])
            return _call(project, "node.reparent", **extra)
        if name == "argos_node_get_prop":
            return _call(project, "node.get_prop", path=a.get("path", ""), prop=a.get("prop", ""))
        if name == "argos_node_set_prop":
            return _call(project, "node.set_prop", path=a.get("path", ""), prop=a.get("prop", ""), value=a.get("value"))
        if name == "argos_node_delete":
            return _call(project, "node.delete", path=a.get("path", ""))
        if name == "argos_undo":
            return _call(project, "undo.undo")
        if name == "argos_redo":
            return _call(project, "undo.redo")
        if name == "argos_runtime_info":
            return _call_rt(project, "runtime.info")
        if name == "argos_runtime_capture":
            return _call_rt(project, "runtime.capture", out=a["out"], wait_frames=int(a.get("wait_frames", 2)))
        if name == "argos_run_play":
            if a.get("scene_path"):
                return _call(project, "run.play_scene", path=a["scene_path"])
            return _call(project, "run.play_main")
        if name == "argos_run_stop":
            return _call(project, "run.stop")
        if name == "argos_capture_scene":
            extra = {"out": a.get("out", ""), "w": int(a.get("w", 1152)), "h": int(a.get("h", 648))}
            if a.get("scene_path"):
                extra["path"] = a["scene_path"]
            return _call(project, "capture.scene", **extra)
        if name == "argos_fs_scan":
            return _call(project, "fs.scan")
        if name == "argos_check":
            return _check_tool(project, a)
        if name == "argos_editor_quit":
            extra = {}
            if bool(a.get("force", False)):
                extra["force"] = True
            return _call(project, "editor.quit", **extra)
        if name == "argos_raw":
            payload = dict(a.get("payload") or {})
            return _call(project, str(a.get("action", "")), **payload)
        if name == "argos_editor_launch":
            proj = project
            if not proj:
                return _error("falta 'project' (o define ARGOS_PROJECT)")
            cmd = [sys.executable, _helper(), "start", proj]
            if not bool(a.get("headless", True)):
                cmd.append("--windowed")
            cmd += ["--wait", str(int(a.get("wait_s", 30)))]
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
            if r.returncode == 0:
                return _result({"started": True, "info": (r.stdout or "").strip()})
            return _error((r.stdout or "") + (r.stderr or ""))
        if name == "argos_editor_stop":
            r = subprocess.run([sys.executable, _helper(), "stop", project], capture_output=True, text=True, timeout=90)
            if r.returncode == 0:
                return _result({"stopped": True, "info": (r.stdout or "").strip()})
            return _error((r.stdout or "") + (r.stderr or ""))
        return _error(f"tool desconocida: {name}")
    except Exception as e:
        if _is_transient(e):
            # BLOQUE A-2: una transitoria que escapa de ws_call == retry agotado.
            # Clasificar con el heartbeat: ¿editor vivo (reimportando) o muerto?
            _log.error("'%s' falló tras agotar los reintentos (%s: %s) — %s",
                       name, type(e).__name__, e, _heartbeat_verdict(project))
        return _error(f"ARGOS excepción: {type(e).__name__}: {e}")

def _pj(desc="Godot project directory (optional when ARGOS_PROJECT is set). launch, check and play accept only ARGOS_PROJECT or ARGOS_ALLOWED_PROJECTS. / Directorio del proyecto (opcional si ARGOS_PROJECT esta definido). launch, check y play solo aceptan esa lista."):
    return {"type": "string", "description": desc}


def _apply_readonly(tools):
    """readOnlyHint only on genuine reads. Strip it from anything that mutates."""
    for tool in tools:
        annotations = dict(tool.get("annotations") or {})
        if tool["name"] in _READ_ONLY_TOOLS:
            annotations["readOnlyHint"] = True
            tool["annotations"] = annotations
        else:
            annotations.pop("readOnlyHint", None)
            if annotations:
                tool["annotations"] = annotations
            else:
                tool.pop("annotations", None)
    return tools


TOOLS = _apply_readonly([
    {"name": "argos_ping", "description": "Ping the Godot editor and check that ARGOS answers. / Ping al editor para comprobar que ARGOS responde.", "inputSchema": {"type": "object", "properties": {"project": _pj()}}},
    {"name": "argos_status", "description": "Editor status: Godot version, project, edited scene, playing flag and port. / Estado del editor: version, proyecto, escena, playing y puerto.", "inputSchema": {"type": "object", "properties": {"project": _pj()}}},
    {"name": "argos_scene_tree", "description": "JSON node tree of the edited scene (name, type, rel, script, pos). max_nodes and max_depth trim it (truncated: true). / Arbol JSON de la escena editada.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "max_nodes": {"type": "integer", "description": "max nodes (default 2000, cap 20000)"}, "max_depth": {"type": "integer", "description": "max depth (default 12, cap 64)"}}}},
    {"name": "argos_scene_open", "description": "Open a scene (res://...) in the editor. / Abre una escena en el editor.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "path": {"type": "string", "description": "res://path/scene.tscn"}}, "required": ["path"]}},
    {"name": "argos_scene_save", "description": "Save the edited scene to disk. / Guarda la escena editada en disco.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string", "description": "optional res:// scene to open if none is open"}}}},
    {"name": "argos_scene_reload", "description": "Reload the edited scene from disk. / Recarga la escena editada desde disco.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string", "description": "optional res:// scene to open if none is open"}}}},
    {"name": "argos_scene_new", "description": "Create a new scene (save and open it). Default root_class is Node2D. / Crea una escena nueva.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "path": {"type": "string"}, "root_class": {"type": "string"}, "name": {"type": "string"}}, "required": ["path"]}},
    {"name": "argos_node_create", "description": "Create a node in the edited scene. props accepts @vec2:x,y and @color:r,g,b,a. / Crea un nodo en la escena editada.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string", "description": "optional res:// scene to open if none is open"}, "parent": {"type": "string", "description": "path relative to the root ('' or '.' = root)"}, "class": {"type": "string"}, "name": {"type": "string"}, "props": {"type": "object"}}, "required": ["class"]}},
    {"name": "argos_node_instantiate", "description": "Instance a saved scene under a parent in the edited scene. / Instancia una escena guardada.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string", "description": "optional res:// scene to open if none is open"}, "scene": {"type": "string"}, "parent": {"type": "string"}, "name": {"type": "string"}}, "required": ["scene"]}},
    {"name": "argos_node_reparent", "description": "Move a node under another parent in the edited scene. / Mueve un nodo a otro padre.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string", "description": "optional res:// scene to open if none is open"}, "path": {"type": "string"}, "parent": {"type": "string"}, "keep_transform": {"type": "boolean"}}, "required": ["path", "parent"]}},
    {"name": "argos_node_get_prop", "description": "Read a node property (Vector and Color come back as @vec2: and @color:). / Lee una propiedad de un nodo.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string", "description": "optional res:// scene to open if none is open"}, "path": {"type": "string"}, "prop": {"type": "string"}}, "required": ["path", "prop"]}},
    {"name": "argos_node_set_prop", "description": "Set a node property. Accepts @vec2:x,y and @color:r,g,b,a. / Cambia una propiedad de un nodo.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string", "description": "optional res:// scene to open if none is open"}, "path": {"type": "string"}, "prop": {"type": "string"}, "value": {}}, "required": ["path", "prop", "value"]}},
    {"name": "argos_node_delete", "description": "Delete a node from the edited scene. / Borra un nodo de la escena editada.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string", "description": "optional res:// scene to open if none is open"}, "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "argos_undo", "description": "Undo the editor's last operation (shared history, including ARGOS edits). / Deshace la ultima operacion del editor.", "inputSchema": {"type": "object", "properties": {"project": _pj()}}},
    {"name": "argos_redo", "description": "Redo the last undone operation. / Rehace la ultima operacion deshecha.", "inputSchema": {"type": "object", "properties": {"project": _pj()}}},
    {"name": "argos_run_play", "description": "Play the game (main scene, or scene_path). The project must be ARGOS_PROJECT or on ARGOS_ALLOWED_PROJECTS. / Ejecuta el juego; el proyecto tiene que estar en la lista permitida.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "scene_path": {"type": "string"}}}},
    {"name": "argos_run_stop", "description": "Stop the running game. / Detiene el juego en ejecucion.", "inputSchema": {"type": "object", "properties": {"project": _pj()}}},
    {"name": "argos_capture_scene", "description": "Render a scene to PNG (offscreen; the editor needs a window), then review the PNG. Output must be res://....png or .argos/captures/....png, with no '..' and no overwrite. / Renderiza una escena a PNG solo en esas rutas.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "out": {"type": "string", "description": "res://....png or .argos/captures/....png (no absolute paths, no '..', no overwrite)"}, "scene_path": {"type": "string"}, "w": {"type": "integer"}, "h": {"type": "integer"}}, "required": ["out"]}},
    {"name": "argos_runtime_info", "description": "Info from the RUNNING game (fps, scene, frames). The game must be running in debug. / Info del juego en ejecucion.", "inputSchema": {"type": "object", "properties": {"project": _pj()}}},
    {"name": "argos_runtime_capture", "description": "PNG of the RUNNING game (real gameplay; needs a window). Output must be res://....png or .argos/captures/....png, with no '..' and no overwrite. / Captura PNG del juego solo en esas rutas.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "out": {"type": "string", "description": "res://....png or .argos/captures/....png (no absolute paths, no '..', no overwrite)"}, "wait_frames": {"type": "integer"}}, "required": ["out"]}},
    {"name": "argos_fs_scan", "description": "Rescan the project filesystem after files were written from outside the editor. / Reescanea el sistema de archivos del proyecto.", "inputSchema": {"type": "object", "properties": {"project": _pj()}}},
    {"name": "argos_check", "description": "Validate GDScript (.gd) with Godot --check-only and return structured errors (file:line + message). Use paths:[...] or all:true (skips addons/ and .godot/). The project must be allowlisted. / Valida scripts y devuelve errores estructurados.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "paths": {"type": "array", "items": {"type": "string"}, "description": "res:// paths of .gd scripts to validate"}, "all": {"type": "boolean", "description": "validate every project .gd (skips addons/ and .godot/)"}}}},
    {"name": "argos_history", "description": "ARGOS operation history (.argos/history.json): action, params, status, error, duration. Filters: limit (<=200), since (ISO8601), tool, status. / Historial de operaciones ARGOS.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "limit": {"type": "integer", "description": "max entries (default 50, cap 200)"}, "since": {"type": "string", "description": "ISO8601; only later entries"}, "tool": {"type": "string"}, "status": {"type": "string", "enum": ["ok", "error"]}}}},
    {"name": "argos_editor_launch", "description": "Launch the Godot editor for an allowlisted project (headless by default; waits for the port). / Lanza el editor de un proyecto permitido.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "headless": {"type": "boolean"}, "wait_s": {"type": "integer"}}}},
    {"name": "argos_editor_stop", "description": "Stop the Godot editor that ARGOS launched. / Detiene el editor lanzado por ARGOS.", "inputSchema": {"type": "object", "properties": {"project": _pj()}}},
    {"name": "argos_editor_quit", "description": "Quit the editor (saves scenes first; force=true skips saving). / Cierra el editor.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "force": {"type": "boolean"}}}},
    {"name": "argos_raw", "description": "Escape hatch: send any ARGOS action with a JSON payload. / Envia cualquier accion ARGOS con un payload JSON.", "inputSchema": {"type": "object", "properties": {"project": _pj(), "action": {"type": "string"}, "payload": {"type": "object"}}, "required": ["action"]}},
])

def send_obj(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def main():
    _setup_log()
    log("iniciado; ARGOS_PROJECT =", DEFAULT_PROJECT or "(ninguno)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method", "")
        try:
            if method == "initialize":
                pv = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
                send_obj({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": pv,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "argos", "version": "1.1.0"},
                }})
            elif method in ("notifications/initialized", "initialized"):
                pass
            elif method == "ping":
                send_obj({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif method == "tools/list":
                send_obj({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = msg.get("params") or {}
                out = call_tool(str(params.get("name", "")), params.get("arguments") or {})
                send_obj({"jsonrpc": "2.0", "id": mid, "result": out})
            elif mid is not None:
                send_obj({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
        except Exception as e:
            if mid is not None:
                send_obj({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})

if __name__ == "__main__":
    main()
