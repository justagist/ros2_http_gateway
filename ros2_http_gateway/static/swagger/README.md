swagger-ui-dist 5.29.0, vendored so /docs needs no CDN.

Upstream: https://github.com/swagger-api/swagger-ui (Apache-2.0, see LICENSE).
Bundled dependency notices are in swagger-ui-bundle.js.LICENSE.txt.

index.html is ours, not upstream's: it fetches the document with a bearer token, since
the gateway does not serve its OpenAPI document unauthenticated.
