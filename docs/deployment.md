# Deployment

The gateway does not terminate TLS, has no rate limit and no request size limit. It binds
`127.0.0.1` unless told otherwise, and binding `0.0.0.0` logs a warning. All of that is the reverse
proxy's job, and this file is how to do it.

## Behind one nginx

The proxy terminates TLS and, if you want, mounts several cells under one host. Tell the gateway
which prefix it was reached through and its OpenAPI `servers`, its Swagger UI asset URLs, its
console asset URLs and its "Try it out" requests all come out prefix-correct.

```nginx
upstream cell3 { server 127.0.0.1:8081; keepalive 8; }
upstream cell4 { server 127.0.0.1:8082; keepalive 8; }

server {
    listen 443 ssl;
    server_name cells.example.com;

    ssl_certificate     /etc/ssl/certs/cells.pem;
    ssl_certificate_key /etc/ssl/private/cells.key;

    # One block per gateway. The trailing slash on both sides strips the prefix before it reaches
    # the gateway, and X-Forwarded-Prefix hands it back so generated URLs keep it.
    location /cell3/ {
        proxy_pass http://cell3/;
        proxy_set_header X-Forwarded-Prefix /cell3;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Connection "";     # keep the upstream connection alive

        # The event stream and the per-goal feedback streams need all three. Without
        # `proxy_buffering off` nginx holds events until the response ends, which for a stream is
        # never; the gateway also sends `X-Accel-Buffering: no`, but do not rely on one of the two.
        proxy_buffering off;
        gzip off;
        proxy_read_timeout 1h;

        client_max_body_size 256k;          # the gateway has no limit of its own
        limit_req zone=api burst=20 nodelay;
    }

    location /cell4/ {
        proxy_pass http://cell4/;
        proxy_set_header X-Forwarded-Prefix /cell4;
        include /etc/nginx/snippets/gateway-common.conf;
    }
}
```

`https://cells.example.com/cell3/api/v1/docs` then loads Swagger UI, whose document says
`servers: [{"url": "/cell3/api/v1"}]`, so every call it makes lands on the right gateway.

Two things to get right: the SSE heartbeat is every 15 s, so `proxy_read_timeout` must be well
above that; and `limit_req` with `client_max_body_size` are the proxy's job precisely because the
gateway deliberately has neither.

## Prefix resolution

Both `X-Forwarded-Prefix` and the ASGI `root_path` are honoured. The header wins when both are
present: a proxy asserting a prefix on the request knows more than a `root_path` fixed at startup.

Everything the gateway generates follows it -- the OpenAPI `servers` entry, both UI shells' asset
URLs, and every URL in the console manifest -- so a client needs no knowledge of the prefix.

## Tokens

Use `token_sha256:` in any config that leaves your machine. A plaintext `token:` is hashed on load
and warned about, and is refused outright when the config file is world-readable, which a git
checkout always is.

```bash
printf %s "$TOKEN" | sha256sum
```

Static bearer tokens have no expiry, no rotation and no revocation beyond editing the config and
restarting, and they identify a *station* rather than a person. If you need per-operator identity,
put an authenticating proxy in front; `Authenticator` is a protocol so JWT or OIDC can be added
without touching the request path.

## Process lifecycle

`SIGTERM` and `SIGINT` both stop the gateway: it stops accepting, ends every open event stream,
lets in-flight requests finish, flushes the audit queue and destroys the ROS node, in that order.
Ctrl-C exits 130 without a traceback; `SIGTERM` propagates as usual, which is what an init system
expects.

`GET <base>/health` is the only unauthenticated route, and is what a readiness probe should use. It
reports the chosen executor too, which is the quickest way to confirm which one `auto` picked.
