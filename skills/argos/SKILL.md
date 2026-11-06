---
name: argos
description: Use when a Hermes god should inspect, build, animate, or visually QA a trusted Godot 4 project through the live editor with the argos MCP tools.
---

# ARGOS - safe operating manual for Hermes gods

ARGOS drives a **live Godot 4 editor**. Use it to assemble and QA a scene. Edit files on disk when you are changing scripts, resources, or project settings that the editor is not currently showing. Do not use ARGOS as a general filesystem tool.

The MCP server is marked **trust: untrusted**. Accept the Hermes trust prompt only for a project you already trust. Launch, check, and play run only projects you trust: playing a scene executes that project's GDScript.

## When to use ARGOS vs the filesystem

- Use ARGOS for the open scene: create nodes, set properties, batch those edits into one undo group, play animations, capture PNGs, and press play.
- Edit `.gd`, `.tscn` text, and `project.godot` on disk when the change is source, then call `argos_fs_scan` (and `argos_check` for scripts).
- Do not use `argos_raw` unless a first-class tool is missing. Prefer `argos_action_batch`, `argos_anim_list`, `argos_anim_get`, `argos_anim_play`, `argos_anim_stop`, and `argos_capture_editor_2d`.

## Allowlist (mandatory)

`argos_editor_launch`, `argos_check`, and `argos_run_play` accept only `ARGOS_PROJECT` or a directory listed in `ARGOS_ALLOWED_PROJECTS` (OS path separator). Other tools may attach to an editor that is already open; launch, check, and play will not start a project that is outside that list.

A refusal looks like `ARGOS: project_not_allowed` followed by JSON:

- `code`: `project_not_allowed`
- `project`: the path you tried
- `allowed`: the resolved allowlist, or the string `only ARGOS_PROJECT` when nothing is configured
- `hint`: how to set `ARGOS_PROJECT` or `ARGOS_ALLOWED_PROJECTS`

The error does not list the rest of the disk. Set the env var to the project root (the directory that contains `project.godot`) and retry. Do not hunt for other project paths.

## Capture paths (mandatory)

Screenshots are PNG only. Pass `out` as `res://....png` or `.argos/captures/....png`.

- Optional `session` must match `[A-Za-z0-9_-]{1,64}`. Files go under `.argos/captures/<session>/`.
- A bare file name is allowed only with `session` (`out: "shot.png"`, `session: "qa"` writes `.argos/captures/qa/shot.png`).
- If you omit the file name and pass `session`, ARGOS allocates the next free `frame_NNN.png` (`frame_001.png`, then `frame_002.png`, up to `frame_999.png`).
- Absolute paths and any `..` segment are rejected. An existing file is never overwritten.
- `session` does not apply to `res://` paths.
- There is no GIF or video export.

`argos_capture_scene` renders a scene (the editor needs a window). `argos_capture_editor_2d` captures the 2D editor viewport. `argos_runtime_capture` captures the running game (also needs a window).

## Happy path

1. Install once: `python <plugin>/tools/argos_install.py --project <project>`.
2. `argos_editor_launch` for an allowlisted project (windowed if you need captures). Confirm with `argos_ping` and `argos_status`.
3. `argos_scene_open` or `argos_scene_new`. If a later edit returns `no_scene`, pass `scene_path="res://..."`. ARGOS can also open the project's main scene once.
4. `argos_action_batch` to create nodes and set props as **one** undo group (`name`, `on_error: "abort"` or `"continue"`, max 64 ops). Allowed ops: `node.create`, `node.instantiate`, `node.get_prop`, `node.set_prop`, `node.delete`, `node.reparent`. Nested `action.batch` and undo/redo inside the batch are rejected (`busy_batch`). Anything else is `not_batchable`.
5. `argos_scene_save`.
6. `argos_anim_list` (read-only) to find `AnimationPlayer` / `AnimationTree` nodes. `argos_anim_get` (read-only) for names, length, loop mode, and track counts, or the tree's states. `argos_anim_play` to play one (`custom_speed`, `from_end`) or travel a state. `argos_anim_stop` stops a player and seeks to 0 (`seek_zero: false` keeps the pose) or deactivates a tree. Playback is not an undo step. These act on the **edited** scene, not the running game.
7. Capture a session: `argos_capture_editor_2d` or `argos_capture_scene` with `session`.
8. `argos_check` on the scripts you touched.
9. `argos_run_play`, then `argos_runtime_capture` into the same session, then `argos_run_stop`.
10. `argos_editor_stop` when you are finished with an editor this bridge launched.

`argos_undo` reverts one editor action. One undo reverts a whole successful batch.

## Failure recipes

| What you see | What to do |
| --- | --- |
| `project_not_allowed` | Point `ARGOS_PROJECT` or `ARGOS_ALLOWED_PROJECTS` at the trusted project root. Do not retry other directories. |
| `no_scene` | Pass `scene_path`, call `argos_scene_open`, or set `application/run/main_scene`. |
| `bad_out` / `bad_session` / `file_exists` | Use a `res://` PNG or `.argos/captures/...png`. Fix the session name. Pick a new leaf or omit it so `frame_NNN.png` advances. Do not overwrite. |
| `too_many_ops` | Split the batch. The cap is 64. |
| `not_batchable` / `busy_batch` | Keep only the allowed node ops. Do not nest a batch or call undo inside it. |
| `no_node` / `bad_type` / `no_anim` | `argos_anim_list` first. The path must be an `AnimationPlayer` or `AnimationTree`, and the animation or state name must exist. |
| `bad_speed` | `custom_speed` has to be a number. |
| Editor not up (`ping` fails, no heartbeat) | `argos_editor_launch` on the allowlisted project and wait. A refused launch did not start Godot. |
| `headless` on capture | Relaunch with a window (`headless: false`). |

## Español (resumen)

ARGOS controla un editor de Godot 4 en vivo. Trabaja solo con proyectos de confianza: `trust` es `untrusted`, y launch, check y play exigen `ARGOS_PROJECT` o `ARGOS_ALLOWED_PROJECTS`. Si llega `project_not_allowed`, configura esa lista; el error no enumera el disco. Las capturas son PNG en `res://` o `.argos/captures/`, con sesion opcional y `frame_NNN.png` si omites el nombre; no pisa archivos y no hay video. Prefiere `argos_action_batch`, las herramientas `argos_anim_*` y `argos_capture_editor_2d`. No uses `argos_raw` si ya existe una herramienta.
