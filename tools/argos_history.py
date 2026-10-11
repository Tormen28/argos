#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argos_history.py — Action history de ARGOS (BLOQUE B2).

Registro persistente (ring buffer) de las operaciones de Hermes sobre el editor:
`.argos/history.json` con formato §2.2 del diseño B.

Escritor UNICO: el servidor MCP (`argos_mcp.py`) en `call_tool`, justo antes de
responder. La CLI (`argos_cli.py history`) SOLO lee (evita doble escritor, R7).

Garantias:
  - Rotacion FIFO por entradas (default 500, env `ARGOS_HISTORY_MAX_ENTRIES`) y
    por bytes (default 1 MiB).
  - Escritura ATOMICA (`history.json.tmp` + `os.replace`), `indent=2`.
  - FAIL-SILENT en escritura: cualquier OSError se loguea a stderr y NO rompe la
    operacion MCP. Estado corrupto al leer: `read` devuelve `history_corrupt`;
    `append` reinicia el archivo (fail-safe, no pierde la operacion actual).

API publica: `append(project, record)`, `read(project, limit, since, tool, status)`.
"""
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

HISTORY_VERSION = 1
HISTORY_FILENAME = "history.json"
HISTORY_MAX_ENTRIES = int(os.environ.get("ARGOS_HISTORY_MAX_ENTRIES", "500") or "500")
HISTORY_MAX_BYTES = 1048576          # 1 MiB
READ_LIMIT_DEFAULT = 50
READ_LIMIT_MAX = 200
PARAM_STR_MAX = 1024                 # 1 KiB por valor string en `params`


def _path(project: str) -> pathlib.Path:
    return pathlib.Path(project) / ".argos" / HISTORY_FILENAME


def _now() -> tuple:
    """(ts_ms, ts_iso8601_utc) del instante actual."""
    ts_ms = int(time.time() * 1000)
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    ts = dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ts_ms % 1000)
    return ts_ms, ts


def _empty() -> dict:
    return {"version": HISTORY_VERSION, "updated_ms": 0, "entries": []}


def _load(project: str):
    """Carga tolerante: ausente -> vacio; JSON invalido -> None (corrupto)."""
    try:
        raw = _path(project).read_text(encoding="utf-8")
    except OSError:
        return _empty()
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    data.setdefault("version", HISTORY_VERSION)
    return data


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _write_atomic(project: str, data: dict) -> None:
    p = _path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(str(p) + ".tmp")
    tmp.write_text(_dump(data), encoding="utf-8")
    os.replace(tmp, p)


# --- redaccion de params (§2.2) ----------------------------------------------

def _shrink(v):
    if isinstance(v, str):
        if len(v) > PARAM_STR_MAX:
            return v[:PARAM_STR_MAX] + "...<truncado>"
        return v
    if isinstance(v, dict):
        return {str(k): _shrink(x) for k, x in v.items() if str(k).lower() != "token"}
    if isinstance(v, list):
        return [_shrink(x) for x in v]
    return v


def redact_params(params) -> dict:
    """Copia redactada de los argumentos MCP: sin `token`, strings <= 1 KiB."""
    if not isinstance(params, dict):
        return {}
    out = {}
    for k, v in params.items():
        if str(k).lower() == "token":
            continue
        out[str(k)] = _shrink(v)
    return out


# --- append -------------------------------------------------------------------

def _make_entry(record: dict, eid: int, now_ms: int, ts: str) -> dict:
    return {
        "id": int(eid),
        "ts": ts,
        "ts_ms": int(now_ms),
        "source": str(record.get("source", "mcp")),
        "tool": str(record.get("tool", "")),
        "action": str(record.get("action", "")),
        "params": redact_params(record.get("params") or {}),
        "status": str(record.get("status", "ok")),
        "error": record.get("error"),
        "duration_ms": int(record.get("duration_ms", 0) or 0),
        "undo_action": record.get("undo_action"),
        "undo_group": record.get("undo_group"),
    }


def _trim(data: dict) -> None:
    """Recorte FIFO por entradas y por bytes, in-place sobre data['entries']."""
    entries = data["entries"]
    if HISTORY_MAX_ENTRIES > 0 and len(entries) > HISTORY_MAX_ENTRIES:
        data["entries"] = entries = entries[-HISTORY_MAX_ENTRIES:]
    # Por bytes: quita por el frente hasta que el documento serializado cumpla.
    while len(entries) > 1 and len(_dump(data).encode("utf-8")) > HISTORY_MAX_BYTES:
        data["entries"] = entries = entries[1:]


def append(project: str, record: dict):
    """Anade una entrada al historial. Devuelve la entrada almacenada o None.

    Fail-silent: un OSError de escritura se loguea y devuelve None sin romper
    la operacion MCP.
    """
    try:
        data = _load(project)
        if data is None:            # corrupto -> reinicia (fail-safe)
            data = _empty()
        entries = data["entries"]
        last_id = 0
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("id"), int) and not isinstance(e.get("id"), bool):
                last_id = max(last_id, e["id"])
        now_ms, ts = _now()
        entry = _make_entry(record or {}, last_id + 1, now_ms, ts)
        entries.append(entry)
        data["entries"] = entries
        data["updated_ms"] = now_ms
        data["version"] = HISTORY_VERSION
        _trim(data)
        _write_atomic(project, data)
        return entry
    except OSError as e:
        print("[argos_history] fail-silent al escribir: %s" % e, file=sys.stderr, flush=True)
        return None


# --- read ---------------------------------------------------------------------

def _parse_since(since):
    """ISO8601 -> ms epoch; None/invalido -> None (sin filtro)."""
    if not since:
        return None
    s = str(since).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def read(project: str, limit: int = READ_LIMIT_DEFAULT, since=None,
         tool=None, status=None) -> dict:
    """Lee entradas (mas recientes al final). Devuelve {ok, count, entries}.

    Archivo corrupto -> {ok: False, error: "history_corrupt"} (sin excepcion).
    `limit` se acota a [1, READ_LIMIT_MAX].
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = READ_LIMIT_DEFAULT
    limit = max(1, min(limit, READ_LIMIT_MAX))

    data = _load(project)
    if data is None:
        return {"ok": False, "error": "history_corrupt"}

    since_ms = _parse_since(since)
    out = []
    for e in data.get("entries") or []:
        if not isinstance(e, dict):
            continue
        if since_ms is not None:
            try:
                if int(e.get("ts_ms", 0)) < since_ms:
                    continue
            except (TypeError, ValueError):
                continue
        if tool and e.get("tool") != tool:
            continue
        if status and e.get("status") != status:
            continue
        out.append(e)
    out = out[-limit:]
    return {"ok": True, "count": len(out), "entries": out}
