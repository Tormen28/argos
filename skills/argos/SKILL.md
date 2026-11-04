---
name: argos
description: Use when the user wants to inspect or edit a Godot 4 project through the live editor (scene tree, nodes, properties, play, screenshots) with the argos MCP tools.
---

# ARGOS - drive the Godot 4 editor

1. Install the addon once per project: `python <plugin>/tools/argos_install.py --project <project>`.
2. Start the editor (`argos_editor_launch`, or open it yourself). Check with `argos_ping` / `argos_status`.
3. Editing tools need an open scene. If none is open, pass `scene_path="res://..."`;
   otherwise ARGOS opens the project's main scene automatically, or returns a clear `no_scene` error.
4. Every edit goes through the editor's undo history: use `argos_undo` / `argos_redo` to revert.
5. Validate scripts with `argos_check` before running; save with `argos_scene_save`.
6. For visual QA use `argos_capture_scene` (editor with a window) and look at the PNG.
7. Only point ARGOS at projects you trust: running a scene executes the project's own GDScript.
