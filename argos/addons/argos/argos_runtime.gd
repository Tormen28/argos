extends Node
## ARGOS Runtime v1.0.0 — servidor WebSocket DENTRO del juego para captura QA en vivo.
## Activo solo en debug builds o con el flag --argos (los exports release lo ignoran).
## Localhost + token compartido (.argos/token). Escribe .argos/runtime_port al arrancar.

const PORT_FIRST := 9091
const PORT_LAST := 9100

var _tcp: TCPServer
var _port := -1
var _token := ""
var _peers: Dictionary = {}
var _next_id := 1

func _ready() -> void:
	var active := OS.is_debug_build() or ("--argos" in OS.get_cmdline_args())
	if not active:
		set_process(false)
		return
	_load_token()
	_start_server()

func _load_token() -> void:
	if FileAccess.file_exists("res://.argos/token"):
		_token = FileAccess.get_file_as_string("res://.argos/token").strip_edges()

func _start_server() -> void:
	for p in range(PORT_FIRST, PORT_LAST + 1):
		var srv := TCPServer.new()
		if srv.listen(p, "127.0.0.1") == OK:
			_tcp = srv
			_port = p
			break
	if _port < 0:
		push_warning("[ARGOS-RT] sin puerto libre en %d-%d" % [PORT_FIRST, PORT_LAST])
		return
	var dir := DirAccess.open("res://")
	if dir and not dir.dir_exists(".argos"):
		dir.make_dir(".argos")
	var fp := FileAccess.open("res://.argos/runtime_port", FileAccess.WRITE)
	if fp:
		fp.store_string(str(_port))
	print("[ARGOS-RT] escuchando ws://127.0.0.1:%d (juego en vivo)" % _port)

func _exit_tree() -> void:
	if _tcp:
		_tcp.stop()
		_tcp = null

func _process(_delta: float) -> void:
	if _tcp == null:
		return
	while _tcp.is_connection_available():
		var conn := _tcp.take_connection()
		if conn:
			var ws := WebSocketPeer.new()
			if ws.accept_stream(conn) == OK:
				_peers[_next_id] = {"ws": ws, "authed": false}
				_next_id += 1
	var dead: Array = []
	for pid in _peers.keys():
		var ws: WebSocketPeer = _peers[pid]["ws"]
		ws.poll()
		var st := ws.get_ready_state()
		if st == WebSocketPeer.STATE_OPEN:
			while ws.get_available_packet_count() > 0:
				_handle(pid, ws.get_packet().get_string_from_utf8())
		elif st == WebSocketPeer.STATE_CLOSED:
			dead.append(pid)
	for pid in dead:
		_peers.erase(pid)

func _handle(pid: int, txt: String) -> void:
	if not _peers.has(pid):
		return
	var peer: Dictionary = _peers[pid]
	var msg = JSON.parse_string(txt)
	if typeof(msg) != TYPE_DICTIONARY:
		_send(pid, _err(0, "bad_json"))
		return
	var rid = msg.get("id", 0)
	if not peer["authed"]:
		if _token != "" and str(msg.get("token", "")) == _token:
			peer["authed"] = true
		else:
			_send(pid, _err(rid, "auth", "token invalido"))
			peer["ws"].close()
			return
	match str(msg.get("action", "")):
		"runtime.ping":
			_send(pid, _ok(rid, {"pong": true, "pid": OS.get_process_id()}))
		"runtime.info":
			var cs := get_tree().current_scene
			var wsz := DisplayServer.window_get_size()
			_send(pid, _ok(rid, {
				"godot": Engine.get_version_info()["string"],
				"fps": Engine.get_frames_per_second(),
				"frames": Engine.get_frames_drawn(),
				"scene": (cs.scene_file_path if cs != null else ""),
				"window": [wsz.x, wsz.y],
				"headless": DisplayServer.get_name() == "headless",
			}))
		"runtime.capture":
			_capture(pid, rid, msg)
		_:
			_send(pid, _err(rid, "unknown_action"))

func _capture(pid: int, rid: Variant, msg: Dictionary) -> void:
	var out_path := str(msg.get("out", ""))
	if not out_path.to_lower().ends_with(".png"):
		_send(pid, _err(rid, "bad_out", "se espera ruta .png"))
		return
	if DisplayServer.get_name() == "headless":
		_send(pid, _err(rid, "headless", "corre el juego CON ventana para capturar"))
		return
	var frames := clampi(int(msg.get("wait_frames", 2)), 1, 120)  # B1: clamp 1..120
	for i in range(frames):
		await get_tree().process_frame
	var img := get_viewport().get_texture().get_image()
	if img == null or img.get_width() == 0:
		_send(pid, _err(rid, "empty_image"))
		return
	var err := img.save_png(out_path)
	if err != OK:
		_send(pid, _err(rid, "save_failed", str(err)))
		return
	_send(pid, _ok(rid, {"path": out_path, "w": img.get_width(), "h": img.get_height(), "fps": Engine.get_frames_per_second()}))

func _ok(rid: Variant, result: Variant) -> Dictionary:
	return {"ok": true, "id": rid, "result": result}

func _err(rid: Variant, code: String, detail: String = "") -> Dictionary:
	return {"ok": false, "id": rid, "error": code, "detail": detail}

func _send(pid: int, data: Dictionary) -> void:
	if not _peers.has(pid):
		return
	var ws: WebSocketPeer = _peers[pid]["ws"]
	if ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
		ws.send_text(JSON.stringify(data))
