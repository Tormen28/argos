#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_version_sync.py — Version sincronizada de ARGOS (BLOQUE B6).

Verifica que la version declarada en `argos/addons/argos/plugin.cfg` y la de
`serverInfo` en `tools/argos_mcp.py` COINCIDEN (evita desincronizacion
silenciosa al cortar un release). Tambien valida el formato SemVer y el nombre
del servidor.

Solo stdlib (no importa websockets ni arranca el server): parsea los archivos
como texto. Ejecutar: python tools/test_version_sync.py
"""
import os
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
PLUGIN_CFG = ROOT / "argos" / "addons" / "argos" / "plugin.cfg"
MCP = HERE / "argos_mcp.py"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read_plugin_version():
    """Version de plugin.cfg (linea `version="X"`). None si no aparece."""
    if not PLUGIN_CFG.exists():
        return None
    text = PLUGIN_CFG.read_text(encoding="utf-8")
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', text)
    return m.group(1) if m else None


def read_server_version():
    """Version de serverInfo en argos_mcp.py. None si no aparece."""
    if not MCP.exists():
        return None
    text = MCP.read_text(encoding="utf-8")
    m = re.search(r'"serverInfo"\s*:\s*\{[^}]*"version"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else None


def read_server_name():
    """Nombre de serverInfo en argos_mcp.py. None si no aparece."""
    if not MCP.exists():
        return None
    text = MCP.read_text(encoding="utf-8")
    m = re.search(r'"serverInfo"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else None


class TestVersionSync(unittest.TestCase):
    def test_plugin_cfg_existe(self):
        self.assertTrue(PLUGIN_CFG.exists(), "falta %s" % PLUGIN_CFG)

    def test_argos_mcp_existe(self):
        self.assertTrue(MCP.exists(), "falta %s" % MCP)

    def test_plugin_version_parseable(self):
        v = read_plugin_version()
        self.assertIsNotNone(v, "plugin.cfg sin version=\"...\"")
        self.assertRegex(v, SEMVER, "plugin.cfg version no es SemVer: %r" % v)

    def test_server_version_parseable(self):
        v = read_server_version()
        self.assertIsNotNone(v, "argos_mcp.py serverInfo sin version")
        self.assertRegex(v, SEMVER, "serverInfo version no es SemVer: %r" % v)

    def test_versions_coinciden(self):
        pv = read_plugin_version()
        sv = read_server_version()
        self.assertEqual(
            pv, sv,
            "version DESINCRONIZADA: plugin.cfg=%r serverInfo=%r" % (pv, sv),
        )

    def test_server_name(self):
        self.assertEqual(read_server_name(), "argos")


if __name__ == "__main__":
    unittest.main(verbosity=2)
