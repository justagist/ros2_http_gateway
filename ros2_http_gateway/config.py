"""Config loading: YAML, then JSON Schema, then a frozen typed model.

Everything here runs before the port is opened and imports no ROS packages, so the whole
config engine is unit-testable on a plain Python installation. Validation that needs real
interface definitions (type resolution, dotted precondition fields) lives in
:mod:`ros2_http_gateway.resolve`.

Every problem in a file is reported rather than just the first, and every message names the
config location and, where there is one, the fix. This is the tool a CI job runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import stat
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

import jsonschema
import yaml

_LOG = logging.getLogger(__name__)

SCHEMA_PATH: Final = Path(__file__).with_name("config.schema.json")

DEFAULT_BIND: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8080
DEFAULT_BASE_PATH: Final = "/api/v1"
DEFAULT_SSE_PATH: Final = "/events"
DEFAULT_MAX_CLIENTS: Final = 32
DEFAULT_TIMEOUT_MS: Final = 5000
DEFAULT_RESULT_TTL_S: Final = 300.0

_PARAM_RE: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WILDCARD: Final = "*"

# Served unconditionally under http.base_path, so an endpoint cannot be configured at one.
# Listed here so a collision fails at startup rather than at the first request.
_BUILT_IN_ROUTES: Final = (
    ("GET", "/health", "health"),
    ("GET", "/openapi.json", "OpenAPI document"),
    ("GET", "/docs", "Swagger UI"),
    # Reserved even when `http.console` is false, so enabling the console can never turn a
    # working config into a colliding one.
    ("GET", "/whoami", "identity"),
    ("GET", "/console", "test console"),
    ("GET", "/console/manifest", "console manifest"),
)

Kind = Literal["topic", "service", "action"]
Method = Literal["GET", "POST"]
QosPreset = Literal["default", "sensor_data"]
Overflow = Literal["drop_oldest", "disconnect"]
Executor = Literal["auto", "events", "multithreaded"]
AuditSink = Literal["stderr", "file"]


class ConfigError(Exception):
    """One or more problems in a config file, reported together."""

    def __init__(self, source: Path | str, problems: Sequence[str]) -> None:
        self.source = str(source)
        self.problems = tuple(problems)
        super().__init__(self.format())

    def format(self) -> str:
        """Return every problem as an indented, human-readable block."""
        head = f"{self.source}: {len(self.problems)} problem(s)"
        return "\n".join([head, *(f"  - {problem}" for problem in self.problems)])


@dataclass(frozen=True, slots=True)
class TokenConfig:
    """One bearer token identity. The plaintext is never retained."""

    user: str
    roles: frozenset[str]
    sha256: str


@dataclass(frozen=True, slots=True)
class Precondition:
    """A state check that must pass before a call is forwarded to ROS."""

    state_endpoint: str
    field_path: tuple[str, ...]
    key: str | None
    allow: Mapping[str, frozenset[str]]

    def permitted_states(self, param_value: str | None) -> frozenset[str] | None:
        """Return the states allowed for ``param_value``, or None if it is not listed.

        The set may be ``{"*"}``, meaning any state.
        """
        if param_value is not None:
            exact = self.allow.get(param_value)
            if exact is not None:
                return exact
        return self.allow.get(_WILDCARD)


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """One configured HTTP endpoint, fully defaulted."""

    path: str
    kind: Kind
    ros_name: str
    method: Method
    roles: frozenset[str]
    type_name: str | None
    path_params: tuple[str, ...]
    qos: QosPreset = "default"
    stale_after_ms: int | None = None
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    request_map: Mapping[str, str] = field(default_factory=dict)
    preconditions: tuple[Precondition, ...] = ()

    @property
    def is_topic_read(self) -> bool:
        """Whether this endpoint serves the latest cached sample of a topic."""
        return self.kind == "topic" and self.method == "GET"

    @property
    def is_topic_write(self) -> bool:
        """Whether this endpoint publishes its request body to a topic."""
        return self.kind == "topic" and self.method == "POST"


@dataclass(frozen=True, slots=True)
class SseConfig:
    """Global server-sent-events stream."""

    path: str
    include: tuple[str, ...]
    overflow: Overflow
    max_clients: int


@dataclass(frozen=True, slots=True)
class HttpConfig:
    """Listening socket and API prefix."""

    bind: str
    port: int
    base_path: str
    console: bool = False


@dataclass(frozen=True, slots=True)
class RosConfig:
    """ROS-side runtime options."""

    executor: Executor


@dataclass(frozen=True, slots=True)
class ActionsConfig:
    """Goal registry behaviour."""

    result_ttl_s: float


@dataclass(frozen=True, slots=True)
class AuditConfig:
    """Where audit records go."""

    sink: AuditSink
    path: Path | None


@dataclass(frozen=True, slots=True)
class JsonConfig:
    """JSON encoding options that the generated converters bake in."""

    int64_as_string: bool


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """The complete, validated, defaulted configuration."""

    source: Path
    endpoints: Mapping[str, EndpointConfig]
    tokens: tuple[TokenConfig, ...]
    http: HttpConfig
    ros: RosConfig
    actions: ActionsConfig
    audit: AuditConfig
    json: JsonConfig
    sse: SseConfig | None

    @property
    def roles(self) -> frozenset[str]:
        """Every role mentioned by any endpoint."""
        return frozenset().union(*(endpoint.roles for endpoint in self.endpoints.values()))

    def endpoints_needing_type_resolution(self) -> tuple[EndpointConfig, ...]:
        """Return endpoints whose interface type must come from the ROS graph."""
        return tuple(e for e in self.endpoints.values() if e.type_name is None)


def load_schema() -> dict[str, Any]:
    """Return the bundled config JSON Schema."""
    with SCHEMA_PATH.open("rb") as handle:
        schema: dict[str, Any] = json.load(handle)
    return schema


def load_config(path: str | Path) -> GatewayConfig:
    """Load, validate and default a config file.

    Raises :class:`ConfigError` listing every problem found, not just the first.
    """
    source = Path(path)
    document = _read_yaml(source)
    _validate_against_schema(source, document)
    return _build(source, document)


def _read_yaml(source: Path) -> dict[str, Any]:
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(source, ["no such file"]) from None
    except OSError as exc:
        raise ConfigError(source, [f"cannot read: {exc.strerror}"]) from None

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(source, [f"not valid YAML: {exc}"]) from None

    if document is None:
        raise ConfigError(source, ["file is empty"])
    if not isinstance(document, dict):
        raise ConfigError(source, [f"top level must be a mapping, got {type(document).__name__}"])
    return document


def _validate_against_schema(source: Path, document: Mapping[str, Any]) -> None:
    validator = jsonschema.Draft202012Validator(load_schema())
    problems = [
        f"{_pointer(error.absolute_path)}: {_schema_message(error)}"
        for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    ]
    if problems:
        raise ConfigError(source, problems)


def _pointer(parts: Iterable[Any]) -> str:
    joined = ".".join(str(part) for part in parts)
    return joined or "<root>"


def _schema_message(error: jsonschema.ValidationError) -> str:
    """Turn a schema error into something a config author can act on.

    oneOf cases match on the document location, not on a substring of the schema path, so
    reshuffling the schema cannot silently restore "is not valid under any of the given schemas".
    """
    path = list(error.absolute_path)
    if error.validator == "oneOf":
        if len(path) == 2 and path[0] == "endpoints":
            return "must name exactly one of topic, service or action"
        if len(path) == 3 and path[:2] == ["auth", "tokens"]:
            return "must give exactly one of token or token_sha256"
    if error.validator == "additionalProperties":
        return f"{error.message} (check the spelling; unknown keys are never ignored)"
    return str(error.message)


class _Builder:
    """Accumulates semantic problems while assembling the typed model."""

    def __init__(self, source: Path, document: Mapping[str, Any]) -> None:
        self.source = source
        self.document = document
        self.problems: list[str] = []

    def fail(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")

    def endpoints(self) -> dict[str, EndpointConfig]:
        defaults = self.document.get("defaults", {})
        built: dict[str, EndpointConfig] = {}
        for path, raw in self.document["endpoints"].items():
            endpoint = self._endpoint(path, raw, defaults)
            if endpoint is not None:
                built[path] = endpoint
        return built

    def _endpoint(
        self, path: str, raw: Mapping[str, Any], defaults: Mapping[str, Any]
    ) -> EndpointConfig | None:
        where = f"endpoints.{path}"
        kind: Kind
        for candidate in ("topic", "service", "action"):
            if candidate in raw:
                kind = candidate
                break
        else:  # pragma: no cover - the schema's oneOf already rejects this
            return None

        params = _path_params(path)
        if len(set(params)) != len(params):
            self.fail(where, "the same path parameter appears more than once")

        method = self._method(where, kind, raw)
        roles = self._roles(where, raw, defaults)
        stale = self._stale_after_ms(where, kind, method, raw, defaults)
        timeout = self._timeout_ms(where, kind, raw, defaults)
        request_map = self._request_map(where, kind, raw, params)
        preconditions = self._preconditions(where, kind, method, raw, params)

        return EndpointConfig(
            path=path,
            kind=kind,
            ros_name=raw[kind],
            method=method,
            roles=roles,
            type_name=raw.get("type"),
            path_params=params,
            qos=self._qos(where, kind, raw),
            stale_after_ms=stale,
            timeout_ms=timeout,
            request_map=request_map,
            preconditions=preconditions,
        )

    def _method(self, where: str, kind: Kind, raw: Mapping[str, Any]) -> Method:
        given = raw.get("method")
        if kind == "topic":
            return "POST" if given == "POST" else "GET"
        if given not in (None, "POST"):
            self.fail(
                where,
                f"a {kind} endpoint is always POST; remove `method: {given}`",
            )
        return "POST"

    def _qos(self, where: str, kind: Kind, raw: Mapping[str, Any]) -> QosPreset:
        given = raw.get("qos")
        if given is None:
            return "default"
        if kind != "topic":
            self.fail(where, f"qos only applies to a topic endpoint, not a {kind} one")
            return "default"
        preset: QosPreset = given
        return preset

    def _roles(
        self, where: str, raw: Mapping[str, Any], defaults: Mapping[str, Any]
    ) -> frozenset[str]:
        roles = raw.get("roles", defaults.get("roles"))
        if not roles:
            self.fail(
                where,
                "no roles; set `roles:` here or under `defaults:` "
                "(an endpoint with no roles would be unreachable)",
            )
            return frozenset()
        return frozenset(roles)

    def _stale_after_ms(
        self,
        where: str,
        kind: Kind,
        method: Method,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any],
    ) -> int | None:
        applicable = kind == "topic" and method == "GET"
        if "stale_after_ms" in raw and not applicable:
            self.fail(where, "stale_after_ms only applies to a topic GET endpoint")
            return None
        if "stale_after_ms" in raw:
            value: int = raw["stale_after_ms"]
            return value
        # From defaults it is silently ignored where it does not apply, so that a single
        # `defaults:` block can cover a config holding all three endpoint kinds.
        return defaults.get("stale_after_ms") if applicable else None

    def _timeout_ms(
        self, where: str, kind: Kind, raw: Mapping[str, Any], defaults: Mapping[str, Any]
    ) -> int:
        applicable = kind in ("service", "action")
        if "timeout_ms" in raw and not applicable:
            self.fail(where, "timeout_ms only applies to a service or action endpoint")
            return DEFAULT_TIMEOUT_MS
        if "timeout_ms" in raw:
            value: int = raw["timeout_ms"]
            return value
        if applicable:
            fallback: int = defaults.get("timeout_ms", DEFAULT_TIMEOUT_MS)
            return fallback
        return DEFAULT_TIMEOUT_MS

    def _request_map(
        self, where: str, kind: Kind, raw: Mapping[str, Any], params: tuple[str, ...]
    ) -> Mapping[str, str]:
        mapping = raw.get("request_map")
        if mapping is None:
            return {}
        if kind != "service":
            self.fail(
                where,
                f"request_map is only supported on a service endpoint, not a {kind} one",
            )
            return {}
        for target, template in mapping.items():
            for referenced in _PARAM_RE.findall(template):
                if referenced not in params:
                    available = ", ".join(f"{{{name}}}" for name in params) or "none"
                    self.fail(
                        f"{where}.request_map.{target}",
                        f"references {{{referenced}}}, which is not a path parameter of "
                        f"{where.removeprefix('endpoints.')} (available: {available})",
                    )
        return dict(mapping)

    def _preconditions(
        self,
        where: str,
        kind: Kind,
        method: Method,
        raw: Mapping[str, Any],
        params: tuple[str, ...],
    ) -> tuple[Precondition, ...]:
        entries = raw.get("preconditions")
        if not entries:
            return ()
        if kind == "topic" and method == "GET":
            self.fail(where, "preconditions guard calls that change state, so not a topic GET")
            return ()

        built: list[Precondition] = []
        for index, entry in enumerate(entries):
            spot = f"{where}.preconditions[{index}]"
            key, key_failed = self._precondition_key(spot, entry, params)
            allow = {value: frozenset(states) for value, states in entry["allow"].items()}
            # Skipped when the key itself was rejected, so one mistake reports once.
            if key is None and not key_failed and set(allow) != {_WILDCARD}:
                self.fail(
                    spot,
                    'without a keying path parameter the only usable allow key is "*"',
                )
            built.append(
                Precondition(
                    state_endpoint=entry["state_endpoint"],
                    field_path=tuple(entry["field"].split(".")),
                    key=key,
                    allow=allow,
                )
            )
        return tuple(built)

    def _precondition_key(
        self, spot: str, entry: Mapping[str, Any], params: tuple[str, ...]
    ) -> tuple[str | None, bool]:
        """Return the keying parameter, plus whether resolving it already failed."""
        given = entry.get("key")
        if given is not None:
            name = given[1:-1]
            if name not in params:
                available = ", ".join(f"{{{param}}}" for param in params) or "none"
                self.fail(spot, f"key {given} is not a path parameter (available: {available})")
                return None, True
            return name, False
        if len(params) == 1:
            return params[0], False
        if not params:
            return None, False
        listed = ", ".join(f"{{{param}}}" for param in params)
        self.fail(
            spot,
            f"the endpoint has several path parameters ({listed}); "
            "add `key:` naming the one that keys the allow map",
        )
        return None, True

    def cross_references(self, endpoints: Mapping[str, EndpointConfig]) -> None:
        """Check that every endpoint reference resolves to a topic read endpoint."""
        for path, endpoint in endpoints.items():
            for index, precondition in enumerate(endpoint.preconditions):
                spot = f"endpoints.{path}.preconditions[{index}].state_endpoint"
                self._require_topic_read(spot, precondition.state_endpoint, endpoints)
                self._require_staleness_bound(spot, precondition.state_endpoint, endpoints)

        sse = self.document.get("sse")
        if sse:
            for index, key in enumerate(sse["include"]):
                self._require_topic_read(f"sse.include[{index}]", key, endpoints)

    def _require_staleness_bound(
        self, spot: str, key: str, endpoints: Mapping[str, EndpointConfig]
    ) -> None:
        """A state endpoint a gate depends on must have an age limit.

        Only an endpoint with ``stale_after_ms`` can ever *be* stale. Without one, killing the
        state publisher leaves the gate authorising against the last sample it ever saw,
        indefinitely. An error rather than a warning: a log line is not a safety control.
        """
        target = endpoints.get(key)
        if target is None or not target.is_topic_read:
            return  # already reported by _require_topic_read
        if target.stale_after_ms is None:
            self.fail(
                spot,
                f"{key!r} has no stale_after_ms, so this gate would authorise against a "
                "sample of any age, including one from before the publisher died. Set "
                f"stale_after_ms on endpoints.{key}",
            )

    def _require_topic_read(
        self, spot: str, key: str, endpoints: Mapping[str, EndpointConfig]
    ) -> None:
        target = endpoints.get(key)
        if target is None:
            known = ", ".join(sorted(endpoints)) or "none"
            self.fail(spot, f"unknown endpoint {key!r} (configured endpoints: {known})")
        elif not target.is_topic_read:
            self.fail(
                spot,
                f"{key!r} is a {target.kind} {target.method} endpoint; only a topic GET "
                "endpoint has a cached sample to read",
            )

    def route_conflicts(self, endpoints: Mapping[str, EndpointConfig]) -> None:
        """Reject any two routes that could match the same request.

        Not just generated action subroutes: ``/a/{x}`` and ``/a/{y}`` on one method are
        indistinguishable, as are a literal and a template covering it. Undetected, which one
        answers comes down to registration order.
        """
        routes = list(_all_routes(endpoints, self.document.get("sse")))
        for index, earlier in enumerate(routes):
            for later in routes[index + 1 :]:
                if earlier.method != later.method:
                    continue
                if not _templates_overlap(earlier.template, later.template):
                    continue
                self.fail(
                    later.where,
                    f"{later.description} can match the same request as "
                    f"{earlier.description}; give them different paths or methods",
                )

    def tokens(self) -> tuple[TokenConfig, ...]:
        """Hash plaintext tokens, refusing outright if the file is world-readable."""
        raw_tokens = self.document["auth"]["tokens"]
        plaintext = [entry for entry in raw_tokens if "token" in entry]
        if plaintext and _is_world_readable(self.source):
            self.fail(
                "auth.tokens",
                f"{len(plaintext)} plaintext token(s) in a world-readable file; "
                f"run `chmod o-r {self.source}` or switch to token_sha256",
            )
        if plaintext:
            users = ", ".join(entry["user"] for entry in plaintext)
            _LOG.warning(
                "SECURITY: plaintext token(s) in %s for %s. They are hashed in memory, but "
                "the file still contains the secret; prefer token_sha256.",
                self.source,
                users,
            )

        built = []
        seen: dict[str, str] = {}
        for index, entry in enumerate(raw_tokens):
            digest = (
                hashlib.sha256(entry["token"].encode("utf-8")).hexdigest()
                if "token" in entry
                else entry["token_sha256"].lower()
            )
            if digest in seen:
                self.fail(
                    f"auth.tokens[{index}]",
                    f"the same token is already assigned to {seen[digest]!r}",
                )
            seen[digest] = entry["user"]
            built.append(
                TokenConfig(user=entry["user"], roles=frozenset(entry["roles"]), sha256=digest)
            )
        return tuple(built)

    def sse(self) -> SseConfig | None:
        raw = self.document.get("sse")
        if not raw:
            return None
        return SseConfig(
            path=raw.get("path", DEFAULT_SSE_PATH),
            include=tuple(raw["include"]),
            overflow=raw.get("overflow", "drop_oldest"),
            max_clients=raw.get("max_clients", DEFAULT_MAX_CLIENTS),
        )

    def http(self) -> HttpConfig:
        raw = self.document.get("http", {})
        base_path = raw.get("base_path", DEFAULT_BASE_PATH).rstrip("/")
        bind = raw.get("bind", DEFAULT_BIND)
        if bind == "0.0.0.0":
            _LOG.warning(
                "gateway is bound to 0.0.0.0; it does not terminate TLS, so put a reverse "
                "proxy in front of it or bind to %s",
                DEFAULT_BIND,
            )
        return HttpConfig(
            bind=bind,
            port=raw.get("port", DEFAULT_PORT),
            base_path=base_path,
            console=raw.get("console", False),
        )

    def audit(self) -> AuditConfig:
        raw = self.document.get("audit", {})
        sink: AuditSink = raw.get("sink", "stderr")
        return AuditConfig(sink=sink, path=Path(raw["path"]) if sink == "file" else None)


def _build(source: Path, document: Mapping[str, Any]) -> GatewayConfig:
    builder = _Builder(source, document)
    endpoints = builder.endpoints()
    builder.cross_references(endpoints)
    builder.route_conflicts(endpoints)
    tokens = builder.tokens()
    sse = builder.sse()
    http = builder.http()
    audit = builder.audit()

    if builder.problems:
        raise ConfigError(source, builder.problems)

    ros_raw = document.get("ros", {})
    actions_raw = document.get("actions", {})
    json_raw = document.get("json", {})
    return GatewayConfig(
        source=source,
        endpoints=endpoints,
        tokens=tokens,
        http=http,
        ros=RosConfig(executor=ros_raw.get("executor", "auto")),
        actions=ActionsConfig(result_ttl_s=actions_raw.get("result_ttl_s", DEFAULT_RESULT_TTL_S)),
        audit=audit,
        json=JsonConfig(int64_as_string=json_raw.get("int64_as_string", True)),
        sse=sse,
    )


class _Route(NamedTuple):
    """One HTTP route the gateway will serve, for collision checking."""

    method: str
    template: str
    where: str
    description: str


def _all_routes(
    endpoints: Mapping[str, EndpointConfig], sse: Mapping[str, Any] | None
) -> Iterator[_Route]:
    """Yield every route the gateway serves, configured, generated or built in.

    Built-ins come first so a configured endpoint colliding with one is the side blamed.
    """
    for method, template, name in _BUILT_IN_ROUTES:
        yield _Route(method, template, "endpoints", f"the built-in {name} route {template}")

    for path, endpoint in endpoints.items():
        where = f"endpoints.{path}"
        yield _Route(endpoint.method, path, where, f"{endpoint.method} {path}")
        if endpoint.kind != "action":
            continue
        # Derived by the action protocol, not written by the author, which is why a collision
        # with one needs pointing out.
        for method, template in (
            ("GET", f"{path}/{{goal_id}}"),
            ("DELETE", f"{path}/{{goal_id}}"),
            ("GET", f"{path}/{{goal_id}}/stream"),
        ):
            yield _Route(method, template, where, f"its generated action route {method} {template}")

    if sse:
        path = sse.get("path", DEFAULT_SSE_PATH)
        yield _Route("GET", path, "sse.path", f"the event stream GET {path}")


def _path_params(path: str) -> tuple[str, ...]:
    return tuple(_PARAM_RE.findall(path))


def _templates_overlap(left: str, right: str) -> bool:
    """Whether two path templates can both match the same concrete path."""
    left_parts, right_parts = left.strip("/").split("/"), right.strip("/").split("/")
    if len(left_parts) != len(right_parts):
        return False
    return all(
        a == b or a.startswith("{") or b.startswith("{")
        for a, b in zip(left_parts, right_parts, strict=True)
    )


def _is_world_readable(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & stat.S_IROTH)
    except OSError:  # pragma: no cover - the file was just read successfully
        return False
