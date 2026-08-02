// The console's skeleton: authenticate, then list what the manifest says this token may touch.
//
// Phase 7a deliberately stops here. Every widget the later phases add -- topic tiles, schema
// driven forms, goal cards, the stream panel -- is built from the same manifest, so nothing
// about an endpoint is hardcoded in this file and nothing needs a build step.
(function () {
  "use strict";

  var api = document.currentScript.dataset.api;
  var gate = document.getElementById("gate");
  var error = document.getElementById("error");
  var identity = document.getElementById("identity");
  var endpoints = document.getElementById("endpoints");
  var token = null;

  function authorised(extra) {
    var headers = { Authorization: "Bearer " + token };
    return Object.assign(headers, extra || {});
  }

  function get(path) {
    return fetch(api + path, { headers: authorised() }).then(function (response) {
      if (!response.ok) {
        throw new Error(path + " answered " + response.status);
      }
      return response.json();
    });
  }

  function connect() {
    token = document.getElementById("token").value.trim();
    if (!token) {
      error.textContent = "A token is required.";
      return;
    }
    error.textContent = "";
    get("/whoami")
      .then(function (who) {
        identity.textContent = who.user + " [" + who.roles.join(", ") + "]";
        return get("/console/manifest");
      })
      .then(show)
      .catch(function (failure) {
        error.textContent = failure.message;
      });
  }

  function show(manifest) {
    gate.hidden = true;
    endpoints.hidden = false;
    endpoints.replaceChildren();
    if (!manifest.endpoints.length) {
      endpoints.append(element("p", "This token may not use any configured endpoint."));
      return;
    }
    manifest.endpoints.forEach(function (endpoint) {
      endpoints.append(card(endpoint));
    });
    if (manifest.sse) {
      endpoints.append(
        element("p", "Event stream: " + manifest.sse.url + " (" + manifest.sse.include.join(", ") + ")", "note")
      );
    }
  }

  function card(endpoint) {
    var node = element("article", "", "card " + endpoint.kind);
    node.append(element("h2", endpoint.method + " " + endpoint.path));
    node.append(element("div", endpoint.kind.replace("_", " ") + " -- " + endpoint.ros_name, "kind"));
    node.append(element("div", endpoint.type, "type"));

    var facts = [];
    facts.push("roles: " + endpoint.roles.join(", "));
    if (endpoint.stale_after_ms != null) facts.push("stale after " + endpoint.stale_after_ms + " ms");
    if (endpoint.timeout_ms != null) facts.push("timeout " + endpoint.timeout_ms + " ms");
    if (endpoint.sse) facts.push("on the event stream");
    node.append(element("div", facts.join(" | "), "facts"));

    (endpoint.preconditions || []).forEach(function (gateRule) {
      node.append(
        element(
          "div",
          "gated on " + gateRule.field + " of " + gateRule.state_endpoint + ": " + JSON.stringify(gateRule.allow),
          "gate"
        )
      );
    });
    return node;
  }

  function element(tag, text, className) {
    var node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  document.getElementById("connect").addEventListener("click", connect);
  document.getElementById("token").addEventListener("keydown", function (event) {
    if (event.key === "Enter") connect();
  });
  document.getElementById("token").focus();
})();
