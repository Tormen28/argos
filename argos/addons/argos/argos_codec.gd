@tool
extends RefCounted
## ARGOS — codec PURO del plugin (BLOQUE B5).
##
## Funciones extraidas de `argos_plugin.gd`: dependen SOLO de `Node`/`Object` y
## tipos de Godot, NUNCA de `EditorInterface` ni del estado del plugin. Por eso
## se pueden cargar y probar en headless (GUT) y son las unicas extraibles sin
## riesgo de regresion (regla 3 de B5).
##
## Comportamiento IDENTICO al pre-extraccion: mismo algoritmo, mismas firmas
## (salvo el prefijo `_`, que era una convencion interna del plugin).
## Se usa via `const Codec := preload("res://addons/argos/argos_codec.gd")`.

# --- conversion de valores (params/valores del protocolo) ---------------------

static func parse_value(v: Variant) -> Variant:
	## Param del protocolo string -> tipo Godot. `@vec2:x,y` / `@color:r,g,b,a`.
	## NOTA B5: se evita el nombre `convert` porque colisiona con el built-in
	## `Object.convert(value, type)` y rompe la compilacion del modulo.
	if typeof(v) == TYPE_STRING:
		var s: String = v
		if s.begins_with("@vec2:"):
			var parts := s.substr(6).split(",")
			if parts.size() == 2:
				return Vector2(parts[0].to_float(), parts[1].to_float())
		elif s.begins_with("@color:"):
			var cp := s.substr(7).split(",")
			if cp.size() == 4:
				return Color(cp[0].to_float(), cp[1].to_float(), cp[2].to_float(), cp[3].to_float())
	return v


static func format_value(v: Variant) -> Variant:
	## Valor Godot -> JSON-safe string tipado (`@vec2:` / `@vec2i:` / `@color:`).
	## Se evita `to_json` para no arriesgar un built-in homonimo.
	match typeof(v):
		TYPE_NIL:
			return null
		TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING:
			return v
		TYPE_VECTOR2:
			return "@vec2:%s,%s" % [v.x, v.y]
		TYPE_VECTOR2I:
			return "@vec2i:%s,%s" % [v.x, v.y]
		TYPE_COLOR:
			return "@color:%s,%s,%s,%s" % [v.r, v.g, v.b, v.a]
		_:
			return str(v)


# --- propiedades --------------------------------------------------------------

static func has_prop(n: Object, prop: String) -> bool:
	for p in n.get_property_list():
		if str(p.get("name", "")) == prop:
			return true
	return false


static func try_set(n: Object, prop: String, v: Variant) -> bool:
	if not has_prop(n, prop):
		return false
	n.set(prop, parse_value(v))
	return true


# --- nodos --------------------------------------------------------------------

static func find_node(root: Node, rel: String) -> Node:
	if rel.is_empty() or rel == ".":
		return root
	return root.get_node_or_null(NodePath(rel))


static func dump_node(n: Node, root: Node, budget: Dictionary, depth: int, max_depth: int) -> Dictionary:
	budget["count"] += 1
	var d := {"name": String(n.name), "type": n.get_class(), "rel": String(root.get_path_to(n))}
	var s: Script = n.get_script()
	if s != null and not String(s.resource_path).is_empty():
		d["script"] = s.resource_path
	if n is Node2D:
		d["pos"] = "@vec2:%s,%s" % [n.position.x, n.position.y]
	if depth >= max_depth:
		if n.get_child_count(false) > 0:
			budget["truncated"] = true
		return d
	var kids: Array = []
	for c in n.get_children(false):
		if budget["count"] >= int(budget["max_nodes"]):
			budget["truncated"] = true
			break
		kids.append(dump_node(c, root, budget, depth + 1, max_depth))
	if not kids.is_empty():
		d["children"] = kids
	return d


# --- capturas (PNG) -----------------------------------------------------------
# Solo res://...png o .argos/captures/...png. Se rechazan rutas absolutas y
# cualquier segmento "..", y no se pisa un archivo existente.

static func check_capture_out(out_path: String) -> Dictionary:
	## "" en error si la ruta es escribible. Si no: bad_out o file_exists.
	## res_path es la ruta res:// canonica a la que hay que guardar.
	var raw := out_path.strip_edges()
	if raw.is_empty() or not raw.to_lower().ends_with(".png"):
		return {"error": "bad_out", "detail": "expected a .png path", "res_path": ""}
	if raw.contains(char(0)):
		return {"error": "bad_out", "detail": "path contains NUL", "res_path": ""}
	var norm := raw.replace("\\", "/")
	if norm.split("/").has(".."):
		return {"error": "bad_out", "detail": "path must not contain '..'", "res_path": ""}
	var res_path := ""
	if norm.begins_with("res://"):
		var rest := norm.substr(6)
		if rest.begins_with("/") or _capture_path_is_absolute(rest):
			return {"error": "bad_out", "detail": "absolute paths are not allowed", "res_path": ""}
		res_path = "res://" + _collapse_capture_rel(rest)
	else:
		if _capture_path_is_absolute(norm):
			return {"error": "bad_out", "detail": "absolute paths are not allowed", "res_path": ""}
		var rel := norm
		while rel.begins_with("./"):
			rel = rel.substr(2)
		rel = _collapse_capture_rel(rel)
		if not rel.begins_with(".argos/captures/") or rel.ends_with("/"):
			return {"error": "bad_out", "detail": "PNG must be under .argos/captures or res://", "res_path": ""}
		res_path = "res://" + rel
	if not res_path.to_lower().ends_with(".png") or res_path.ends_with("/"):
		return {"error": "bad_out", "detail": "expected a .png file", "res_path": ""}
	if FileAccess.file_exists(res_path):
		return {"error": "file_exists", "detail": "refusing to overwrite an existing file", "res_path": ""}
	var global_path := ProjectSettings.globalize_path(res_path)
	if DirAccess.dir_exists_absolute(global_path):
		return {"error": "file_exists", "detail": "refusing to overwrite an existing file", "res_path": ""}
	return {"error": "", "detail": "", "res_path": res_path}


static func prepare_capture_dir(res_path: String) -> void:
	var parent := res_path.get_base_dir()
	if parent.is_empty():
		return
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(parent))


static func _collapse_capture_rel(rel: String) -> String:
	var kept := PackedStringArray()
	for seg in rel.split("/"):
		if seg.is_empty() or seg == ".":
			continue
		kept.append(seg)
	return "/".join(kept)


static func _capture_path_is_absolute(norm: String) -> bool:
	if norm.begins_with("/") or norm.begins_with("//"):
		return true
	if norm.length() >= 3 and norm.substr(1, 2) == ":/" and _capture_is_drive_letter(norm.substr(0, 1)):
		return true
	return false


static func _capture_is_drive_letter(ch: String) -> bool:
	if ch.is_empty():
		return false
	var c := ch.unicode_at(0)
	return (c >= 65 and c <= 90) or (c >= 97 and c <= 122)
