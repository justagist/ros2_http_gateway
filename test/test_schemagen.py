"""JSON Schema tests.

The load-bearing test here is the agreement test: whatever the generated converter emits
for a populated message must validate against the generated read schema, and must be
accepted by the write schema. A schema that merely looks plausible is worse than none,
because it is what the OpenAPI document promises to clients.
"""

from __future__ import annotations

import pytest

# Must precede every ROS import, and doubles as a barrier the import sorter will not
# reorder across.
pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

import jsonschema
import orjson
from rosidl_runtime_py.utilities import get_message

from ros2_http_gateway.codegen import build_dict_to_msg, build_msg_to_dict
from ros2_http_gateway.schemagen import json_schema_for, schema_ref_name

from ros_fixtures import ALL_TYPES, populate

pytestmark = pytest.mark.ros

ORJSON_OPTS = orjson.OPT_SERIALIZE_NUMPY


def encoded(type_name: str, *, int64_as_string: bool = True) -> object:
    """Return what a GET on a populated message of this type would return."""
    cls = get_message(type_name)
    as_dict = build_msg_to_dict(cls, int64_as_string=int64_as_string)(populate(cls))
    return orjson.loads(orjson.dumps(as_dict, option=ORJSON_OPTS))


@pytest.mark.parametrize("type_name", ALL_TYPES)
@pytest.mark.parametrize("direction", ["read", "write"])
def test_generated_schema_is_a_valid_json_schema(type_name: str, direction: str) -> None:
    schema = json_schema_for(get_message(type_name), direction=direction)  # type: ignore[arg-type]
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_converter_output_validates_against_the_read_schema(type_name: str) -> None:
    schema = json_schema_for(get_message(type_name), direction="read")
    jsonschema.Draft202012Validator(schema).validate(encoded(type_name))


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_converter_output_is_accepted_by_the_write_schema(type_name: str) -> None:
    """A response body must be a legal request body; round-tripping depends on it."""
    schema = json_schema_for(get_message(type_name), direction="write")
    jsonschema.Draft202012Validator(schema).validate(encoded(type_name))


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_read_schema_without_int64_as_string(type_name: str) -> None:
    schema = json_schema_for(get_message(type_name), direction="read", int64_as_string=False)
    jsonschema.Draft202012Validator(schema).validate(encoded(type_name, int64_as_string=False))


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_write_schema_accepts_everything_the_decoder_accepts(type_name: str) -> None:
    """Anything the schema passes, the converter must build; here, the empty body."""
    cls = get_message(type_name)
    jsonschema.Draft202012Validator(json_schema_for(cls, direction="write")).validate({})
    assert build_dict_to_msg(cls)({}) is not None


# --------------------------------------------------------------------------------------
# specific encodings
# --------------------------------------------------------------------------------------


def test_read_schema_requires_every_field() -> None:
    schema = json_schema_for(get_message("geometry_msgs/msg/Twist"), direction="read")
    assert schema["required"] == ["linear", "angular"]


def test_write_schema_requires_nothing() -> None:
    schema = json_schema_for(get_message("geometry_msgs/msg/Twist"), direction="write")
    assert "required" not in schema


def test_unknown_fields_are_closed_off_in_both_directions() -> None:
    for direction in ("read", "write"):
        schema = json_schema_for(get_message("geometry_msgs/msg/Twist"), direction=direction)  # type: ignore[arg-type]
        assert schema["additionalProperties"] is False


def test_int64_is_a_string_on_read_and_either_on_write() -> None:
    cls = get_message("test_msgs/msg/BasicTypes")
    read = json_schema_for(cls, direction="read")["properties"]["int64_value"]
    write = json_schema_for(cls, direction="write")["properties"]["int64_value"]
    assert read["type"] == "string"
    assert set(write["type"]) == {"string", "integer"}


def test_int64_is_a_bounded_integer_when_the_flag_is_off() -> None:
    cls = get_message("test_msgs/msg/BasicTypes")
    field = json_schema_for(cls, direction="read", int64_as_string=False)["properties"][
        "int64_value"
    ]
    assert field == {"type": "integer", "minimum": -(2**63), "maximum": 2**63 - 1}


def test_smaller_integers_carry_their_range() -> None:
    properties = json_schema_for(get_message("test_msgs/msg/BasicTypes"))["properties"]
    assert properties["uint8_value"] == {"type": "integer", "minimum": 0, "maximum": 255}
    assert properties["int8_value"] == {"type": "integer", "minimum": -128, "maximum": 127}


def test_scalar_octet_is_a_byte_valued_integer() -> None:
    properties = json_schema_for(get_message("test_msgs/msg/BasicTypes"))["properties"]
    assert properties["byte_value"] == {"type": "integer", "minimum": 0, "maximum": 255}


def test_doubles_are_nullable_on_read_only() -> None:
    """Because NaN and the infinities are encoded as null."""
    cls = get_message("geometry_msgs/msg/Vector3")
    assert json_schema_for(cls, direction="read")["properties"]["x"]["type"] == ["number", "null"]
    assert json_schema_for(cls, direction="write")["properties"]["x"]["type"] == "number"


def test_byte_collections_are_base64_on_read_and_either_on_write() -> None:
    cls = get_message("sensor_msgs/msg/Image")
    read = json_schema_for(cls, direction="read")["properties"]["data"]
    assert read == {"type": "string", "contentEncoding": "base64"}

    write = json_schema_for(cls, direction="write")["properties"]["data"]
    assert write["oneOf"][0]["contentEncoding"] == "base64"
    assert write["oneOf"][1]["type"] == "array"


def test_fixed_size_array_pins_its_length() -> None:
    field = json_schema_for(get_message("sensor_msgs/msg/Imu"))["properties"][
        "orientation_covariance"
    ]
    assert (field["minItems"], field["maxItems"]) == (9, 9)


def test_bounded_sequence_carries_its_maximum_but_no_minimum() -> None:
    field = json_schema_for(get_message("test_msgs/msg/BoundedSequences"))["properties"][
        "float64_values"
    ]
    assert field["maxItems"] == 3
    assert "minItems" not in field


def test_unbounded_sequence_carries_no_bound() -> None:
    field = json_schema_for(get_message("sensor_msgs/msg/JointState"))["properties"]["position"]
    assert field["type"] == "array"
    assert "maxItems" not in field


def test_bounded_string_carries_its_length_limit() -> None:
    field = json_schema_for(get_message("test_msgs/msg/Strings"))["properties"][
        "bounded_string_value"
    ]
    assert field == {"type": "string", "maxLength": 22}


def test_nested_types_are_interned_once_and_referenced() -> None:
    schema = json_schema_for(get_message("geometry_msgs/msg/Twist"))
    key = schema_ref_name(get_message("geometry_msgs/msg/Vector3"))
    assert key == "geometry_msgs.msg.Vector3"
    assert schema["properties"]["linear"] == {"$ref": f"#/$defs/{key}"}
    assert schema["properties"]["angular"] == {"$ref": f"#/$defs/{key}"}
    assert list(schema["$defs"]) == [key]


def test_deeply_nested_type_stays_small_thanks_to_defs() -> None:
    schema = json_schema_for(get_message("test_msgs/msg/MultiNested"))
    assert len(orjson.dumps(schema)) < 20_000
    assert len(schema["$defs"]) >= 4


def test_title_names_the_interface() -> None:
    assert json_schema_for(get_message("sensor_msgs/msg/Imu"))["title"] == "sensor_msgs/msg/Imu"


def test_schema_rejects_a_field_that_the_converter_would_also_reject() -> None:
    cls = get_message("test_msgs/msg/BasicTypes")
    validator = jsonschema.Draft202012Validator(json_schema_for(cls, direction="write"))
    assert not validator.is_valid({"uint8_value": 300})
    assert not validator.is_valid({"int32_value": "not-a-number"})
    assert not validator.is_valid({"nope": 1})
