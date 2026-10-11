extends GutTest
## B-Suite — tests de la logica PURA del undo grouping (BLOQUE B3).
##
## `ArgosActionBuffer` y `sanitize_batch_name` no dependen del editor real: el
## buffer aplica el lado `do` sobre Nodes reales y el replay se hace contra un
## FakeUR (misma interfaz que EditorUndoRedoManager). Cubre los puntos del spike
## §1.3 (aplicar+replay sin duplicar, rollback consistente) sin abrir editor.

const Argos = preload("res://addons/argos/argos_plugin.gd")


## Replica minima de la interfaz de EditorUndoRedoManager usada por el buffer.
class FakeUR:
	var action_name := ""
	var committed = null
	var context = null
	var log: Array = []

	func create_action(name, _merge_mode = 0, ctx = null, _backward = false, _unsaved = true) -> void:
		action_name = name
		context = ctx
		log.append("create")

	func add_do_method(_obj, method, _a = null, _b = null) -> void:
		log.append("do:" + str(method))

	func add_undo_method(_obj, method, _a = null, _b = null) -> void:
		log.append("undo:" + str(method))

	func add_do_property(_obj, prop, _value) -> void:
		log.append("doprop:" + str(prop))

	func add_undo_property(_obj, prop, _value) -> void:
		log.append("undoprop:" + str(prop))

	func add_do_reference(_obj) -> void:
		log.append("doref")

	func add_undo_reference(_obj) -> void:
		log.append("undoref")

	func commit_action(execute = true) -> void:
		committed = execute
		log.append("commit")


# --- caps / contrato §1.1-1.4 ---

func test_cap_max_batch_ops() -> void:
	assert_eq(Argos.MAX_BATCH_OPS, 64, "tope duro de ops por batch")

func test_batch_allowed_solo_node() -> void:
	assert_true("node.create" in Argos.BATCH_ALLOWED)
	assert_true("node.set_prop" in Argos.BATCH_ALLOWED)
	assert_false("scene.new" in Argos.BATCH_ALLOWED, "scene.new no es batchable")
	assert_false("undo.undo" in Argos.BATCH_ALLOWED, "undo prohibido en batch")


# --- saneado de nombre (§1.4) ---

func test_sanitize_colapsa_espacios() -> void:
	assert_eq(Argos.sanitize_batch_name("  construir   enemigo  "), "construir enemigo")
	assert_eq(Argos.sanitize_batch_name("a\nb\tc"), "a b c")

func test_sanitize_elimina_comillas() -> void:
	assert_eq(Argos.sanitize_batch_name('x"y'), "xy")

func test_sanitize_fallback() -> void:
	assert_eq(Argos.sanitize_batch_name(""), "operacion")
	assert_eq(Argos.sanitize_batch_name("   "), "operacion")

func test_sanitize_cap_80() -> void:
	assert_eq(Argos.sanitize_batch_name("z".repeat(100)), "z".repeat(80))


# --- buffer: aplicar `do` + replay sin duplicar (spike §1.3 punto 2) ---

func test_buffer_aplica_do_una_vez_y_replay_no_duplica() -> void:
	var parent := Node.new()
	add_child_autofree(parent)
	var child := Node.new()
	var buf = Argos.ArgosActionBuffer.new()
	buf.create_action("ARGOS: crear Child")
	buf.add_do_method(parent, "add_child", child)
	buf.add_do_reference(child)
	buf.add_undo_method(parent, "remove_child", child)
	buf.commit_action()
	assert_eq(parent.get_child_count(), 1, "do aplicado una vez")
	assert_eq(child.get_parent(), parent)

	var fake := FakeUR.new()
	buf.replay_into(fake, null, "ARGOS: construir")
	assert_eq(parent.get_child_count(), 1, "el replay NO re-aplica add_child")
	assert_eq(fake.action_name, "ARGOS: construir")
	assert_eq(fake.committed, false, "commit(false): registra sin re-ejecutar")
	assert_true("do:add_child" in fake.log, "replay registra el do")
	assert_true("undo:remove_child" in fake.log, "replay registra el undo")
	assert_eq(fake.log[fake.log.size() - 1], "commit")


func test_buffer_guarda_primer_nombre() -> void:
	var buf = Argos.ArgosActionBuffer.new()
	buf.create_action("primero")
	buf.create_action("segundo")
	assert_eq(buf.first_name(), "primero")


# --- buffer: rollback (spike §1.3 punto 4) ---

func test_rollback_restaura_propiedad() -> void:
	var n := Node.new()
	n.name = "Viejo"
	add_child_autofree(n)
	var buf = Argos.ArgosActionBuffer.new()
	buf.create_action("ARGOS: set")
	buf.add_do_property(n, "name", "Nuevo")
	buf.add_undo_property(n, "name", "Viejo")
	buf.commit_action()
	assert_eq(String(n.name), "Nuevo", "do aplicado")
	buf.rollback()
	assert_eq(String(n.name), "Viejo", "rollback restaura la propiedad")
	assert_true(is_instance_valid(n), "nodo preexistente NO se libera")
	assert_eq(buf.command_count(), 0)


func test_rollback_libera_nodo_creado_huerfano() -> void:
	var parent := Node.new()
	add_child_autofree(parent)
	var child := Node.new()
	var buf = Argos.ArgosActionBuffer.new()
	buf.create_action("ARGOS: crear")
	buf.add_do_method(parent, "add_child", child)
	buf.add_do_reference(child)
	buf.add_undo_method(parent, "remove_child", child)
	buf.commit_action()
	assert_eq(parent.get_child_count(), 1)
	buf.rollback()
	assert_eq(parent.get_child_count(), 0, "rollback quita el hijo")
	assert_false(is_instance_valid(child), "nodo creado por el batch se libera (sin fuga)")


func test_rollback_restaura_estado_compuesto() -> void:
	var parent := Node.new()
	add_child_autofree(parent)
	var child := Node.new()
	var buf = Argos.ArgosActionBuffer.new()
	buf.create_action("ARGOS: compuesto")
	buf.add_do_method(parent, "add_child", child)
	buf.add_do_reference(child)
	buf.add_undo_method(parent, "remove_child", child)
	buf.add_do_property(child, "name", "Nuevo")
	buf.add_undo_property(child, "name", "Viejo")
	buf.commit_action()
	assert_eq(parent.get_child_count(), 1)
	assert_eq(String(child.name), "Nuevo")
	buf.rollback()
	assert_eq(parent.get_child_count(), 0, "estado inicial restaurado")


## --- rollback: orden DIRECTO (paridad con UndoRedo::undo; sonda P4) -----------

class _OrderSpy extends RefCounted:
	## Registra el orden de las llamadas; sin type hints en los args para admitir
	## objetos no-Node (el buffer usa callv con los objetos que se le pasen).
	var calls: Array
	func _init(calls_ref: Array) -> void:
		calls = calls_ref
	func remove_child(_n) -> void:
		calls.append("remove_child")
	func add_child(_n) -> void:
		calls.append("add_child")
	func set_owner(_n) -> void:
		calls.append("set_owner")


func test_rollback_delete_add_child_antes_de_set_owner() -> void:
	# Replica EXACTA del grupo de `_node_delete`:
	#   do(remove_child) + undo_reference + undo(add_child) + undo(set_owner)
	# El rollback debe aplicar los undo en orden DIRECTO (UndoRedo::undo ejecuta
	# undo_ops front->next). Con orden INVERSO, set_owner correria con el nodo
	# detached -> "Invalid owner" y owner perdido (sonda P4 / MEDIA confirmado).
	var calls: Array = []
	var parent := _OrderSpy.new(calls)
	var node := _OrderSpy.new(calls)
	var buf = Argos.ArgosActionBuffer.new()
	buf.create_action("ARGOS: borrar N")
	buf.add_do_method(parent, "remove_child", node)
	buf.add_undo_reference(node)
	buf.add_undo_method(parent, "add_child", node)
	buf.add_undo_method(node, "set_owner", parent)
	buf.commit_action()
	assert_eq(calls, ["remove_child"], "al grabar solo se aplica el lado do")
	buf.rollback()
	assert_eq(calls, ["remove_child", "add_child", "set_owner"],
		"rollback aplica undo en orden directo: add_child antes de set_owner")
