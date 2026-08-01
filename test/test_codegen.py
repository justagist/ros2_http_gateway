"""Generated-converter tests: round-trip identity, encoding contract, golden source.

Marked ``ros`` throughout: code generation reads real interface definitions, so there is
nothing meaningful to test without a sourced ROS 2 environment.
"""

from __future__ import annotations

import base64
import itertools
from pathlib import Path

import pytest

# Must precede every ROS import, and doubles as a barrier the import sorter will not
# reorder across.
pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

import numpy
import orjson
from rclpy.serialization import deserialize_message, serialize_message
from rosidl_runtime_py.convert import message_to_ordereddict
from rosidl_runtime_py.utilities import get_message

from ros2_http_gateway.codegen import (
    ConversionError,
    build_dict_to_msg,
    build_msg_to_dict,
    dict_to_msg_source,
    msg_to_dict_source,
)

from ros_fixtures import (
    ALL_TYPES,
    BIG_INT64,
    declared_field_tree,
    has_fieldless_message,
    populate,
)

pytestmark = pytest.mark.ros

ORJSON_OPTS = orjson.OPT_SERIALIZE_NUMPY
GOLDEN_DIR = Path(__file__).parent / "golden"

# Definitions that have been stable for several distros, so a golden file is a regression
# guard rather than a distro tripwire.
GOLDEN_TYPES = [
    "builtin_interfaces/msg/Time",
    "geometry_msgs/msg/Twist",
    "std_msgs/msg/Header",
    "test_msgs/msg/Arrays",
    "test_msgs/msg/BoundedSequences",
]


def _json_round_trip(value: object) -> object:
    return orjson.loads(orjson.dumps(value, option=ORJSON_OPTS))


def assert_identical(cls: type, original: object, rebuilt: object) -> None:
    """Assert two messages are indistinguishable.

    Serialised bytes are the strongest comparison and are used wherever they are sound.
    They are not sound for a type tree containing a field-less message: rosidl gives those
    a hidden, uninitialised ``structure_needs_at_least_one_member`` byte, so two default
    instances can differ in a byte no field maps to. Those types are compared through
    their JSON encoding, and ``test_converter_covers_every_declared_field`` independently
    rules out a field that both converters skipped.
    """
    if has_fieldless_message(cls):
        to_dict = build_msg_to_dict(cls)
        assert orjson.dumps(to_dict(rebuilt), option=ORJSON_OPTS) == orjson.dumps(
            to_dict(original), option=ORJSON_OPTS
        )
    else:
        assert serialize_message(rebuilt) == serialize_message(original)


# --------------------------------------------------------------------------------------
# round-trip identity
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_default_message_round_trips(type_name: str) -> None:
    cls = get_message(type_name)
    to_dict, from_dict = build_msg_to_dict(cls), build_dict_to_msg(cls)
    original = cls()
    rebuilt = from_dict(_json_round_trip(to_dict(original)))
    assert_identical(cls, original, rebuilt)


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_populated_message_round_trips(type_name: str) -> None:
    """msg -> dict -> JSON -> dict -> msg must be the identity on the wire."""
    cls = get_message(type_name)
    to_dict, from_dict = build_msg_to_dict(cls), build_dict_to_msg(cls)
    original = populate(cls)
    rebuilt = from_dict(_json_round_trip(to_dict(original)))
    assert_identical(cls, original, rebuilt)


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_deserialised_message_encodes_identically(type_name: str) -> None:
    """The runtime converts messages that came off the wire, not ones built in Python."""
    cls = get_message(type_name)
    to_dict = build_msg_to_dict(cls)
    original = populate(cls)
    via_cdr = deserialize_message(serialize_message(original), cls)
    assert orjson.dumps(to_dict(via_cdr), option=ORJSON_OPTS) == orjson.dumps(
        to_dict(original), option=ORJSON_OPTS
    )


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_round_trip_without_int64_as_string(type_name: str) -> None:
    cls = get_message(type_name)
    to_dict = build_msg_to_dict(cls, int64_as_string=False)
    from_dict = build_dict_to_msg(cls, int64_as_string=False)
    original = populate(cls)
    rebuilt = from_dict(_json_round_trip(to_dict(original)))
    assert_identical(cls, original, rebuilt)


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_converter_covers_every_declared_field(type_name: str) -> None:
    """No field may be silently skipped by both converters at once.

    This is what keeps assert_identical honest for the types where serialised bytes cannot
    be compared: a field neither converter touched would round-trip perfectly by being
    absent from both sides.
    """
    cls = get_message(type_name)
    encoded = _json_round_trip(build_msg_to_dict(cls)(populate(cls)))

    def check(subtree: dict[str, object], value: object, where: str) -> None:
        assert isinstance(value, dict), f"{where} is not an object"
        assert set(value) == set(subtree), f"{where}: converter keys do not match the fields"
        for name, nested in subtree.items():
            if not isinstance(nested, dict):
                continue
            child = value[name]
            if isinstance(child, list):
                for index, item in enumerate(child):
                    check(nested, item, f"{where}.{name}[{index}]")
            elif isinstance(child, dict):
                check(nested, child, f"{where}.{name}")

    check(declared_field_tree(cls), encoded, type_name)


# --------------------------------------------------------------------------------------
# encoding contract
# --------------------------------------------------------------------------------------


def test_int64_becomes_a_string_and_survives_above_2_53() -> None:
    cls = get_message("test_msgs/msg/BasicTypes")
    msg = cls()
    msg.int64_value = BIG_INT64
    msg.uint64_value = 2**64 - 1

    encoded = _json_round_trip(build_msg_to_dict(cls)(msg))
    assert isinstance(encoded, dict)
    assert encoded["int64_value"] == str(BIG_INT64)
    assert encoded["uint64_value"] == str(2**64 - 1)
    assert isinstance(encoded["int32_value"], int), "only 64-bit ints become strings"

    rebuilt = build_dict_to_msg(cls)(encoded)
    assert rebuilt.int64_value == BIG_INT64
    assert rebuilt.uint64_value == 2**64 - 1


def test_int64_as_string_disabled_emits_numbers() -> None:
    cls = get_message("test_msgs/msg/BasicTypes")
    msg = cls()
    msg.int64_value = 7
    encoded = _json_round_trip(build_msg_to_dict(cls, int64_as_string=False)(msg))
    assert isinstance(encoded, dict)
    assert encoded["int64_value"] == 7


def test_int64_collections_become_string_arrays() -> None:
    cls = get_message("std_msgs/msg/Int64MultiArray")
    msg = cls()
    msg.data = [BIG_INT64, -BIG_INT64]
    encoded = _json_round_trip(build_msg_to_dict(cls)(msg))
    assert isinstance(encoded, dict)
    assert encoded["data"] == [str(BIG_INT64), str(-BIG_INT64)]
    assert build_dict_to_msg(cls)(encoded).data.tolist() == [BIG_INT64, -BIG_INT64]


def test_uint8_sequence_becomes_base64() -> None:
    cls = get_message("sensor_msgs/msg/Image")
    msg = cls()
    msg.data = b"\x00\x01\xfe\xff"
    encoded = _json_round_trip(build_msg_to_dict(cls)(msg))
    assert isinstance(encoded, dict)
    assert encoded["data"] == base64.b64encode(b"\x00\x01\xfe\xff").decode()
    assert build_dict_to_msg(cls)(encoded).data.tobytes() == b"\x00\x01\xfe\xff"


def test_octet_collection_becomes_base64_and_scalar_octet_becomes_an_int() -> None:
    multi = get_message("std_msgs/msg/ByteMultiArray")
    msg = multi()
    msg.data = [b"\x01", b"\x02", b"\xff"]
    encoded = _json_round_trip(build_msg_to_dict(multi)(msg))
    assert isinstance(encoded, dict)
    assert encoded["data"] == base64.b64encode(b"\x01\x02\xff").decode()
    assert build_dict_to_msg(multi)(encoded).data == [b"\x01", b"\x02", b"\xff"]

    basic = get_message("test_msgs/msg/BasicTypes")
    scalar = basic()
    scalar.byte_value = b"\x2a"
    scalar_json = _json_round_trip(build_msg_to_dict(basic)(scalar))
    assert isinstance(scalar_json, dict)
    assert scalar_json["byte_value"] == 42
    assert build_dict_to_msg(basic)(scalar_json).byte_value == b"\x2a"


def test_fixed_size_numeric_array_stays_numpy_for_orjson() -> None:
    """The read path must not pay for a .tolist() copy on covariance-shaped fields."""
    cls = get_message("sensor_msgs/msg/Imu")
    as_dict = build_msg_to_dict(cls)(populate(cls))
    assert isinstance(as_dict["orientation_covariance"], numpy.ndarray)
    assert orjson.dumps(as_dict, option=ORJSON_OPTS)


def test_numeric_sequence_is_listified_because_orjson_cannot_take_array_array() -> None:
    cls = get_message("sensor_msgs/msg/JointState")
    msg = cls()
    msg.position = [1.5, 2.5]
    as_dict = build_msg_to_dict(cls)(msg)
    assert as_dict["position"] == [1.5, 2.5]
    assert orjson.dumps(as_dict, option=ORJSON_OPTS)


def test_omitted_fields_keep_ros_defaults() -> None:
    cls = get_message("test_msgs/msg/Defaults")
    built = build_dict_to_msg(cls)({})
    assert serialize_message(built) == serialize_message(cls())
    assert built.bool_value is True, "test_msgs/Defaults declares a non-zero default"


def test_partial_body_only_overrides_named_fields() -> None:
    cls = get_message("geometry_msgs/msg/Twist")
    built = build_dict_to_msg(cls)({"linear": {"x": 1.5}})
    assert (built.linear.x, built.linear.y, built.angular.z) == (1.5, 0.0, 0.0)


def test_nan_and_infinity_encode_as_null() -> None:
    """Documented lossy case: JSON has no NaN, so orjson writes null."""
    cls = get_message("geometry_msgs/msg/Twist")
    msg = cls()
    msg.linear.x = float("nan")
    msg.linear.y = float("inf")
    encoded = _json_round_trip(build_msg_to_dict(cls)(msg))
    assert isinstance(encoded, dict)
    assert encoded["linear"]["x"] is None
    assert encoded["linear"]["y"] is None


def test_path_parameter_strings_coerce_into_numeric_fields() -> None:
    """Path parameters always arrive as strings; request_map targets must accept them."""
    cls = get_message("test_msgs/msg/BasicTypes")
    built = build_dict_to_msg(cls)(
        {"int32_value": "-5", "float64_value": "1.25", "bool_value": "true", "uint8_value": "7"}
    )
    assert (built.int32_value, built.float64_value, built.bool_value, built.uint8_value) == (
        -5,
        1.25,
        True,
        7,
    )


# --------------------------------------------------------------------------------------
# rejection
# --------------------------------------------------------------------------------------


def test_unknown_field_is_rejected_not_ignored() -> None:
    cls = get_message("geometry_msgs/msg/Twist")
    with pytest.raises(ConversionError, match="unknown field"):
        build_dict_to_msg(cls)({"linaer": {"x": 1.0}})


def test_unknown_field_in_a_nested_message_is_rejected() -> None:
    cls = get_message("geometry_msgs/msg/Twist")
    with pytest.raises(ConversionError, match="linear: unknown field"):
        build_dict_to_msg(cls)({"linear": {"w": 1.0}})


def test_unknown_field_on_an_empty_message_is_rejected() -> None:
    cls = get_message("test_msgs/msg/Empty")
    with pytest.raises(ConversionError, match="unknown field"):
        build_dict_to_msg(cls)({"anything": 1})


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("int8_value", 128, "outside"),
        ("int8_value", -129, "outside"),
        ("uint8_value", -1, "outside"),
        ("uint8_value", 256, "outside"),
        ("int32_value", 2**31, "outside"),
        ("uint64_value", 2**64, "outside"),
        ("int32_value", 1.5, "expected an integer"),
        ("int32_value", True, "expected an integer"),
        ("int32_value", "abc", "expected an integer"),
        ("float64_value", "abc", "expected a number"),
        ("float64_value", None, "expected a number"),
        ("bool_value", 1, "expected a boolean"),
        ("bool_value", "maybe", "expected a boolean"),
    ],
)
def test_out_of_contract_scalars_are_rejected(field: str, value: object, match: str) -> None:
    cls = get_message("test_msgs/msg/BasicTypes")
    with pytest.raises(ConversionError, match=match):
        build_dict_to_msg(cls)({field: value})


def test_non_string_for_a_string_field_is_rejected() -> None:
    cls = get_message("std_msgs/msg/String")
    with pytest.raises(ConversionError, match="expected a string"):
        build_dict_to_msg(cls)({"data": 5})


def test_wrong_fixed_array_length_is_rejected() -> None:
    cls = get_message("sensor_msgs/msg/Imu")
    with pytest.raises(ConversionError, match="expected exactly 9 items"):
        build_dict_to_msg(cls)({"orientation_covariance": [0.0, 1.0]})


def test_bounded_sequence_over_its_maximum_is_rejected() -> None:
    cls = get_message("test_msgs/msg/BoundedSequences")
    with pytest.raises(ConversionError, match="at most 3 items"):
        build_dict_to_msg(cls)({"float64_values": [1.0, 2.0, 3.0, 4.0]})


def test_bad_base64_for_a_byte_field_is_rejected() -> None:
    cls = get_message("sensor_msgs/msg/Image")
    with pytest.raises(ConversionError, match="expected base64"):
        build_dict_to_msg(cls)({"data": "not base64!!"})


def test_scalar_where_an_array_belongs_is_rejected() -> None:
    cls = get_message("sensor_msgs/msg/JointState")
    with pytest.raises(ConversionError, match="expected an array"):
        build_dict_to_msg(cls)({"name": "not-a-list"})


def test_non_object_body_is_rejected() -> None:
    cls = get_message("geometry_msgs/msg/Twist")
    with pytest.raises(ConversionError, match="expected an object"):
        build_dict_to_msg(cls)([1, 2, 3])


# --------------------------------------------------------------------------------------
# numeric sequence strategy
# --------------------------------------------------------------------------------------


def test_short_numeric_sequence_becomes_a_list_and_long_one_a_numpy_view() -> None:
    """Both encode identically; the split exists only because each is faster at its length."""
    cls = get_message("sensor_msgs/msg/JointState")
    to_dict = build_msg_to_dict(cls)

    short = cls()
    short.position = [float(i) for i in range(8)]
    assert isinstance(to_dict(short)["position"], list)

    long_message = cls()
    long_message.position = [float(i) for i in range(512)]
    assert isinstance(to_dict(long_message)["position"], numpy.ndarray)


@pytest.mark.parametrize("length", [0, 1, 63, 64, 65, 300])
def test_numeric_sequence_json_is_identical_either_side_of_the_threshold(length: int) -> None:
    cls = get_message("sensor_msgs/msg/JointState")
    message = cls()
    message.position = [i * 1.5 for i in range(length)]
    encoded = _json_round_trip(build_msg_to_dict(cls)(message))
    assert isinstance(encoded, dict)
    assert encoded["position"] == [i * 1.5 for i in range(length)]


def test_numpy_view_of_a_sequence_is_zero_copy() -> None:
    """The point of the exercise: no list build, no second walk in orjson."""
    cls = get_message("sensor_msgs/msg/JointState")
    message = cls()
    message.position = [float(i) for i in range(128)]
    view = build_msg_to_dict(cls)(message)["position"]
    assert isinstance(view, numpy.ndarray)
    assert view.base is not None, "a copy would have no base buffer"


def test_float32_sequence_always_uses_the_view_whatever_its_length() -> None:
    """Otherwise number formatting would depend on the length of the sequence.

    array('f').tolist() widens to double and prints 1.8700000047683716; numpy prints the
    shortest text that round-trips as float32, 1.87. Both read back exactly, but the choice
    must not vary with length, and fixed-size float32[N] already emits the numpy form.
    """
    cls = get_message("test_msgs/msg/UnboundedSequences")
    to_dict = build_msg_to_dict(cls)
    for length in (1, 4, 200):
        message = cls()
        message.float32_values = [1.87] * length
        encoded = to_dict(message)
        assert isinstance(encoded["float32_values"], numpy.ndarray)
        assert b"1.87," in orjson.dumps(encoded, option=ORJSON_OPTS).replace(b"]", b",")


def test_float32_scalar_and_sequence_and_array_agree() -> None:
    """The three float32 shapes must not print the same value three different ways."""
    arrays = get_message("test_msgs/msg/Arrays")()
    arrays.float32_values = [1.87, 1.87, 1.87]
    fixed = build_msg_to_dict(type(arrays))(arrays)["float32_values"]

    sequences = get_message("test_msgs/msg/UnboundedSequences")()
    sequences.float32_values = [1.87]
    sequence = build_msg_to_dict(type(sequences))(sequences)["float32_values"]

    assert orjson.dumps(fixed[:1], option=ORJSON_OPTS) == orjson.dumps(sequence, option=ORJSON_OPTS)


def test_int64_sequence_ignores_the_view_because_every_element_needs_str() -> None:
    cls = get_message("std_msgs/msg/Int64MultiArray")
    message = cls()
    message.data = [BIG_INT64] * 128
    encoded = build_msg_to_dict(cls)(message)["data"]
    assert encoded == [str(BIG_INT64)] * 128


def test_walrus_bindings_do_not_collide_across_fields() -> None:
    """Three double sequences in one dict literal each need their own temporary."""
    cls = get_message("sensor_msgs/msg/JointState")
    message = cls()
    message.position = [float(i) for i in range(100)]
    message.velocity = [float(i) for i in range(3)]
    message.effort = [float(i) for i in range(200)]
    encoded = _json_round_trip(build_msg_to_dict(cls)(message))
    assert isinstance(encoded, dict)
    assert len(encoded["position"]) == 100
    assert len(encoded["velocity"]) == 3
    assert len(encoded["effort"]) == 200


def test_numeric_sequence_inside_a_collection_of_messages_still_works() -> None:
    """A walrus inside a comprehension binds in the enclosing scope; make sure it compiles."""
    cls = get_message("test_msgs/msg/MultiNested")
    assert build_msg_to_dict(cls)(populate(cls))["unbounded_sequence_of_arrays"] is not None


# --------------------------------------------------------------------------------------
# error paths name the field the client wrote
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_name", "body", "expected_field", "fragment"),
    [
        ("geometry_msgs/msg/Twist", {"linear": {"x": "abc"}}, "linear.x", "expected a number"),
        ("geometry_msgs/msg/Twist", {"linear": {"w": 1}}, "linear", "unknown field(s) w"),
        (
            "sensor_msgs/msg/Imu",
            {"header": {"stamp": {"sec": 2**31}}},
            "header.stamp.sec",
            "is outside",
        ),
        (
            "sensor_msgs/msg/Imu",
            {"orientation_covariance": [1.0]},
            "orientation_covariance",
            "expected exactly 9 items",
        ),
        ("sensor_msgs/msg/JointState", {"name": [1]}, "name[]", "expected a string"),
        (
            "diagnostic_msgs/msg/DiagnosticArray",
            {"status": [{"values": [{"key": 5}]}]},
            "status[].values[].key",
            "expected a string",
        ),
        (
            "diagnostic_msgs/msg/DiagnosticArray",
            {"status": [{"nope": 5}]},
            "status[]",
            "unknown field(s) nope",
        ),
    ],
)
def test_conversion_error_carries_the_dotted_field_path(
    type_name: str, body: object, expected_field: str, fragment: str
) -> None:
    """The 400 body quotes this straight back, so it has to read like the request."""
    with pytest.raises(ConversionError) as exc:
        build_dict_to_msg(get_message(type_name))(body)
    assert exc.value.field == expected_field
    assert str(exc.value).startswith(f"{expected_field}: ")
    assert fragment in str(exc.value)


def test_unknown_field_at_the_root_is_reported_against_the_body() -> None:
    with pytest.raises(ConversionError) as exc:
        build_dict_to_msg(get_message("geometry_msgs/msg/Twist"))({"linaer": {}})
    assert exc.value.field is None
    assert str(exc.value) == "body: unknown field(s) linaer"


def test_same_type_at_two_paths_gets_two_builders_with_distinct_labels() -> None:
    """Vector3 appears at both linear and angular; each must name its own path."""
    from_dict = build_dict_to_msg(get_message("geometry_msgs/msg/Twist"))
    for field in ("linear", "angular"):
        with pytest.raises(ConversionError) as exc:
            from_dict({field: {"x": "nope"}})
        assert exc.value.field == f"{field}.x"


# --------------------------------------------------------------------------------------
# generated source
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("type_name", GOLDEN_TYPES)
def test_generated_source_matches_golden(type_name: str) -> None:
    """Locks the shape of the hot path. Regenerate with `pytest --update-golden`."""
    cls = get_message(type_name)
    actual = (
        f"# {type_name} msg_to_dict\n"
        f"{msg_to_dict_source(cls)}\n"
        f"# {type_name} dict_to_msg\n"
        f"{dict_to_msg_source(cls)}"
    )
    golden = GOLDEN_DIR / f"{type_name.replace('/', '__')}.py.txt"
    if not golden.exists():
        pytest.fail(f"missing golden file {golden}; run scripts/update_golden.py")
    assert actual == golden.read_text(encoding="utf-8")


def test_read_converter_has_no_loop_or_lookup_per_field() -> None:
    """The point of code generation: one expression, no introspection at runtime."""
    source = msg_to_dict_source(get_message("geometry_msgs/msg/PoseStamped"))
    assert "getattr" not in source
    assert "for " not in source, "PoseStamped has no collections, so it needs no loop"
    assert source.count("def ") == 1


def test_nested_message_inside_a_collection_does_not_hoist_a_local() -> None:
    """Regression: hoisting a temp out of a comprehension makes the loop variable escape."""
    cls = get_message("diagnostic_msgs/msg/DiagnosticArray")
    to_dict = build_msg_to_dict(cls)
    assert to_dict(populate(cls))["status"][0]["values"][0]["key"]


# --------------------------------------------------------------------------------------
# performance
# --------------------------------------------------------------------------------------

_MIN_SPEEDUP = 5.0


@pytest.mark.bench
def test_converter_beats_message_to_ordereddict_on_imu() -> None:
    """Asserted, not informational: the generated path must be worth its complexity."""
    import time

    cls = get_message("sensor_msgs/msg/Imu")
    msg = deserialize_message(serialize_message(populate(cls)), cls)
    generated = build_msg_to_dict(cls)

    def timed(func: object, rounds: int = 3000) -> float:
        assert callable(func)
        func(msg)
        best = float("inf")
        for _ in range(5):
            start = time.perf_counter()
            for _ in range(rounds):
                func(msg)
            best = min(best, time.perf_counter() - start)
        return best

    speedup = timed(message_to_ordereddict) / timed(generated)
    assert speedup >= _MIN_SPEEDUP, f"only {speedup:.1f}x faster than message_to_ordereddict"


@pytest.mark.bench
def test_converter_speedup_reported_for_every_battery_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Informational sweep, so a regression on a shape other than Imu is visible."""
    import time

    lines = []
    for type_name in ALL_TYPES:
        cls = get_message(type_name)
        msg = deserialize_message(serialize_message(populate(cls)), cls)
        generated = build_msg_to_dict(cls)

        def timed(func: object, message: object = msg, rounds: int = 500) -> float:
            assert callable(func)
            func(message)
            start = time.perf_counter()
            for _ in range(rounds):
                func(message)
            return time.perf_counter() - start

        lines.append(f"{type_name:42s} {timed(message_to_ordereddict) / timed(generated):6.1f}x")
    with capsys.disabled():
        print("\n" + "\n".join(lines))


def test_converters_are_cheap_to_build() -> None:
    """Startup cost matters: every configured type is generated before the port opens."""
    import time

    start = time.perf_counter()
    for type_name in ALL_TYPES:
        cls = get_message(type_name)
        build_msg_to_dict(cls)
        build_dict_to_msg(cls)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"generating {len(ALL_TYPES)} type pairs took {elapsed:.2f}s"


def test_generated_source_is_attached_for_debugging() -> None:
    cls = get_message("geometry_msgs/msg/Twist")
    assert "def to_dict" in build_msg_to_dict(cls).__ros_source__
    assert "def from_dict" in build_dict_to_msg(cls).__ros_source__


def test_counter_shared_across_a_tree_gives_distinct_values() -> None:
    """Guards the fixture itself: identical values would hide field-ordering bugs."""
    cls = get_message("geometry_msgs/msg/Twist")
    msg = populate(cls, itertools.count(1))
    values = [msg.linear.x, msg.linear.y, msg.linear.z, msg.angular.x]
    assert len(set(values)) == len(values)
