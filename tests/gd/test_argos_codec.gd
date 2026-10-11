extends GutTest
## B5 — tests del codec PURO extraido de `argos_plugin.gd` a `argos_codec.gd`.
##
## Escritos ANTES de la extraccion (TDD rojo->verde): caracterizan el
## comportamiento PREEXISTENTE de `_has_prop`, `_try_set`, `_find_node` y
## `_dump_node`. Ninguna de estas funciones depende de `EditorInterface` ni del
## estado del plugin, por eso son las unicas extraibles (regla 3 de B5).

const Codec = preload("res://addons/argos/argos_codec.gd")


# --- has_prop ----------------------------------------------------------------

func test_has_prop_true_en_propiedad_existente() -> void:
	var n := Node.new()
	add_child_autofree(n)
	assert_true(Codec.has_prop(n, "name"))
	assert_true(Codec.has_prop(n, "process_mode"))


func test_has_prop_false_en_inexistente() -> void:
	var n := Node.new()
	add_child_autofree(n)
	assert_false(Codec.has_prop(n, "position"), "position es de Node2D, no de Node")
	assert_false(Codec.has_prop(n, "no_existe_xyz"))


# --- try_set -----------------------------------------------------------------

func test_try_set_aplica_convert_y_retorna_true() -> void:
	var n := Node2D.new()
	add_child_autofree(n)
	assert_true(Codec.try_set(n, "position", "@vec2:3,4"))
	assert_eq(n.position, Vector2(3, 4), "try_set convierte el param (@vec2)")


func test_try_set_false_si_no_existe_y_no_aplica() -> void:
	var n := Node.new()
	add_child_autofree(n)
	assert_false(Codec.try_set(n, "no_existe_xyz", 123))


# --- find_node ---------------------------------------------------------------

func test_find_node_identidad_para_vacio_o_punto() -> void:
	var root := Node.new()
	add_child_autofree(root)
	assert_eq(Codec.find_node(root, ""), root)
	assert_eq(Codec.find_node(root, "."), root)


func test_find_node_relativo_y_anidado() -> void:
	var root := Node.new()
	root.name = "Root"
	add_child_autofree(root)
	var mid := Node.new()
	mid.name = "Mid"
	root.add_child(mid)
	var leaf := Node.new()
	leaf.name = "Leaf"
	mid.add_child(leaf)
	assert_eq(Codec.find_node(root, "Mid"), mid)
	assert_eq(Codec.find_node(root, "Mid/Leaf"), leaf)


func test_find_node_inexistente_es_null() -> void:
	var root := Node.new()
	add_child_autofree(root)
	assert_null(Codec.find_node(root, "Nope"))


# --- dump_node ---------------------------------------------------------------

func test_dump_node_estructura_pos_y_hijos() -> void:
	var root := Node2D.new()
	root.name = "Root"
	root.position = Vector2(1.5, -2.25)
	add_child_autofree(root)
	var child := Node2D.new()
	child.name = "Child"
	root.add_child(child)
	var budget := {"count": 0, "max_nodes": 100, "truncated": false}
	var d := Codec.dump_node(root, root, budget, 0, 12)
	assert_eq(d["name"], "Root")
	assert_eq(d["type"], "Node2D")
	assert_eq(d["rel"], ".")
	assert_eq(d["pos"], "@vec2:1.5,-2.25")
	assert_true(d.has("children"))
	assert_eq(d["children"].size(), 1)
	assert_eq(d["children"][0]["name"], "Child")
	assert_eq(d["children"][0]["rel"], "Child")
	assert_eq(d["children"][0]["type"], "Node2D")
	assert_false(budget["truncated"])
	assert_eq(budget["count"], 2, "el budget cuenta todos los nodos visitados")


func test_dump_node_sin_hijos_no_incluye_children() -> void:
	var n := Node.new()
	n.name = "Solo"
	add_child_autofree(n)
	var budget := {"count": 0, "max_nodes": 100, "truncated": false}
	var d := Codec.dump_node(n, n, budget, 0, 12)
	assert_false(d.has("children"))
	assert_false(budget["truncated"])


func test_dump_node_max_depth_trunca() -> void:
	var root := Node.new()
	root.name = "Root"
	add_child_autofree(root)
	root.add_child(Node.new())
	var budget := {"count": 0, "max_nodes": 100, "truncated": false}
	var d := Codec.dump_node(root, root, budget, 0, 0)
	assert_false(d.has("children"))
	assert_true(budget["truncated"], "profundidad maxima con hijos -> truncated")


func test_dump_node_max_nodes_trunca() -> void:
	var root := Node.new()
	root.name = "Root"
	add_child_autofree(root)
	for i in 3:
		root.add_child(Node.new())
	var budget := {"count": 0, "max_nodes": 2, "truncated": false}
	var d := Codec.dump_node(root, root, budget, 0, 12)
	assert_eq(d["children"].size(), 1, "root=1 + 1 hijo, el 3.er nodo corta")
	assert_true(budget["truncated"])


func test_dump_node_incluye_script_si_tiene() -> void:
	var n := Node.new()
	n.name = "ConScript"
	n.set_script(load("res://addons/argos/argos_runtime.gd"))
	autofree(n)  # no se mete al arbol: evita el _ready del runtime
	var budget := {"count": 0, "max_nodes": 100, "truncated": false}
	var d := Codec.dump_node(n, n, budget, 0, 12)
	assert_eq(d.get("script"), "res://addons/argos/argos_runtime.gd")
