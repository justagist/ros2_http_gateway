# HTTP protocol reference

Every route lives under `http.base_path` (default `/api/v1`). `GET <base>/health` is the
only unauthenticated route.

## Authentication

```
Authorization: Bearer <token>
```

The token is hashed and compared in constant time against the configured `token_sha256`
values. Browser `EventSource` cannot set headers, so the SSE route additionally accepts
`?access_token=`; that is the only route that does, and requests authenticated that way are
marked as such in the audit record.

## Status code taxonomy

The dividing line is whether the request ever crossed into ROS.

**4xx: it did not.** Nothing was published, called or sent. Retrying the identical request
will fail the same way.

| Status | When |
|---|---|
| 400 | Malformed body, a path parameter that will not convert, or a value outside the field's range |
| 401 | Missing, malformed or unknown bearer token |
| 403 | Valid token whose roles do not include one of the endpoint's `roles` |
| 404 | Unknown route, or an unknown or evicted `goal_id` |

**503: a capacity limit was reached.** Also never reached ROS, but unlike a 4xx the identical
request may succeed later: `PUBLISH_QUEUE_FULL` when the ROS side is not draining published
messages fast enough, `TOO_MANY_CLIENTS` when the event stream is at `sse.max_clients`.

**200: it did, and this is the outcome.** The call was authorised and well-formed. Success
and failure are both reported in the body, because "the service returned false" and "the
service timed out" are results, not protocol errors.

| `error_code` | Meaning |
|---|---|
| `null` | Success |
| `TIMEOUT` | The service or action did not answer within `timeout_ms` |
| `SERVICE_UNAVAILABLE` | No server was offering that service |
| `PRECONDITION_FAILED` | A configured precondition denied the call |
| `GOAL_REJECTED` | The action server refused the goal |

## Error envelope

Both classes use the same shape, so a client parses one thing:

```json
{
  "success": false,
  "error_code": "PRECONDITION_FAILED",
  "message": "start is not allowed while state is RUNNING"
}
```

For a 400 from field conversion, `message` begins with the dotted path of the offending
field, exactly as the client wrote it, and `field` carries it separately:

```json
{
  "success": false,
  "error_code": "BAD_REQUEST",
  "message": "header.stamp.sec: 2147483648 is outside [-2147483648, 2147483647]",
  "field": "header.stamp.sec"
}
```

Collection elements are marked with `[]`: `name[]`, `status[].values[].key`.

Request bodies are read strictly: a JSON number must be a number, a JSON boolean must be a
boolean, and a string is neither. Two exceptions exist so that a response body is always a
legal request body: `int64`/`uint64` also accept a string, and float fields also accept
`null`, which reads back as NaN. Path parameters are coerced against their target field
before conversion, so the schemas never have to advertise string-for-number.

## Topics

### `GET <base>/<endpoint>`

```json
{
  "data": { "...": "the message" },
  "stamp": 1780000000.123,
  "seq": 4711,
  "stale": false
}
```

`stamp` is the wall-clock time the sample arrived, `seq` a monotonic counter per endpoint.
`stale` is true when the newest sample is older than `stale_after_ms`.

Before any message has arrived the body is exactly:

```json
{"data": null, "stamp": null, "seq": 0, "stale": true}
```

A 200 rather than a 404, because the endpoint is configured and reachable; there is simply
nothing on the topic yet. Stale data is still returned once it exists, flagged rather than
withheld: an HMI showing a value greyed out is more useful than one showing nothing.

A publish returns as soon as the message is queued for the ROS side. If a client publishes
faster than ROS drains, the queue fills and further publishes get 503 with
`PUBLISH_QUEUE_FULL` rather than blocking the event loop.

### `POST <base>/<endpoint>` (publish)

Body is the message. Returns 200 `{"success": true}` once the publish is queued to the ROS
thread; the gateway does not wait for subscribers.

## Services

### `POST <base>/<endpoint>`

Body fields map onto the request message by name. `request_map` entries take precedence and
may draw on path parameters.

```json
{
  "success": true,
  "message": "",
  "error_code": null,
  "response": { "...": "the service response" }
}
```

`success` reflects the call, not the robot's opinion: a service that answers
`{"success": false}` in its own response still produces HTTP 200 with `"success": true` at
this layer and its own fields under `response`. Conflating the two would leave a client unable
to distinguish "the gateway could not ask" from "the robot said no". On `TIMEOUT` or
`SERVICE_UNAVAILABLE`, `response` is `null`.

A timed-out request is withdrawn from the client, so a late reply can never be mistaken for
the answer to a subsequent call.

`SERVICE_UNAVAILABLE` reflects readiness at the moment of the call. A server that disappears
between the readiness check and the call, or mid-call, reports `TIMEOUT` after the full wait
rather than `SERVICE_UNAVAILABLE`.

### Preconditions

A configured precondition is evaluated before the call is forwarded. A refusal is HTTP 200
with `PRECONDITION_FAILED`, and is audited as `decision: deny` with the reason:

```json
{
  "success": false,
  "message": "data is 'IDLE', which does not permit this call (allowed: PAUSED, RUNNING)",
  "error_code": "PRECONDITION_FAILED",
  "response": null
}
```

Three situations refuse rather than pass, because a gate that fails open is not a gate:

- the path parameter's value is not listed in `allow`;
- the state endpoint has received nothing yet, so the state is unknown;
- its newest sample is older than its own `stale_after_ms`. Deciding it is safe to start
  because the robot was idle a minute ago is the mistake preconditions exist to prevent.

The last one is worth planning for: a precondition on a topic with a staleness threshold makes
the gate depend on that topic still being published.

Authentication and the role check run *before* policy, so a caller who may not use an endpoint
learns nothing about the robot's state from the refusal.

## Actions

An action endpoint at `/pick` generates four routes.

| Route | Result |
|---|---|
| `POST <base>/pick` | 202 `{"goal_id": "<uuid>"}`, or 200 with `GOAL_REJECTED` |
| `GET <base>/pick/{goal_id}` | Status, result and latest feedback |
| `GET <base>/pick/{goal_id}/stream` | SSE: feedback events, then one terminal event, then close |
| `DELETE <base>/pick/{goal_id}` | Requests cancellation; 200 with the outcome |

```json
{
  "status": "executing",
  "result": null,
  "last_feedback": { "...": "the newest feedback" }
}
```

`status` is one of `accepted`, `executing`, `canceling`, `succeeded`, `aborted`,
`canceled`. A terminal status always comes with a `result`: the underlying goal handle reports
terminal slightly before the result has been collected, and the gateway deliberately keeps
reporting the running status until it has both, so a client may treat a terminal status as
"the result is ready". A terminal goal stays queryable for `actions.result_ttl_s` (default 300) and
then returns 404 with `UNKNOWN_GOAL`, the same as an id that never existed.

Goal ids are namespaced per endpoint: two endpoints pointing at the same action do not share
them, so an id from one cannot be read through the other's routes.

`SERVICE_UNAVAILABLE` when no action server is offering the action; `GOAL_REJECTED` when one is
and refuses the goal. Neither leaves a record behind, so no id is returned.

Cancel answers `200 {"success": true}` once the server accepts the request; the goal then moves
to `canceling` and reaches `canceled` when the server confirms. `CANCEL_REJECTED` if the server
declines, `ALREADY_TERMINAL` if it has already finished.

**Preconditions apply only to goal submission.** Status, stream and cancel are not gated. A
state gate able to stop an operator cancelling a goal would be the opposite of a safety
feature.

`timeout_ms` bounds the accept/reject handshake, not the goal. An action is long-running by
definition, so there is no deadline on its result.

The per-goal stream keeps a bounded backlog per client (64 frames, not configurable, unlike the
global stream's `sse.overflow`). A client that cannot keep up loses its **oldest** queued
feedback rather than stalling the goal, so **gaps in feedback are expected behaviour under a
slow reader, not a fault**: ordering is preserved and the terminal event is never dropped.
Feedback is progress reporting; if you need every message, read the action's feedback topic
directly.

Because a derived route could otherwise be shadowed, a config where another endpoint would
collide with `/pick/{goal_id}` is rejected at startup.

## Server-sent events

`GET <base><sse.path>` (default `/api/v1/events`).

```
event: /mission/status
data: {"data":{...},"stamp":1780000000.1,"seq":12,"stale":false}
```

The event name is the endpoint key and `data` is byte-identical to that endpoint's GET body:
the same memoised encoding backs both, so one parser serves both routes.

On connect, one snapshot per included endpoint, then deltas. Endpoints that have received
nothing yet are in the snapshot too, with the same `"data": null` body a GET would return, so
a client learns the whole endpoint set up front. A comment frame every 15 seconds keeps
proxies from idling the connection out. Past `sse.max_clients` the route answers 503 with
`TOO_MANY_CLIENTS`.

**Each client sees only the endpoints its own roles permit.** One connection carries several
endpoints, so filtering is per client rather than per stream: including a restricted endpoint
cannot hand it to a caller who may not GET it. A token holding none of the included endpoints'
roles gets 403 at connect.

Browser `EventSource` cannot set headers, so this route also accepts `?access_token=`. It is
the only route that does, and the audit record for such a request carries `"credential":
"query"` -- the query string itself is never logged.

Only the newest sample of an endpoint is streamed. A topic published faster than the loop
drains will therefore show gaps in `seq`, exactly as a client polling the GET route would miss
samples between polls; this is a latest-value gateway, not a message bus. If you need every
message, subscribe to the topic in ROS.

A client that cannot keep up is handled per `sse.overflow`: `drop_oldest` (default) discards
its **oldest** queued events, so the newest state always arrives, while `disconnect` closes it.
One slow client never slows a fast one.

Action feedback is not carried here; it is streamed per goal from
`/pick/{goal_id}/stream`.

`SIGTERM` ends every open stream before the server waits for in-flight requests, so a
connected stream cannot hold shutdown open.

## Process lifecycle

`SIGTERM` and `SIGINT` both stop the gateway: it stops accepting, lets in-flight requests
finish, flushes the audit queue and destroys the ROS node, in that order. Ctrl-C exits 130
without a traceback; `SIGTERM` propagates as usual, which is what an init system expects.

## Audit

One JSON line per authenticated request: timestamp, user, roles, method, path, decision and
its reason, outcome, latency in milliseconds. Never tokens, never request bodies, never the
query string. A `credential` field appears only when the token did not arrive in the
`Authorization` header, which today means the stream's `?access_token=`.

For a stream the latency is the time to accept the connection, not its lifetime.

## OpenAPI

`GET <base>/openapi.json` serves an OpenAPI 3.1 document generated at startup from the
config and the introspected interface types. Swagger UI is served from `<base>/docs` from
vendored assets, with no CDN.

Both honour the ASGI `root_path` and `X-Forwarded-Prefix`, so a gateway mounted at
`location /cell3/` in nginx advertises and links prefix-correct URLs.
