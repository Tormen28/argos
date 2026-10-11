# ARGOS — portal Godot ↔ herramientas externas (v1.0.0)

Puente entre un proyecto **Godot 4.x** y herramientas de fuera del editor (CLI, scripts, agentes) sobre **WebSocket local**. El addon *es* el servidor: no hay proceso aparte que mantener (**zero-sidecar**).

Desde fuera del editor se puede: leer el árbol de escena, crear / borrar / mover nodos y cambiar propiedades (con **undo** real del editor), abrir y guardar escenas, lanzar y detener el juego, validar sintaxis GDScript y **capturar imágenes** del editor y del juego en ejecución.

## Componentes

| Pieza | Qué es |
|---|---|
| `argos/addons/argos/` | Addon de Godot: `argos_plugin.gd` (editor: escenas, nodos, undo, play, capturas) + `argos_runtime.gd` (autoload del juego: captura en vivo, solo en modo debug) |
| `tools/argos_mcp.py` | Servidor MCP por stdio — 27 herramientas |
| `tools/argos_cli.py` | Cliente de línea de comandos (incluye `rt-*` para el juego en vivo) |
| `tools/argos_editor.py` | Arranque, parada y estado del editor (headless o con ventana) |
| `tools/argos_check.py` | Validador `--check-only` de GDScript |
| `tools/argos_install.py` | Instala el addon en un proyecto (addon + autoload del runtime + `.gitignore`) |
| `tools/argos_gut.py` · `argos_history.py` · `argos_record.py` · `argos_errors.py` | Pruebas GUT, historial de operaciones, grabación de trayectorias y catálogo de errores |
| `tests/gd/` | Tests en GDScript del plugin, del códec y del undo |

## Uso rápido

```bash
python tools/argos_install.py --project <proyecto>                  # instalar en un proyecto
python tools/argos_editor.py start <proyecto> [--windowed]          # editor (headless por defecto)
python tools/argos_cli.py --project <proyecto> status               # ping / status / tree / ...
python tools/argos_check.py <proyecto> --all                        # validar sintaxis
python tools/argos_cli.py --project <proyecto> rt-capture out.png   # juego en vivo -> PNG
python tools/argos_editor.py stop <proyecto>                        # detener
```

## Requisitos

- **Godot 4.x** (probado en 4.7)
- **Python 3.11+**

## Estado

**v1.0.0** — completo y probado: instalación, control del editor, CLI y servidor MCP, con su suite de pruebas en verde.
