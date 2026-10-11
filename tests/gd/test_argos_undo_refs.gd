extends GutTest
## B3.1 — Caracterizacion de la semantica de referencias de UndoRedo (core).
##
## El bug `node.delete -> undo -> nueva accion` (nodo perdido) se debe a QUE lado
## guarda la referencia el handler de borrado:
##   * UndoRedo::discard_redo() libera los do_ops de la rama redo descartada
##     (Operation::delete_reference() -> memdelete(obj) para Nodes). Una referencia
##     en el lado `do` de un borrado muere al descartar el redo, AUNQUE el undo ya
##     haya devuelto el nodo al arbol -> el nodo (vivo, en el arbol) se libera.
##   * Una referencia en el lado `undo` sobrevive (ya no esta en do_ops). Es el
##     idioma canonico del editor (SceneTreeDock::_delete_nodes -> add_undo_reference).
##
## Corren headless con el `UndoRedo` del core; `EditorUndoRedoManager` (el que usa
## el plugin) delega exactamente en el, asi que la semantica es identica.

const Argos = preload("res://addons/argos/argos_plugin.gd")


func _delete_action(ur: UndoRedo, parent: Node, n: Node, use_undo_ref: bool) -> void:
	# Nota: el UndoRedo del core toma Callables; EditorUndoRedoManager (el que usa
	# el plugin) expone add_*_method(obj, method, ...) vararg, pero delega aqui.
	ur.create_action("ARGOS: borrar " + String(n.name))
	ur.add_do_method(parent.remove_child.bind(n))
	if use_undo_ref:
		ur.add_undo_reference(n)
	else:
		ur.add_do_reference(n)
	ur.add_undo_method(parent.add_child.bind(n))
	ur.commit_action()


func _nueva_accion(ur: UndoRedo, parent: Node) -> void:
	# Cualquier accion nueva ejecuta discard_redo() y descarta la rama redo.
	ur.create_action("nueva")
	ur.add_do_property(parent, "name", "renombrado")
	ur.add_undo_property(parent, "name", String(parent.name))
	ur.commit_action()


func test_add_do_reference_pierde_el_nodo_al_descartar_redo() -> void:
	var parent := Node2D.new()
	parent.name = "Parent"
	add_child_autofree(parent)
	var n := Node2D.new()
	n.name = "Victima"
	parent.add_child(n)

	var ur := UndoRedo.new()
	_delete_action(ur, parent, n, false)   # add_do_reference (el bug)
	ur.undo()
	assert_true(n.is_inside_tree(), "el undo devuelve el nodo al arbol")
	_nueva_accion(ur, parent)              # discard_redo

	# Documenta el TRAP del engine: el nodo en el arbol se libera.
	assert_false(is_instance_valid(n),
		"add_do_reference + discard_redo libera el nodo (por eso es incorrecto para delete)")
	ur.free()  # UndoRedo es Object (no RefCounted): sin free() fuga


func test_add_undo_reference_sobrevive_al_descartar_redo() -> void:
	var parent := Node2D.new()
	parent.name = "Parent"
	add_child_autofree(parent)
	var n := Node2D.new()
	n.name = "Viva"
	parent.add_child(n)

	var ur := UndoRedo.new()
	_delete_action(ur, parent, n, true)    # add_undo_reference (el fix)
	ur.undo()
	assert_true(n.is_inside_tree(), "el undo devuelve el nodo al arbol")
	_nueva_accion(ur, parent)              # discard_redo

	assert_true(is_instance_valid(n), "add_undo_reference preserva el nodo")
	assert_true(n.is_inside_tree(), "el nodo sigue en el arbol (no se libera)")
	ur.free()  # liberar el UndoRedo (Object, no RefCounted) para no fugar


func test_replay_del_buffer_usa_undo_reference_para_delete() -> void:
	# El buffer de B3 propaga el `add_undo_reference` del handler a la accion real.
	var parent := Node.new()
	add_child_autofree(parent)
	var child := Node.new()
	parent.add_child(child)
	var buf = Argos.ArgosActionBuffer.new()
	buf.create_action("ARGOS: borrar Child")
	buf.add_do_method(child, "set_meta", "x", 1)
	buf.add_undo_reference(child)
	buf.add_undo_method(child, "remove_meta", "x")

	var fake := _FakeUR.new()
	buf.replay_into(fake, null, "ARGOS: batch borrar")
	assert_true("undoref" in fake.log, "replay registra undo_reference")
	assert_false("doref" in fake.log, "replay NO registra do_reference en delete")


class _FakeUR:
	var log: Array = []
	func create_action(_n, _m = 0, _c = null, _b = false, _u = true) -> void:
		log.append("create")
	func add_do_method(_o, _m, _a = null, _b = null) -> void:
		log.append("do")
	func add_undo_method(_o, _m, _a = null, _b = null) -> void:
		log.append("undo")
	func add_do_property(_o, _p, _v) -> void:
		log.append("doprop")
	func add_undo_property(_o, _p, _v) -> void:
		log.append("undoprop")
	func add_do_reference(_o) -> void:
		log.append("doref")
	func add_undo_reference(_o) -> void:
		log.append("undoref")
	func commit_action(_e = true) -> void:
		log.append("commit")
