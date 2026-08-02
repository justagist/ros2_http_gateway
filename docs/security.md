# Security model

What this gateway does and does not protect, stated plainly so that a reviewer does not have
to infer it from the code.

## What it is for

Exposing a small, named set of ROS topics, services and actions over HTTP to clients that
are already inside a trust boundary you control: an HMI on the cell network, a fleet
dashboard behind a VPN, a customer integration behind a reverse proxy. It is not an
internet-facing API and it is not an identity provider.

## Deny by default

Nothing is reachable unless an endpoint names it. There is no graph discovery, no wildcard
exposure, and no way to reach a topic by guessing its name. A config typo cannot fail open:
unknown keys are rejected, an endpoint with no roles is rejected, and two routes that could
answer the same request are rejected.

## Authentication and authorisation

- `Authorization: Bearer <token>`, compared against the configured SHA-256 digests with
  `hmac.compare_digest`. Every configured digest is compared even after a match, so the time
  taken does not reveal which token was presented or where it sits in the file.
- Roles are per endpoint. A token with no matching role gets 403, and the call never reaches
  ROS.
- Plaintext `token:` entries are hashed on load, warned about, and **refused outright if the
  config file is world-readable**.
- The plaintext token is never retained in memory after load, and never logged.

### What the tokens are not

Static bearer tokens have no expiry, no rotation and no revocation beyond editing the config
and restarting. They identify a *station*, not a person. If you need per-operator identity,
put an authenticating proxy in front; `Authenticator` is a protocol so that JWT or OIDC can
be added without touching the request path.

## Transport

The gateway does not terminate TLS and will not in v1. Bind it to localhost and put nginx,
Caddy or Traefik in front. Binding to `0.0.0.0` is possible, must be explicit, and logs a
warning.

## Audit

One JSON line per request that reaches a configured route, whether it was allowed, denied by
policy, or failed inside a handler. Records carry the user, roles, method, path, decision and
reason, status, outcome and latency. There is no field for a token or a request body, so
neither can be logged by accident.

### Known gap: unmatched routes are not audited

A request that matches no configured route gets Starlette's 404 and leaves **no audit
record**. The reasoning is that there is no endpoint, so there was no policy and no decision
to record.

The consequence is worth stating: **path-scanning probes leave no trace in the audit file.**
Someone walking `/api/v1/admin`, `/api/v1/../../etc/passwd` and so on is invisible to this
log. If you need that visibility today, take it from the reverse proxy's access log, which
sees every request including the ones that never match.

Accepted for v1 because the audit log's purpose is answering "who did what to the robot",
and a request that reached no endpoint did nothing. A counter of unmatched requests, or an
opt-in catch-all record, is cheap and is the obvious way to close this if it matters to you.

## Denial of service

Not a design goal, but the obvious footguns are bounded:

- The audit queue is bounded; overflow drops records, counts them, and says so in the log and
  in the next record written.
- The publish queue is bounded; a client publishing faster than ROS drains gets 503 rather
  than growing memory or blocking the event loop.
- SSE has a `max_clients` limit and per-client bounded queues.
- The event stream filters per client by role, so it cannot become a way to read an endpoint
  the caller may not GET.
- The stream accepts `?access_token=` because browser `EventSource` cannot set headers. This is
  the one place a credential can appear in a URL, so it can also appear in the access log of
  any proxy in front of the gateway. The gateway logs the path without its query string and
  marks the record `"credential": "query"`; if that trade is unacceptable, do not configure an
  `sse` block, or terminate EventSource in your own front end and use the header.

There is no rate limiting and no request size limit. Put both in the reverse proxy.

## The OpenAPI document and Swagger UI

- `<base>/openapi.json` is authenticated. An anonymous caller cannot enumerate the configured
  surface from it.
- Any authenticated caller sees the whole document, including endpoints their roles cannot use.
  This is deliberate rather than an oversight: an unknown path answers 404 and a known one with
  the wrong role answers 403, so the same list is already enumerable by asking. Per-role
  filtering would be theatre while that is true.
- `<base>/docs` serves an unauthenticated HTML shell, because a browser cannot attach a header
  to a top-level navigation. The shell contains no information about the API; it prompts for a
  token and fetches the document with it. The token is held in the page for the life of the tab,
  never in `localStorage` or `sessionStorage`, and never placed in a URL.
- Swagger UI is vendored (~1.7 MB under `ros2_http_gateway/static/swagger/`, Apache-2.0, notices
  alongside it). Nothing is fetched from a CDN, so the browser talks only to your gateway, and a
  test asserts the served page contains no external URL.

## Reporting

Security issues: open a GitHub issue for anything already public, or email the maintainer
listed in `package.xml` for anything that is not.
