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
