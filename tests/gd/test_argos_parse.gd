extends GutTest
## B-Suite — tests de PARSING puro del plugin ARGOS.
##
## `convert` (params @vec2:@color:) y `to_json` (valores -> string tipado) viven
## en `argos_codec.gd` (B5): funciones estaticas libres de estado del editor,
## testeables en headless.

const Codec = preload("res://addons/argos/argos_codec.gd")


func test_convert_vec2() -> void:
	assert_eq(Codec.parse_value("@vec2:1.5,2.5"), Vector2(1.5, 2.5))
	assert_eq(Codec.parse_value("@vec2:-3,4"), Vector2(-3, 4))

func test_convert_color() -> void:
	assert_eq(Codec.parse_value("@color:1,0,0,0.5"), Color(1, 0, 0, 0.5))

func test_convert_string_plana_pasa_tal_cual() -> void:
	assert_eq(Codec.parse_value("hola"), "hola")
	assert_eq(Codec.parse_value(""), "")

func test_convert_no_string_pasa_tal_cual() -> void:
	assert_eq(Codec.parse_value(42), 42)
	assert_eq(Codec.parse_value(true), true)

func test_convert_marcador_incompleto_no_convierte() -> void:
	# "@vec2:1" tiene una sola componente -> se devuelve sin convertir.
	assert_eq(Codec.parse_value("@vec2:1"), "@vec2:1")

func test_to_json_escalares() -> void:
	assert_eq(Codec.format_value(null), null)
	assert_eq(Codec.format_value(true), true)
	assert_eq(Codec.format_value(7), 7)
	assert_eq(Codec.format_value("x"), "x")

func test_to_json_tipados() -> void:
	# Vector2 guarda floats -> "%s" imprime "3.0,4.0" (comportamiento real).
	assert_eq(Codec.format_value(Vector2(3, 4)), "@vec2:3.0,4.0")
	assert_eq(Codec.format_value(Vector2i(3, 4)), "@vec2i:3,4")

func test_roundtrip_vec2() -> void:
	var v := Vector2(12.5, -7.25)
	assert_eq(Codec.parse_value(Codec.format_value(v)), v)
