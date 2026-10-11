@tool
extends EditorPlugin
## ARGOS — Portal Godot (v1.0.0): edición de escenas desde fuera del editor, con undo/redo, cierre seguro y propiedades veraces.
## Servidor WebSocket local (127.0.0.1) para control del editor desde Hermes.
## Protocolo: JSON {id, token, action, ...payload} -> {ok, id, result|error, detail}
## BLOQUE A-2: heartbeat en .argos/heartbeat cada <=30 s (distinguir «reimportando» de «editor muerto»).

# BLOQUE B5: codec PURO (parse_value/format_value/has_prop/try_set/find_node/dump_node).
# Sin estado del editor ni EditorInterface; testeable en headless (GUT).
const Codec := preload("res://addons/argos/argos_codec.gd")

const PORT_FIRST := 9080
const PORT_LAST := 9090
const TOKEN_FILE := "res://.argos/token"
const PORT_FILE := "res://.argos/port"
const HEARTBEAT_FILE := "res://.argos/heartbeat"
const HEARTBEAT_INTERVAL_MS := 25000  # <=30 s; umbral de frescura del servidor MCP: 90 s

# BLOQUE B1: limites (§3.1). Errores EXACTOS: payload_too_large / too_many_props /
# prop_too_long / props_too_large / bad_size / bad_out / bad_path.
const MAX_MSG_BYTES := 8388608       # 8 MiB (guarda en _handle_message antes de JSON.parse)
const MAX_PEERS := 16                # hotfix M2: tope de conexiones simultaneas
# hotfix M2: tope del PRIMER mensaje (auth) de un peer sin autenticar. Cubre el mayor
# payload legitimo de un primer mensaje bajo los caps B1 (node.create props <= 256 KiB)
# y queda muy por debajo del buffer de entrada (16 MiB).
const MAX_AUTH_MSG_BYTES := 524288   # 512 KiB
# micro-fix MEDIA-1: un peer que no se autentica no puede inmovilizar un slot
# indefinidamente; se cierra si no autentica en AUTH_TIMEOUT_MS.
const AUTH_TIMEOUT_MS := 10000       # 10 s
# micro-fix M2.2: un peer que no COMPLETA el handshake WebSocket (TCP crudo,
# slowloris) nunca llega a STATE_OPEN, asi que el timeout de auth no lo tocaba y
# inmovilizaba slots. Se cierra si no pasa a STATE_OPEN en HANDSHAKE_TIMEOUT_MS.
const HANDSHAKE_TIMEOUT_MS := 10000  # 10 s
const MAX_PROPS := 128               # claves por node.create
const MAX_PROP_STR := 4096           # chars por valor string
const MAX_PROPS_BYTES := 262144      # 256 KiB de JSON de props
const TREE_MAX_NODES_DEFAULT := 2000
const TREE_MAX_NODES_CAP := 20000
const TREE_MAX_DEPTH_DEFAULT := 12
const TREE_MAX_DEPTH_CAP := 64
const CAPTURE_MIN := 16              # rango valido de w/h
const CAPTURE_MAX := 4096

# BLOQUE B3: undo grouping (action.batch). Feature AISLADA: los ops sueltos NO
# cambian su comportamiento; solo action.batch usa el ArgosActionBuffer.
const MAX_BATCH_OPS := 64
const BATCH_ALLOWED := ["node.create", "node.instantiate", "node.get_prop",
	"node.set_prop", "node.delete", "node.reparent"]

var _tcp: TCPServer
var _port := -1
var _token := ""
var _peers: Dictionary = {}
var _next_id := 1
var _prev_lowproc := false
var _last_hb := -1
# BLOQUE B3: vacio = modo normal; no vacio = action.batch en curso.
var _batch: Dictionary = {}

func _enter_tree() -> void:
	randomize()
	_ensure_credentials()
	# BLOQUE A-2: heartbeat inmediato al activarse + periodico desde _process
	_write_heartbeat()
	_last_hb = Time.get_ticks_msec()
	# Keepalive (Consejo COUNCIL-010): el editor no debe dormir _process al perder foco
	_prev_lowproc = OS.low_processor_usage_mode
	OS.low_processor_usage_mode = false
	print("[ARGOS] low_processor_usage_mode=false (keepalive; previo=", _prev_lowproc, ")")
	_start_server()

func _exit_tree() -> void:
	for pid in _peers.keys():
		_peers[pid]["ws"].close()
	_peers.clear()
	if _tcp:
		_tcp.stop()
		_tcp = null
	OS.low_processor_usage_mode = _prev_lowproc

func _process(_delta: float) -> void:
	# BLOQUE A-2: heartbeat barato (comparacion de ticks; escritura cada <=30 s)
	if Time.get_ticks_msec() - _last_hb >= HEARTBEAT_INTERVAL_MS:
		_last_hb = Time.get_ticks_msec()
		_write_heartbeat()
	if _tcp:
		while _tcp.is_connection_available():
			var conn := _tcp.take_connection()
			if conn:
				# Hotfix M2: tope de peers. Si se alcanza, se cierra la conexion
				# nueva con log claro (sin crashear) y no se le asigna buffer.
				if peers_full(_peers.size()):
					print("[ARGOS] conexion RECHAZADA: tope de peers (%d) alcanzado" % MAX_PEERS)
					conn.disconnect_from_host()
					continue
				var ws := WebSocketPeer.new()
				# B1: buffer de entrada por encima del cap logico para que la guarda
				# MAX_MSG_BYTES pueda responder payload_too_large (default 64 KiB).
				ws.inbound_buffer_size = MAX_MSG_BYTES * 2
				if ws.accept_stream(conn) == OK:
					_peers[_next_id] = {"ws": ws, "authed": false, "born_ms": Time.get_ticks_msec()}
					print("[ARGOS] conexion aceptada id=", _next_id)
					_next_id += 1
	var dead: Array = []
	var now_ms := Time.get_ticks_msec()
	for pid in _peers.keys():
		var ws: WebSocketPeer = _peers[pid]["ws"]
		ws.poll()
		var st := ws.get_ready_state()
		if st == WebSocketPeer.STATE_OPEN:
			while ws.get_available_packet_count() > 0:
				_handle_message(pid, ws.get_packet().get_string_from_utf8())
			# micro-fix MEDIA-1: cierra el peer que no autentico dentro del plazo.
			if _peers.has(pid) and not _peers[pid]["authed"] \
					and auth_expired(now_ms, int(_peers[pid].get("born_ms", now_ms))):
				print("[ARGOS] conexion cerrada: timeout de auth (%d ms)" % AUTH_TIMEOUT_MS)
				ws.close()
		elif st == WebSocketPeer.STATE_CLOSED:
			dead.append(pid)
		elif _peers.has(pid) and handshake_expired(now_ms, int(_peers[pid].get("born_ms", now_ms))):
			# micro-fix M2.2: TCP crudo que nunca completa el handshake WS.
			print("[ARGOS] conexion cerrada: timeout de handshake (%d ms)" % HANDSHAKE_TIMEOUT_MS)
			ws.close()
	for pid in dead:
		_peers.erase(pid)

func _ensure_credentials() -> void:
	var dir := DirAccess.open("res://")
	if dir and not dir.dir_exists(".argos"):
		dir.make_dir(".argos")
	if not FileAccess.file_exists("res://.argos/.gdignore"):
		var gd := FileAccess.open("res://.argos/.gdignore", FileAccess.WRITE)
		if gd:
			gd.store_string("")
	# Rotacion por sesion (COUNCIL-010): token nuevo en cada arranque del editor
	_token = "%08x%08x%08x%08x" % [randi(), randi(), randi(), randi()]
	var ft := FileAccess.open(TOKEN_FILE, FileAccess.WRITE)
	if ft:
		ft.store_string(_token)

func _write_heartbeat() -> void:
	## BLOQUE A-2: senal de vida en disco para el MCP. Fail-silent: ningun
	## error aqui debe crashear el editor (push_warning como maximo).
	var dir := DirAccess.open("res://")
	if dir and not dir.dir_exists(".argos"):
		dir.make_dir(".argos")
	var f := FileAccess.open(HEARTBEAT_FILE, FileAccess.WRITE)
	if f == null:
		push_warning("[ARGOS] heartbeat: no escribible " + HEARTBEAT_FILE)
		return
	f.store_string(JSON.stringify({"ts": int(Time.get_unix_time_from_system() * 1000.0), "pid": OS.get_process_id()}))
	f.flush()
	f.close()

func _start_server() -> void:
	for p in range(PORT_FIRST, PORT_LAST + 1):
		var srv := TCPServer.new()
		if srv.listen(p, "127.0.0.1") == OK:
			_tcp = srv
			_port = p
			break
	if _port < 0:
		push_error("[ARGOS] sin puerto libre en rango %d-%d" % [PORT_FIRST, PORT_LAST])
		return
	var fp := FileAccess.open(PORT_FILE, FileAccess.WRITE)
	if fp:
		fp.store_string(str(_port))
	print("[ARGOS] escuchando ws://127.0.0.1:%d (token en %s)" % [_port, TOKEN_FILE])

func _handle_message(pid: int, txt: String) -> void:
	if not _peers.has(pid):
		return
	# B3: action.batch es SINCRONO dentro de un mensaje; si al empezar otro mensaje
	# quedo estado, fue un fallo interno -> se RE-PORTA al cliente que lo pidio y se
	# limpia (micro-fix: antes se descartaba en silencio, sin respuesta).
	if not _batch.is_empty():
		var b_pid := int(_batch.get("pid", pid))
		var b_rid: Variant = _batch.get("rid", 0)
		push_warning("[ARGOS] estado de batch huerfano: se responde batch_interrupted")
		_batch = {}
		_send(b_pid, _err(b_rid, "batch_interrupted", "fallo interno durante action.batch"))
	var peer: Dictionary = _peers[pid]
	var nbytes := txt.to_utf8_buffer().size()
	if not peer["authed"]:
		# Hotfix M2 (auth-first): una conexion SIN autenticar no puede consumir el
		# buffer grande; su primer mensaje (con token) va acotado por auth_gate.
		var gate := auth_gate(nbytes)
		if not gate.is_empty():
			_send(pid, _err(0, "auth", "mensaje previo a auth demasiado grande (%d > %d)" % [nbytes, MAX_AUTH_MSG_BYTES]))
			peer["ws"].close()
			return
		var amsg: Variant = JSON.parse_string(txt)
		if typeof(amsg) != TYPE_DICTIONARY:
			# micro-fix LOW-1: un no-autenticado que manda basura se cierra
			# (no puede mantener el slot vivo con mensajes invalidos).
			_send(pid, _err(0, "bad_json"))
			peer["ws"].close()
			return
		if str(amsg.get("token", "")) != _token:
			_send(pid, _err(amsg.get("id", 0), "auth", "token invalido"))
			peer["ws"].close()
			return
		peer["authed"] = true
		_dispatch(pid, amsg)
		return
	# Peer autenticado: B1 §3.1 corta payloads gigantes ANTES de parsear (evita OOM).
	if not payload_gate(nbytes).is_empty():
		_send(pid, _err(0, "payload_too_large", "%d > %d bytes" % [nbytes, MAX_MSG_BYTES]))
		return
	var msg: Variant = JSON.parse_string(txt)
	if typeof(msg) != TYPE_DICTIONARY:
		_send(pid, _err(0, "bad_json"))
		return
	_dispatch(pid, msg)

func _dispatch(pid: int, msg: Dictionary) -> void:
	var rid = msg.get("id", 0)
	var action := str(msg.get("action", ""))
	match action:
		"ping":
			_send(pid, _ok(rid, {"pong": true, "ts": Time.get_unix_time_from_system()}))
		"status":
			_send(pid, _ok(rid, _status()))
		"scene.get_tree":
			_send(pid, _ok(rid, _scene_tree(msg)))
		"scene.open":
			var op_path := str(msg.get("path", ""))
			if not op_path.begins_with("res://"):
				_send(pid, _err(rid, "bad_path", "se espera res://..."))
			else:
				EditorInterface.open_scene_from_path(op_path)
				_send(pid, _ok(rid, {"opened": op_path}))
		"scene.save":
			EditorInterface.save_scene()
			_send(pid, _ok(rid, {"saved": true}))
		"scene.reload":
			var sroot := EditorInterface.get_edited_scene_root()
			if sroot:
				EditorInterface.reload_scene_from_path(sroot.scene_file_path)
			_send(pid, _ok(rid, {"reloaded": true}))
		"node.create":
			_node_create(pid, rid, msg)
		"node.instantiate":
			_node_instantiate(pid, rid, msg)
		"node.reparent":
			_node_reparent(pid, rid, msg)
		"scene.new":
			_scene_new(pid, rid, msg)
		"undo.undo":
			if not _batch.is_empty():
				_send(pid, _err(rid, "busy_batch", "undo no permitido dentro de action.batch"))
			else:
				_history().undo()
				_send(pid, _ok(rid, {"undone": true}))
		"undo.redo":
			if not _batch.is_empty():
				_send(pid, _err(rid, "busy_batch", "redo no permitido dentro de action.batch"))
			else:
				_history().redo()
				_send(pid, _ok(rid, {"redone": true}))
		"node.get_prop":
			_node_get_prop(pid, rid, msg)
		"node.set_prop":
			_node_set_prop(pid, rid, msg)
		"node.delete":
			_node_delete(pid, rid, msg)
		"action.batch":
			_action_batch(pid, rid, msg)
		"run.play_main":
			EditorInterface.play_main_scene()
			_send(pid, _ok(rid, {"playing": true}))
		"run.play_scene":
			EditorInterface.play_custom_scene(str(msg.get("path", "")))
			_send(pid, _ok(rid, {"playing": true}))
		"run.stop":
			EditorInterface.stop_playing_scene()
			_send(pid, _ok(rid, {"stopped": true}))
		"run.is_playing":
			_send(pid, _ok(rid, {"playing": EditorInterface.is_playing_scene()}))
		"capture.editor_2d":
			_capture_editor_2d(pid, rid, msg)
		"capture.scene":
			_capture_scene(pid, rid, msg)
		"fs.scan":
			var efs := EditorInterface.get_resource_filesystem()
			efs.scan()
			_send(pid, _ok(rid, {"scanning": efs.is_scanning()}))
		"fs.update_file":
			var up_path := str(msg.get("path", ""))
			if not up_path.begins_with("res://"):
				_send(pid, _err(rid, "bad_path", "se espera res://..."))
			else:
				var efs2 := EditorInterface.get_resource_filesystem()
				efs2.update_file(up_path)
				_send(pid, _ok(rid, {"updated": up_path}))
		"editor.quit":
			var force := bool(msg.get("force", false))
			var saved := false
			if not force and EditorInterface.has_method("save_all_scenes"):
				EditorInterface.save_all_scenes()
				saved = true
			_send(pid, _ok(rid, {"quitting": true, "saved": saved, "force": force}))
			_quit_editor.call_deferred()
		_:
			_send(pid, _err(rid, "unknown_action", action))

func _quit_editor() -> void:
	get_tree().quit()

# ---- helpers de escena ----

func _scene_tree(msg: Dictionary) -> Dictionary:
	var root := EditorInterface.get_edited_scene_root()
	var out := {"edited": null, "open_scenes": EditorInterface.get_open_scenes()}
	if root:
		# B1 §3.1: recorte acotado (default 2000 nodos / 12 niveles; cap 20000/64).
		var max_nodes := clampi(int(msg.get("max_nodes", TREE_MAX_NODES_DEFAULT)), 1, TREE_MAX_NODES_CAP)
		var max_depth := clampi(int(msg.get("max_depth", TREE_MAX_DEPTH_DEFAULT)), 0, TREE_MAX_DEPTH_CAP)
		var budget := {"count": 0, "max_nodes": max_nodes, "truncated": false}
		var tree := Codec.dump_node(root, root, budget, 0, max_depth)
		var edited := {"path": root.scene_file_path, "tree": tree}
		if budget["truncated"]:
			edited["truncated"] = true
		out["edited"] = edited
	return out

func _node_create(pid: int, rid: Variant, msg: Dictionary) -> void:
	var root := EditorInterface.get_edited_scene_root()
	if root == null:
		_send(pid, _err(rid, "no_scene", "no hay escena editada"))
		return
	var parent := Codec.find_node(root, str(msg.get("parent", "")))
	if parent == null:
		_send(pid, _err(rid, "no_parent", str(msg.get("parent", ""))))
		return
	var cls := str(msg.get("class", ""))
	if not ClassDB.class_exists(cls) or not ClassDB.can_instantiate(cls):
		_send(pid, _err(rid, "bad_class", cls))
		return
	var inst := ClassDB.instantiate(cls) as Node
	if inst == null:
		_send(pid, _err(rid, "not_a_node", cls))
		return
	var nm := str(msg.get("name", ""))
	if not nm.is_empty():
		inst.name = nm
	var applied: Array = []
	var skipped: Array = []
	var props = msg.get("props", {})
	if typeof(props) == TYPE_DICTIONARY:
		# B1 §3.1: caps de props (codigos EXACTOS). inst aun no esta en el arbol.
		var props_json := JSON.stringify(props)
		if props.size() > MAX_PROPS:
			inst.free()
			_send(pid, _err(rid, "too_many_props", "%d > %d" % [props.size(), MAX_PROPS]))
			return
		if props_json.to_utf8_buffer().size() > MAX_PROPS_BYTES:
			inst.free()
			_send(pid, _err(rid, "props_too_large", "%d > %d bytes" % [props_json.to_utf8_buffer().size(), MAX_PROPS_BYTES]))
			return
		for k in props.keys():
			if typeof(props[k]) == TYPE_STRING and String(props[k]).length() > MAX_PROP_STR:
				inst.free()
				_send(pid, _err(rid, "prop_too_long", str(k)))
				return
		for k in props.keys():
			if Codec.try_set(inst, str(k), props[k]):
				applied.append(str(k))
			else:
				skipped.append(str(k))
	var ur := _ur()
	var label := "ARGOS: crear %s" % String(inst.name)
	ur.create_action(label)
	ur.add_do_method(parent, "add_child", inst)
	ur.add_do_method(inst, "set_owner", root)
	ur.add_do_reference(inst)
	ur.add_undo_method(parent, "remove_child", inst)
	ur.commit_action()
	_send(pid, _ok(rid, {"created": String(root.get_path_to(inst)), "applied": applied, "skipped": skipped, "undo_action": label}))

func _node_get_prop(pid: int, rid: Variant, msg: Dictionary) -> void:
	var root := EditorInterface.get_edited_scene_root()
	if root == null:
		_send(pid, _err(rid, "no_scene"))
		return
	var n := Codec.find_node(root, str(msg.get("path", "")))
	if n == null:
		_send(pid, _err(rid, "no_node", str(msg.get("path", ""))))
		return
	_send(pid, _ok(rid, {"value": Codec.format_value(n.get(str(msg.get("prop", ""))))}))

func _node_set_prop(pid: int, rid: Variant, msg: Dictionary) -> void:
	var root := EditorInterface.get_edited_scene_root()
	if root == null:
		_send(pid, _err(rid, "no_scene"))
		return
	var n := Codec.find_node(root, str(msg.get("path", "")))
	if n == null:
		_send(pid, _err(rid, "no_node", str(msg.get("path", ""))))
		return
	var prop := str(msg.get("prop", ""))
	if not Codec.has_prop(n, prop):
		_send(pid, _err(rid, "no_prop", prop))
		return
	var new_val: Variant = Codec.parse_value(msg.get("value"))
	var old_val: Variant = n.get(prop)
	var ur := _ur()
	var label := "ARGOS: set %s.%s" % [String(n.name), prop]
	ur.create_action(label)
	ur.add_do_property(n, prop, new_val)
	ur.add_undo_property(n, prop, old_val)
	ur.commit_action()
	_send(pid, _ok(rid, {"set": true, "prop": prop, "undo_action": label}))

func _node_delete(pid: int, rid: Variant, msg: Dictionary) -> void:
	var root := EditorInterface.get_edited_scene_root()
	if root == null:
		_send(pid, _err(rid, "no_scene"))
		return
	var n := Codec.find_node(root, str(msg.get("path", "")))
	if n == null:
		_send(pid, _err(rid, "no_node", str(msg.get("path", ""))))
		return
	if n == root:
		_send(pid, _err(rid, "is_root", "no se borra la raiz"))
		return
	var parent := n.get_parent()
	if parent == null:
		_send(pid, _err(rid, "no_parent", str(msg.get("path", ""))))
		return
	var del_path := String(root.get_path_to(n))
	var del_class := n.get_class()
	print("[ARGOS] delete: %s (%s) (deshacible con undo)" % [del_path, del_class])
	var ur := _ur()
	var label := "ARGOS: borrar %s" % String(n.name)
	ur.create_action(label)
	ur.add_do_method(parent, "remove_child", n)
	# B3.1: el nodo se REMUEVE en el do -> la referencia de propiedad va en el
	# UNDO (idioma canonico del editor: SceneTreeDock::_delete_nodes usa
	# add_undo_reference). Con add_do_reference, undo restaura el nodo pero la
	# proxima accion ejecuta UndoRedo::discard_redo() -> memdelete(referencia)
	# -> el nodo (ya en el arbol) se libera/desaparece. Evidencia: undo_redo.cpp.
	ur.add_undo_reference(n)
	ur.add_undo_method(parent, "add_child", n)
	ur.add_undo_method(n, "set_owner", root)
	ur.commit_action()
	_send(pid, _ok(rid, {"deleted": del_path, "class": del_class, "undoable": true, "undo_action": label}))

func _node_instantiate(pid: int, rid: Variant, msg: Dictionary) -> void:
	var root := EditorInterface.get_edited_scene_root()
	if root == null:
		_send(pid, _err(rid, "no_scene"))
		return
	var parent := Codec.find_node(root, str(msg.get("parent", "")))
	if parent == null:
		_send(pid, _err(rid, "no_parent", str(msg.get("parent", ""))))
		return
	var scene_path := str(msg.get("scene", ""))
	var packed := load(scene_path)
	if packed == null or not (packed is PackedScene):
		_send(pid, _err(rid, "load_failed", scene_path))
		return
	var inst := (packed as PackedScene).instantiate()
	if inst == null:
		_send(pid, _err(rid, "instantiate_failed", scene_path))
		return
	var nm := str(msg.get("name", ""))
	if not nm.is_empty():
		inst.name = nm
	var owner_all := bool(msg.get("owner_all", true))
	var ur := _ur()
	var label := "ARGOS: instanciar %s" % scene_path.get_file()
	ur.create_action(label)
	ur.add_do_method(parent, "add_child", inst)
	ur.add_do_method(inst, "set_owner", root)
	if owner_all:
		ur.add_do_method(self, "_set_owner_recursive", inst, root)
	ur.add_do_reference(inst)
	ur.add_undo_method(parent, "remove_child", inst)
	ur.commit_action()
	_send(pid, _ok(rid, {"created": String(root.get_path_to(inst)), "scene": scene_path, "undo_action": label}))

func _node_reparent(pid: int, rid: Variant, msg: Dictionary) -> void:
	var root := EditorInterface.get_edited_scene_root()
	if root == null:
		_send(pid, _err(rid, "no_scene"))
		return
	var n := Codec.find_node(root, str(msg.get("path", "")))
	if n == null or n == root:
		_send(pid, _err(rid, "no_node", str(msg.get("path", ""))))
		return
	var new_parent := Codec.find_node(root, str(msg.get("parent", "")))
	if new_parent == null:
		_send(pid, _err(rid, "no_parent", str(msg.get("parent", ""))))
		return
	var old_parent := n.get_parent()
	if old_parent == null:
		_send(pid, _err(rid, "no_parent", "nodo sin padre"))
		return
	var keep := bool(msg.get("keep_transform", false))
	var ur := _ur()
	var label := "ARGOS: reparent %s" % String(n.name)
	ur.create_action(label)
	ur.add_do_method(n, "reparent", new_parent, keep)
	ur.add_do_method(n, "set_owner", root)
	ur.add_undo_method(n, "reparent", old_parent, keep)
	ur.add_undo_method(n, "set_owner", root)
	ur.commit_action()
	_send(pid, _ok(rid, {"moved": String(root.get_path_to(n)), "parent": str(msg.get("parent", "")), "undo_action": label}))

# ---- BLOQUE B3: undo grouping (action.batch) ---------------------------------
# Contrato §1.1: {action:"action.batch", name, on_error:"abort"|"continue", ops:[…]}
# -> {ok, result:{undo_action, ops:[…], stopped_at}} | {ok:false, error, detail, results}
# Feature AISLADA: solo se activa durante action.batch (via _ur/_send/buffer).

func _action_batch(pid: int, rid: Variant, msg: Dictionary) -> void:
	if not _batch.is_empty():
		_send(pid, _err(rid, "busy_batch", "action.batch anidado"))
		return
	var root := EditorInterface.get_edited_scene_root()
	if root == null:
		_send(pid, _err(rid, "no_scene", "no hay escena editada"))
		return
	var ops: Variant = msg.get("ops")
	if typeof(ops) != TYPE_ARRAY or (ops as Array).is_empty():
		_send(pid, _err(rid, "bad_ops", "se espera 'ops' no vacio"))
		return
	if ops.size() > MAX_BATCH_OPS:
		_send(pid, _err(rid, "too_many_ops", "%d > %d" % [ops.size(), MAX_BATCH_OPS]))
		return
	var on_error := str(msg.get("on_error", "abort"))
	if on_error.is_empty():
		on_error = "abort"
	if on_error != "abort" and on_error != "continue":
		_send(pid, _err(rid, "bad_on_error", on_error))
		return

	var batch_name := sanitize_batch_name(str(msg.get("name", "")))
	var label := "ARGOS: " + batch_name
	var buffer := ArgosActionBuffer.new()
	_batch = {"buffer": buffer, "results": [], "pid": pid, "rid": rid}

	var entries: Array = []
	var stopped_at: Variant = null
	var abort_code := ""
	var abort_detail := ""

	for i in ops.size():
		var op: Variant = ops[i]
		var op_action := ""
		var op_entry: Dictionary = {}
		var op_failed := false
		if typeof(op) != TYPE_DICTIONARY:
			op_failed = true
			op_entry = {"action": "", "ok": false, "error": "bad_op", "detail": "op %d no es objeto" % i}
		else:
			op_action = str(op.get("action", ""))
			if op_action == "action.batch" or op_action == "undo.undo" or op_action == "undo.redo":
				op_failed = true
				op_entry = {"action": op_action, "ok": false, "error": "busy_batch", "detail": "op no permitida en batch"}
			elif not (op_action in BATCH_ALLOWED):
				op_failed = true
				op_entry = {"action": op_action, "ok": false, "error": "not_batchable", "detail": op_action}
			else:
				var before: int = _batch["results"].size()
				_dispatch(pid, op)
				var after: int = _batch["results"].size()
				if after <= before:
					op_failed = true
					op_entry = {"action": op_action, "ok": false, "error": "no_response", "detail": op_action}
				else:
					var r: Dictionary = _batch["results"][after - 1]
					if bool(r.get("ok", false)):
						op_entry = {"action": op_action, "ok": true, "result": r.get("result")}
					else:
						op_failed = true
						op_entry = {"action": op_action, "ok": false,
							"error": str(r.get("error", "error")), "detail": str(r.get("detail", ""))}
		entries.append(op_entry)
		if op_failed:
			if on_error == "abort":
				abort_code = str(op_entry.get("error", "error"))
				abort_detail = str(op_entry.get("detail", ""))
				break
			elif stopped_at == null:
				stopped_at = i

	_batch = {}

	if not abort_code.is_empty():
		# §1.1 abort: revierte lo aplicado y NO registra accion de undo.
		buffer.rollback()
		_send(pid, {"ok": false, "id": rid, "error": abort_code, "detail": abort_detail, "results": entries})
		return

	# §1.2: replay a UNA accion real; commit(false) registra sin re-ejecutar el do.
	if buffer.has_commands():
		buffer.replay_into(EditorInterface.get_editor_undo_redo(), root, label)
		_send(pid, _ok(rid, {"undo_action": label, "ops": entries, "stopped_at": stopped_at}))
	else:
		_send(pid, _ok(rid, {"undo_action": null, "ops": entries, "stopped_at": stopped_at}))

static func sanitize_batch_name(raw: String) -> String:
	## §1.4: strip; colapsa espacios; elimina control/`"`; cap 80; fallback.
	var out := ""
	var prev_space := false
	for i in raw.length():
		var ch := raw[i]
		var cc := ch.unicode_at(0)
		if cc < 32:                       # control, saltos, tabs -> espacio
			if not prev_space:
				out += " "
				prev_space = true
			continue
		if ch == '"':                     # comilla: se elimina (sin espacio)
			continue
		if ch == " ":
			if not prev_space:
				out += " "
				prev_space = true
			continue
		out += ch
		prev_space = false
	out = out.strip_edges()
	if out.length() > 80:
		out = out.substr(0, 80).strip_edges()
	return out if not out.is_empty() else "operacion"

func _scene_new(pid: int, rid: Variant, msg: Dictionary) -> void:
	var path := str(msg.get("path", ""))
	if not path.begins_with("res://"):
		_send(pid, _err(rid, "bad_path", "se espera path res://..."))
		return
	if FileAccess.file_exists(path):
		_send(pid, _err(rid, "exists", path))
		return
	var cls := str(msg.get("root_class", "Node2D"))
	if not ClassDB.class_exists(cls) or not ClassDB.can_instantiate(cls):
		_send(pid, _err(rid, "bad_class", cls))
		return
	var new_root := ClassDB.instantiate(cls) as Node
	if new_root == null:
		_send(pid, _err(rid, "not_a_node", cls))
		return
	var nm := str(msg.get("name", ""))
	if nm.is_empty():
		nm = path.get_file().get_basename().capitalize()
	new_root.name = nm
	var ps := PackedScene.new()
	var err := ps.pack(new_root)
	if err != OK:
		new_root.free()
		_send(pid, _err(rid, "pack_failed", str(err)))
		return
	err = ResourceSaver.save(ps, path)
	new_root.free()
	if err != OK:
		_send(pid, _err(rid, "save_failed", str(err)))
		return
	var efs := EditorInterface.get_resource_filesystem()
	efs.update_file(path)
	EditorInterface.open_scene_from_path(path)
	_send(pid, _ok(rid, {"created": path, "root": nm, "opened": true}))

func _set_owner_recursive(n: Node, ow: Node) -> void:
	for c in n.get_children(false):
		c.owner = ow
		_set_owner_recursive(c, ow)

func _ur() -> Variant:
	## BLOQUE B3: en modo batch devuelve el ArgosActionBuffer (misma interfaz);
	## fuera de batch, el EditorUndoRedoManager real (comportamiento intacto).
	## Variant: los handlers usan llamadas dinamicas equivalentes en ambos modos.
	if not _batch.is_empty():
		return _batch["buffer"]
	return EditorInterface.get_editor_undo_redo()

func _history() -> UndoRedo:
	## B3: undo/redo SIEMPRE usan el manager real (nunca el buffer).
	var eur := EditorInterface.get_editor_undo_redo()
	var eroot := EditorInterface.get_edited_scene_root()
	if eroot == null:
		return eur.get_history_undo_redo(EditorUndoRedoManager.GLOBAL_HISTORY)
	return eur.get_history_undo_redo(eur.get_object_history_id(eroot))

# ---- captura ----

func _capture_scene(pid: int, rid: Variant, msg: Dictionary) -> void:
	# B1: la validación de entrada precede al chequeo de entorno (errores estables
	# en headless: bad_out/bad_size en vez de headless).
	var out_path := str(msg.get("out", ""))
	if not out_path.to_lower().ends_with(".png"):
		_send(pid, _err(rid, "bad_out", "se espera ruta .png"))
		return
	var w := int(msg.get("w", 1152))
	var h := int(msg.get("h", 648))
	if w < CAPTURE_MIN or w > CAPTURE_MAX or h < CAPTURE_MIN or h > CAPTURE_MAX:
		_send(pid, _err(rid, "bad_size", "%dx%d (rango %d..%d)" % [w, h, CAPTURE_MIN, CAPTURE_MAX]))
		return
	if DisplayServer.get_name() == "headless":
		_send(pid, _err(rid, "headless", "captura requiere editor CON VENTANA (no headless)"))
		return
	var scene_path := str(msg.get("path", ""))
	if scene_path.is_empty():
		var eroot := EditorInterface.get_edited_scene_root()
		if eroot != null:
			scene_path = eroot.scene_file_path
	if scene_path.is_empty():
		_send(pid, _err(rid, "no_scene", "sin 'path' y sin escena editada"))
		return
	var packed := load(scene_path)
	if packed == null or not (packed is PackedScene):
		_send(pid, _err(rid, "load_failed", scene_path))
		return
	var inst := (packed as PackedScene).instantiate()
	if inst == null:
		_send(pid, _err(rid, "instantiate_failed", scene_path))
		return
	var sub := SubViewport.new()
	sub.size = Vector2i(w, h)
	sub.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	sub.add_child(inst)
	add_child(sub)
	var t0 := Time.get_ticks_msec()
	for i in 3:
		await get_tree().process_frame
	print("[ARGOS] capture.scene: 3 frames en %d ms" % (Time.get_ticks_msec() - t0))
	var tex := sub.get_texture()
	var img: Image = null
	if tex != null:
		img = tex.get_image()
	sub.queue_free()
	if img == null or img.get_width() == 0:
		_send(pid, _err(rid, "empty_image", "sin render (ventana minimizada?)"))
		return
	var iw := img.get_width()
	var ih := img.get_height()
	var err := img.save_png(out_path)
	if err != OK:
		_send(pid, _err(rid, "save_failed", str(err)))
		return
	_send(pid, _ok(rid, {"path": out_path, "w": iw, "h": ih, "scene": scene_path}))

func _capture_editor_2d(pid: int, rid: Variant, msg: Dictionary) -> void:
	var out_path := str(msg.get("path", ""))
	if not out_path.to_lower().ends_with(".png"):
		_send(pid, _err(rid, "bad_out", "se espera ruta .png"))
		return
	if DisplayServer.get_name() == "headless":
		_send(pid, _err(rid, "headless", "captura requiere editor CON VENTANA (no headless)"))
		return
	if not EditorInterface.has_method("get_editor_viewport_2d"):
		_send(pid, _err(rid, "no_api", "EditorInterface.get_editor_viewport_2d no existe"))
		return
	var vp := EditorInterface.get_editor_viewport_2d()
	if vp == null:
		_send(pid, _err(rid, "no_viewport"))
		return
	var t0 := Time.get_ticks_msec()
	for i in 2:
		await get_tree().process_frame
	print("[ARGOS] capture.editor_2d: 2 frames en %d ms" % (Time.get_ticks_msec() - t0))
	var tex := vp.get_texture()
	if tex == null:
		_send(pid, _err(rid, "no_texture", "headless?"))
		return
	var img := tex.get_image()
	if img == null or img.get_width() == 0:
		_send(pid, _err(rid, "empty_image", "headless?"))
		return
	var err := img.save_png(out_path)
	if err != OK:
		_send(pid, _err(rid, "save_failed", str(err)))
		return
	_send(pid, _ok(rid, {"path": out_path, "w": img.get_width(), "h": img.get_height()}))

# ---- estado / transporte ----

func _status() -> Dictionary:
	var root := EditorInterface.get_edited_scene_root()
	var efs := EditorInterface.get_resource_filesystem()
	return {
		"godot": Engine.get_version_info(),
		"editor": Engine.is_editor_hint(),
		"project": ProjectSettings.globalize_path("res://"),
		"edited_scene": (root.scene_file_path if root != null else ""),
		"is_playing": EditorInterface.is_playing_scene(),
		"fs_scanning": efs.is_scanning(),
		"peers": _peers.size(),
		"port": _port,
	}

func _ok(rid: Variant, result: Variant) -> Dictionary:
	return {"ok": true, "id": rid, "result": result}

func _err(rid: Variant, code: String, detail: String = "") -> Dictionary:
	return {"ok": false, "id": rid, "error": code, "detail": detail}

func _send(pid: int, data: Dictionary) -> void:
	# BLOQUE B3: en modo batch las respuestas de cada handler se ACUMULAN; el
	# plugin envia UNA sola respuesta combinada al terminar action.batch.
	if not _batch.is_empty():
		_batch["results"].append(data)
		return
	if not _peers.has(pid):
		return
	var ws: WebSocketPeer = _peers[pid]["ws"]
	if ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
		ws.send_text(JSON.stringify(data))

# ---- guards puros (hotfix M2) ----
# Funciones ESTATICAS y sin dependencias del editor: se pueden cargar y probar
# en headless (`load("res://addons/argos/argos_plugin.gd").peers_full(…)`).

static func peers_full(current_peers: int, cap: int = MAX_PEERS) -> bool:
	## True si ya hay `cap` conexiones simultaneas y debe rechazarse la nueva.
	return current_peers >= cap

static func auth_gate(nbytes: int) -> String:
	## Tope del primer mensaje (no autenticado). "" = pasa; si no, codigo.
	if nbytes > MAX_AUTH_MSG_BYTES:
		return "auth_too_large"
	return ""

static func payload_gate(nbytes: int) -> String:
	## Tope de payload para peers YA autenticados. "" = pasa; si no, codigo.
	if nbytes > MAX_MSG_BYTES:
		return "payload_too_large"
	return ""

static func auth_expired(now_ms: int, born_ms: int, timeout_ms: int = AUTH_TIMEOUT_MS) -> bool:
	## micro-fix MEDIA-1: True si un peer sin autenticar supero su plazo.
	return now_ms - born_ms >= timeout_ms

static func handshake_expired(now_ms: int, born_ms: int, timeout_ms: int = HANDSHAKE_TIMEOUT_MS) -> bool:
	## micro-fix M2.2: True si un peer no completo el handshake WS en su plazo.
	return now_ms - born_ms >= timeout_ms


# ---- BLOQUE B3: buffer de acciones (sin refactor de handlers) ----------------
# Expone la MISMA interfaz que EditorUndoRedoManager para los 5 handlers. Aplica
# el lado `do` al momento (para que los paths sigan siendo correctos) y GRABA los
# comandos para construir UNA sola accion real al final del batch.
class ArgosActionBuffer extends RefCounted:
	var _name := ""
	var _cmds: Array = []

	func create_action(name: Variant, _merge_mode: Variant = 0, _context: Variant = null,
			_backward: bool = false, _unsaved: bool = true) -> void:
		# §1.2: solo el primer nombre importa (la etiqueta real la fija el batch).
		if _name.is_empty():
			_name = str(name)

	func add_do_method(obj: Object, method: String, arg0: Variant = null,
			arg1: Variant = null, arg2: Variant = null) -> void:
		var args := _collect_args(arg0, arg1, arg2)
		_cmds.append({"kind": "do_method", "obj": obj, "method": method, "args": args})
		if obj != null and is_instance_valid(obj):
			obj.callv(method, args)   # aplica YA (los handlers leen el estado despues)

	func add_undo_method(obj: Object, method: String, arg0: Variant = null,
			arg1: Variant = null, arg2: Variant = null) -> void:
		_cmds.append({"kind": "undo_method", "obj": obj, "method": method,
			"args": _collect_args(arg0, arg1, arg2)})

	func add_do_property(obj: Object, prop: String, value: Variant) -> void:
		_cmds.append({"kind": "do_property", "obj": obj, "prop": prop, "value": value})
		if obj != null and is_instance_valid(obj):
			obj.set(prop, value)

	func add_undo_property(obj: Object, prop: String, value: Variant) -> void:
		_cmds.append({"kind": "undo_property", "obj": obj, "prop": prop, "value": value})

	func add_do_reference(obj: Variant) -> void:
		_cmds.append({"kind": "do_reference", "obj": obj})

	func add_undo_reference(obj: Variant) -> void:
		_cmds.append({"kind": "undo_reference", "obj": obj})

	func commit_action(_execute: bool = true) -> void:
		pass   # la accion real se commitea UNA vez al terminar el batch

	func has_commands() -> bool:
		return not _cmds.is_empty()

	func command_count() -> int:
		return _cmds.size()

	func first_name() -> String:
		return _name

	static func _collect_args(a0: Variant, a1: Variant, a2: Variant) -> Array:
		var out: Array = [a0, a1, a2]
		while not out.is_empty() and out[out.size() - 1] == null:
			out.pop_back()
		return out

	func replay_into(real: Variant, context: Variant = null, label: String = "") -> void:
		## Registra UNA accion real con los comandos grabados y commit(false):
		## no re-ejecuta el `do` (ya se aplico al grabar), evita duplicacion.
		var lname := label
		if lname.is_empty():
			lname = _name if not _name.is_empty() else "ARGOS: operacion"
		real.create_action(lname, UndoRedo.MERGE_DISABLE, context)
		for c in _cmds:
			var kind := str(c["kind"])
			match kind:
				"do_method":
					real.callv("add_do_method", [c["obj"], c["method"]] + c["args"])
				"undo_method":
					real.callv("add_undo_method", [c["obj"], c["method"]] + c["args"])
				"do_property":
					real.add_do_property(c["obj"], c["prop"], c["value"])
				"undo_property":
					real.add_undo_property(c["obj"], c["prop"], c["value"])
				"do_reference":
					real.add_do_reference(c["obj"])
				"undo_reference":
					real.add_undo_reference(c["obj"])
		real.commit_action(false)

	func rollback() -> void:
		## Abort: aplica los `undo` grabados en orden DIRECTO (paridad con el
		## UndoRedo real, que ejecuta `undo_ops` front->next en `undo_redo.cpp`) y
		## libera nodos creados por el batch que quedaron huerfanos (no registra
		## accion). NO usar orden inverso: el grupo de `node.delete` graba
		## `undo(add_child)` antes de `undo(set_owner)`; invertirlo ejecutaria
		## `set_owner` con el nodo detached -> "Invalid owner. Owner must be an
		## ancestor in the tree" y owner=NULL (sonda P4; ver
		## scratch/B3-sondas-resultado.md).
		for i in _cmds.size():
			var c: Dictionary = _cmds[i]
			var o: Variant = c["obj"]
			if o != null and is_instance_valid(o):
				match str(c["kind"]):
					"undo_method":
						o.callv(str(c["method"]), c["args"])
					"undo_property":
						o.set(str(c["prop"]), c["value"])
		_cleanup_created_orphans()
		_cmds.clear()

	func _cleanup_created_orphans() -> void:
		for c in _cmds:
			if str(c["kind"]) != "do_method" or str(c["method"]) != "add_child":
				continue
			var args: Array = c["args"]
			if args.is_empty():
				continue
			var child: Variant = args[0]
			if is_instance_valid(child) and child is Node and (child as Node).get_parent() == null:
				(child as Node).free()

