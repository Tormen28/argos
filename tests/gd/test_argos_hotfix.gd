extends GutTest
## B-Suite — tests de los guards PUROS del plugin ARGOS (hotfix M2).
##
## Estos guards viven como funciones estaticas en argos_plugin.gd, sin depender
## de EditorInterface, por lo que se pueden cargar y ejecutar en headless con GUT.
## Lo que dependa del editor (handlers con EditorInterface, captura, undo) NO va
## aqui: es integracion futura (ver scratch/BLOQUE-B-suite.md).

const Argos = preload("res://addons/argos/argos_plugin.gd")


# --- Caps documentados (B1 + hotfix M2) ---

func test_caps_hotfix_m2() -> void:
	assert_eq(Argos.MAX_PEERS, 16, "tope de peers simultaneos")
	assert_eq(Argos.MAX_AUTH_MSG_BYTES, 524288, "tope del primer mensaje (auth)")
	assert_eq(Argos.MAX_MSG_BYTES, 8388608, "tope de payload autenticado (8 MiB)")

func test_caps_b1_siguen_igual() -> void:
	assert_eq(Argos.MAX_PROPS, 128)
	assert_eq(Argos.MAX_PROP_STR, 4096)
	assert_eq(Argos.MAX_PROPS_BYTES, 262144)


# --- peers_full: tope de conexiones simultaneas ---

func test_peers_full_bajo_el_tope() -> void:
	assert_false(Argos.peers_full(0), "0 peers: acepta")
	assert_false(Argos.peers_full(15), "15 < 16: acepta")

func test_peers_full_en_y_sobre_el_tope() -> void:
	assert_true(Argos.peers_full(16), "16 == tope: rechaza")
	assert_true(Argos.peers_full(17), "17 > tope: rechaza")

func test_peers_full_tope_explicito() -> void:
	assert_false(Argos.peers_full(2, 3))
	assert_true(Argos.peers_full(3, 3))


# --- auth_gate: un peer sin autenticar no puede consumir payload grande ---

func test_auth_gate_acepta_hasta_el_tope() -> void:
	assert_eq(Argos.auth_gate(0), "")
	assert_eq(Argos.auth_gate(1), "")
	assert_eq(Argos.auth_gate(Argos.MAX_AUTH_MSG_BYTES), "", "frontera exacta: pasa")

func test_auth_gate_rechaza_sobre_el_tope() -> void:
	assert_eq(Argos.auth_gate(Argos.MAX_AUTH_MSG_BYTES + 1), "auth_too_large")

func test_auth_gate_rechaza_payload_de_16mib() -> void:
	# El escenario del hallazgo 2: un no-autenticado intenta consumir el buffer grande.
	assert_eq(Argos.auth_gate(16 * 1024 * 1024), "auth_too_large")


# --- payload_gate: peers YA autenticados ---

func test_payload_gate_frontera_8mib() -> void:
	assert_eq(Argos.payload_gate(Argos.MAX_MSG_BYTES), "", "8 MiB exactos: pasa")
	assert_eq(Argos.payload_gate(Argos.MAX_MSG_BYTES + 1), "payload_too_large")

func test_payload_gate_no_aplica_tope_de_auth() -> void:
	# Autenticado: 64 KiB+1 y < 8 MiB debe pasar (no se confunde con auth_gate).
	assert_eq(Argos.payload_gate(Argos.MAX_AUTH_MSG_BYTES + 1), "")


# --- auth_expired: cierre de no-autenticados (micro-fix MEDIA-1) ---

func test_cap_auth_timeout() -> void:
	assert_eq(Argos.AUTH_TIMEOUT_MS, 10000, "10 s sin autenticarse -> cierra")

func test_auth_expired_frontera() -> void:
	assert_false(Argos.auth_expired(0, 0), "recien nacido: no expira")
	assert_false(Argos.auth_expired(9999, 0), "bajo el plazo: no expira")
	assert_true(Argos.auth_expired(10000, 0), "frontera exacta: expira")
	assert_true(Argos.auth_expired(10001, 0), "sobre el plazo: expira")

func test_auth_expired_timeout_explicito() -> void:
	assert_false(Argos.auth_expired(1500, 1000, 1000), "500 < 1000: no")
	assert_true(Argos.auth_expired(2000, 1000, 1000), "1000 >= 1000: si")


# --- handshake_expired: cierre de peers sin handshake WS (micro-fix M2.2) ---

func test_cap_handshake_timeout() -> void:
	assert_eq(Argos.HANDSHAKE_TIMEOUT_MS, 10000, "10 s sin completar handshake -> cierra")

func test_handshake_expired_frontera() -> void:
	assert_false(Argos.handshake_expired(0, 0), "recien nacido: no expira")
	assert_false(Argos.handshake_expired(9999, 0), "bajo el plazo: no expira")
	assert_true(Argos.handshake_expired(10000, 0), "frontera exacta: expira")
	assert_true(Argos.handshake_expired(10001, 0), "sobre el plazo: expira")

func test_handshake_expired_timeout_explicito() -> void:
	assert_false(Argos.handshake_expired(1500, 1000, 1000), "500 < 1000: no")
	assert_true(Argos.handshake_expired(2000, 1000, 1000), "1000 >= 1000: si")
