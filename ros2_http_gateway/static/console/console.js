// The test console. Everything it shows comes from /console/manifest and the schemas in the
// OpenAPI document; no endpoint, type or route is named in this file.
//
// Two halves. The first is pure logic -- schema to form, response to taxonomy, staleness,
// percentiles, gate verdicts -- with no DOM in it, exported so it can be run and tested outside a
// browser. The second wires it to the page and only runs where there is a document. Keeping the
// seam there is what makes the interesting parts testable without a browser at all.
(function (root) {
  "use strict";

  // ==================================================================== pure logic

  var SCALARS = ["string", "integer", "number", "boolean"];

  // A body no generated converter will accept: unknown fields are rejected rather than ignored.
  // The auth matrix sends this to anything that is not a GET, so the authorised column proves the
  // token and its roles were accepted without publishing, calling or submitting anything. 400 is
  // reached only after auth and the role check have passed.
  var PROBE = { __console_probe__: true };

  function typesOf(schema) {
    var declared = schema && schema.type;
    if (!declared) return [];
    return Array.isArray(declared) ? declared : [declared];
  }

  function isScalar(schema) {
    var types = typesOf(schema).filter(function (name) {
      return name !== "null";
    });
    if (!types.length) return false;
    return types.every(function (name) {
      return SCALARS.indexOf(name) >= 0;
    });
  }

  function nullable(schema) {
    return typesOf(schema).indexOf("null") >= 0;
  }

  // A widget kind per scalar, so the form can use a checkbox for a boolean and a number input
  // for something bounded, without knowing anything about ROS.
  function widgetFor(schema) {
    var types = typesOf(schema).filter(function (name) {
      return name !== "null";
    });
    if (schema.enum) return "enum";
    if (types.indexOf("boolean") >= 0) return "boolean";
    if (types.length === 1 && (types[0] === "integer" || types[0] === "number")) return "number";
    return "text";
  }

  function deref(schema, resolve) {
    var seen = 0;
    while (schema && schema.$ref && seen < 8) {
      schema = resolve(schema.$ref);
      seen += 1;
    }
    return schema || {};
  }

  // Flat fields, plus one level of nesting. Anything deeper, and anything with an array or a
  // choice in it, goes to the raw editor: guessing a widget for those is how a form starts lying
  // about what it will send.
  function formPlan(schema, resolve) {
    schema = deref(schema, resolve);
    if (typesOf(schema).indexOf("object") < 0 || !schema.properties) {
      return { mode: "raw", reason: "the body is not an object", fields: [] };
    }
    var fields = [];
    var names = Object.keys(schema.properties);
    for (var i = 0; i < names.length; i += 1) {
      var name = names[i];
      var property = deref(schema.properties[name], resolve);
      if (isScalar(property)) {
        fields.push(scalarField([name], property));
        continue;
      }
      if (typesOf(property).indexOf("object") >= 0 && property.properties) {
        var nested = Object.keys(property.properties);
        for (var j = 0; j < nested.length; j += 1) {
          var inner = deref(property.properties[nested[j]], resolve);
          if (!isScalar(inner)) {
            return {
              mode: "raw",
              reason: name + "." + nested[j] + " is deeper than one level",
              fields: [],
            };
          }
          fields.push(scalarField([name, nested[j]], inner));
        }
        continue;
      }
      return { mode: "raw", reason: name + " is an array or a choice", fields: [] };
    }
    return { mode: "form", reason: "", fields: fields };
  }

  function scalarField(path, schema) {
    return {
      path: path,
      name: path.join("."),
      widget: widgetFor(schema),
      nullable: nullable(schema),
      minimum: schema.minimum,
      maximum: schema.maximum,
      maxLength: schema.maxLength,
      options: schema.enum || null,
      schema: schema,
    };
  }

  // Assemble {"a.b": "1"} into {a: {b: 1}}, coercing per widget. A blank field is omitted, which
  // the write schemas allow: an absent field keeps the ROS default.
  function assemble(fields, values) {
    var body = {};
    fields.forEach(function (field) {
      var raw = values[field.name];
      if (raw === undefined || raw === "") return;
      var value = raw;
      if (field.widget === "boolean") value = raw === true || raw === "true";
      else if (field.widget === "number") value = Number(raw);
      var target = body;
      for (var i = 0; i < field.path.length - 1; i += 1) {
        target[field.path[i]] = target[field.path[i]] || {};
        target = target[field.path[i]];
      }
      target[field.path[field.path.length - 1]] = value;
    });
    return body;
  }

  // A validator for the subset the gateway's own generator emits, so the raw editor can refuse a
  // body before a round trip instead of after one. Not a general JSON Schema implementation.
  function validate(value, schema, resolve, where) {
    where = where || "body";
    schema = deref(schema, resolve);
    var problems = [];
    if (schema.const !== undefined && value !== schema.const) {
      return [where + ": must be " + JSON.stringify(schema.const)];
    }
    if (schema.enum && schema.enum.indexOf(value) < 0) {
      return [where + ": must be one of " + JSON.stringify(schema.enum)];
    }
    if (schema.oneOf || schema.anyOf) {
      var branches = schema.oneOf || schema.anyOf;
      var matched = branches.some(function (branch) {
        return validate(value, branch, resolve, where).length === 0;
      });
      return matched ? [] : [where + ": matches none of the accepted forms"];
    }
    var types = typesOf(schema);
    if (types.length && !types.some(function (name) {
        return matchesType(value, name);
      })) {
      return [where + ": expected " + types.join(" or ") + ", got " + describe(value)];
    }
    if (types.indexOf("object") >= 0 && value && typeof value === "object") {
      (schema.required || []).forEach(function (name) {
        if (!(name in value)) problems.push(where + ": missing " + name);
      });
      Object.keys(value).forEach(function (name) {
        var property = schema.properties && schema.properties[name];
        if (!property) {
          if (schema.additionalProperties === false) {
            problems.push(where + ": unknown field " + name);
          }
          return;
        }
        problems = problems.concat(validate(value[name], property, resolve, where + "." + name));
      });
    }
    if (types.indexOf("array") >= 0 && Array.isArray(value)) {
      if (schema.minItems !== undefined && value.length < schema.minItems) {
        problems.push(where + ": needs at least " + schema.minItems + " items");
      }
      if (schema.maxItems !== undefined && value.length > schema.maxItems) {
        problems.push(where + ": takes at most " + schema.maxItems + " items");
      }
      if (schema.items) {
        value.forEach(function (item, index) {
          problems = problems.concat(
            validate(item, schema.items, resolve, where + "[" + index + "]")
          );
        });
      }
    }
    if (typeof value === "number") {
      if (schema.minimum !== undefined && value < schema.minimum) {
        problems.push(where + ": below " + schema.minimum);
      }
      if (schema.maximum !== undefined && value > schema.maximum) {
        problems.push(where + ": above " + schema.maximum);
      }
    }
    if (typeof value === "string" && schema.maxLength !== undefined) {
      if (value.length > schema.maxLength) {
        problems.push(where + ": longer than " + schema.maxLength);
      }
    }
    return problems;
  }

  function matchesType(value, name) {
    if (name === "null") return value === null;
    if (name === "array") return Array.isArray(value);
    if (name === "object") return value !== null && typeof value === "object" && !Array.isArray(value);
    if (name === "integer") return typeof value === "number" && Number.isInteger(value);
    if (name === "number") return typeof value === "number";
    if (name === "boolean") return typeof value === "boolean";
    if (name === "string") return typeof value === "string";
    return false;
  }

  function describe(value) {
    if (value === null) return "null";
    if (Array.isArray(value)) return "an array";
    return typeof value;
  }

  // The protocol taxonomy, as visually distinct outcomes. One function, so every panel classifies
  // a response the same way.
  //
  // A precondition refusal gets its own outcome rather than being lumped in with a ROS-side
  // failure. Both are 200s carrying an `error_code`, but the gate is decided by the gateway before
  // the call is forwarded -- nothing reached ROS -- and it is the one outcome the gate box beside
  // the button is there to predict. Calling it a ROS error would misdescribe it and hide the
  // connection.
  function classify(status, body) {
    if (status === 0) return "unreachable";
    if (status === 503) return "capacity";
    if (status >= 400) return "refused";
    if (body && body.error_code === "PRECONDITION_FAILED") return "gated";
    if (body && body.error_code) return "ros_error";
    return "ok";
  }

  function explain(outcome) {
    if (outcome === "refused") return "4xx -- never reached ROS";
    if (outcome === "capacity") return "503 -- a limit was reached, nothing reached ROS";
    if (outcome === "gated") return "200 -- refused by a state precondition, nothing reached ROS";
    if (outcome === "ros_error") return "200 -- reached ROS, which reported a problem";
    if (outcome === "unreachable") return "the gateway did not answer";
    return "200 -- success";
  }

  // Staleness is measured from when *this browser* received the sample, never from the server's
  // wall-clock stamp: the two clocks are unrelated and a skewed one would grey a live tile. The
  // server's own `stale` flag stays authoritative once it says so.
  function staleness(sample, staleAfterMs, elapsedMs) {
    if (!sample || sample.seq === 0) {
      return { state: "empty", remainingMs: null };
    }
    if (staleAfterMs == null) {
      return { state: "fresh", remainingMs: null };
    }
    if (sample.stale) {
      return { state: "stale", remainingMs: 0 };
    }
    var remaining = staleAfterMs - elapsedMs;
    if (remaining <= 0) return { state: "stale", remainingMs: 0 };
    return { state: remaining < staleAfterMs / 4 ? "ageing" : "fresh", remainingMs: remaining };
  }

  function percentiles(samples) {
    if (!samples.length) return { p50: null, p95: null, count: 0 };
    var ordered = samples.slice().sort(function (a, b) {
      return a - b;
    });
    return {
      p50: ordered[Math.min(ordered.length - 1, Math.floor(ordered.length * 0.5))],
      p95: ordered[Math.min(ordered.length - 1, Math.floor(ordered.length * 0.95))],
      count: ordered.length,
    };
  }

  // Read a dotted field out of a decoded sample, which is how a precondition names its state.
  function extract(data, dotted) {
    var value = data;
    var parts = dotted.split(".");
    for (var i = 0; i < parts.length; i += 1) {
      if (value === null || typeof value !== "object") return undefined;
      value = value[parts[i]];
    }
    return value;
  }

  // What the gateway will decide, computed the same way it decides it: the allow map is keyed by
  // the path parameter's value, "*" matches any key or any state, and an unlisted key is a denial.
  function gateVerdict(precondition, sample, paramValue) {
    var allow = precondition.allow || {};
    var permitted = paramValue != null && allow[paramValue] ? allow[paramValue] : allow["*"];
    if (!permitted) return { verdict: "blocked", reason: "not a permitted value here" };
    if (permitted.indexOf("*") >= 0) return { verdict: "allowed", reason: "any state" };
    if (!sample || sample.seq === 0) {
      return { verdict: "blocked", reason: "the state endpoint has published nothing" };
    }
    if (sample.stale) return { verdict: "blocked", reason: "the state is stale" };
    var current = extract(sample.data, precondition.field);
    if (current === undefined) return { verdict: "unknown", reason: "field not in the sample" };
    var text = typeof current === "boolean" ? (current ? "true" : "false") : String(current);
    return permitted.indexOf(text) >= 0
      ? { verdict: "allowed", reason: text }
      : { verdict: "blocked", reason: text + " is not in " + permitted.join(", ") };
  }

  // Substitute path parameters into an endpoint's URL template.
  function fill(url, params) {
    return url.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, function (whole, name) {
      var value = params[name];
      return value === undefined || value === "" ? whole : encodeURIComponent(value);
    });
  }

  var logic = {
    PROBE: PROBE,
    assemble: assemble,
    classify: classify,
    explain: explain,
    extract: extract,
    fill: fill,
    formPlan: formPlan,
    gateVerdict: gateVerdict,
    percentiles: percentiles,
    staleness: staleness,
    validate: validate,
  };
  root.gatewayConsole = logic;

  // Outside a browser (a test runner, say) the logic above is the whole point and there is
  // nothing to wire it to.
  if (typeof document === "undefined") return;

  // ======================================================================= the page

  var api = document.currentScript.dataset.api;
  var WALLET_KEY = "ros2_http_gateway.wallet";
  var BAD_TOKEN = "not-a-real-token";
  var POLL_MS = 1000;

  var wallet = loadWallet();
  var manifest = null;
  var schemas = {};
  var polls = {};

  var elements = {
    gate: document.getElementById("gate"),
    error: document.getElementById("error"),
    identity: document.getElementById("identity"),
    wallet: document.getElementById("wallet"),
    endpoints: document.getElementById("endpoints"),
    label: document.getElementById("label"),
    token: document.getElementById("token"),
  };

  // ------------------------------------------------------------------ the wallet

  function loadWallet() {
    try {
      var stored = JSON.parse(window.localStorage.getItem(WALLET_KEY) || "null");
      if (stored && Array.isArray(stored.tokens)) return stored;
    } catch (ignored) {
      // Corrupt or unavailable storage is not worth a failure: start empty.
    }
    return { tokens: [], active: null };
  }

  function saveWallet() {
    try {
      window.localStorage.setItem(WALLET_KEY, JSON.stringify(wallet));
    } catch (ignored) {
      // Private browsing, or storage disabled. The session still works, it just will not persist.
    }
  }

  function activeToken() {
    var found = wallet.tokens.filter(function (entry) {
      return entry.label === wallet.active;
    });
    return found.length ? found[0].token : null;
  }

  function addToken(label, token) {
    wallet.tokens = wallet.tokens.filter(function (entry) {
      return entry.label !== label;
    });
    wallet.tokens.push({ label: label, token: token });
    wallet.active = label;
    saveWallet();
  }

  function renderWallet() {
    elements.wallet.replaceChildren();
    if (!wallet.tokens.length) return;
    wallet.tokens.forEach(function (entry) {
      var chip = node("button", entry.label, "token-chip" + (entry.label === wallet.active ? " active" : ""));
      chip.type = "button";
      chip.title = "switch to this token";
      chip.addEventListener("click", function () {
        wallet.active = entry.label;
        saveWallet();
        connect();
      });
      var drop = node("button", "x", "token-drop");
      drop.type = "button";
      drop.title = "forget this token";
      drop.addEventListener("click", function (event) {
        event.stopPropagation();
        wallet.tokens = wallet.tokens.filter(function (other) {
          return other.label !== entry.label;
        });
        if (wallet.active === entry.label) wallet.active = null;
        saveWallet();
        renderWallet();
      });
      var holder = node("span", "", "token-holder");
      holder.append(chip, drop);
      elements.wallet.append(holder);
    });
  }

  // ------------------------------------------------------------------- requests

  function send(method, url, options) {
    options = options || {};
    var headers = {};
    var token = "token" in options ? options.token : activeToken();
    if (token) headers.Authorization = "Bearer " + token;
    var init = { method: method, headers: headers };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    var started = performance.now();
    return fetch(url, init).then(
      function (response) {
        return response
          .text()
          .then(function (text) {
            var body = null;
            try {
              body = text ? JSON.parse(text) : null;
            } catch (ignored) {
              body = { message: text };
            }
            return {
              status: response.status,
              body: body,
              ms: performance.now() - started,
              outcome: logic.classify(response.status, body),
            };
          });
      },
      function () {
        return { status: 0, body: null, ms: performance.now() - started, outcome: "unreachable" };
      }
    );
  }

  function connect() {
    var token = activeToken();
    if (!token) {
      elements.error.textContent = "Add a token first.";
      return;
    }
    elements.error.textContent = "";
    send("GET", api + "/whoami")
      .then(function (who) {
        if (who.status !== 200) throw new Error("whoami answered " + who.status);
        elements.identity.textContent = who.body.user + " [" + who.body.roles.join(", ") + "]";
        return send("GET", api + "/console/manifest");
      })
      .then(function (answer) {
        if (answer.status !== 200) throw new Error("the manifest answered " + answer.status);
        manifest = answer.body;
        return send("GET", manifest.openapi);
      })
      .then(function (document_) {
        schemas = (document_.body && document_.body.components.schemas) || {};
        render();
      })
      .catch(function (failure) {
        elements.error.textContent = failure.message;
      });
  }

  function resolver(ref) {
    return schemas[ref.replace("#/components/schemas/", "")];
  }

  // --------------------------------------------------------------------- render

  function render() {
    renderWallet();
    elements.gate.hidden = false;
    elements.endpoints.hidden = false;
    stopPolling();
    elements.endpoints.replaceChildren();
    if (!manifest.endpoints.length) {
      elements.endpoints.append(node("p", "This token may not use any configured endpoint."));
      return;
    }
    manifest.endpoints.forEach(function (endpoint) {
      elements.endpoints.append(card(endpoint));
    });
  }

  function card(endpoint) {
    var holder = node("article", "", "card " + endpoint.kind);
    holder.append(node("h2", endpoint.method + " " + endpoint.path));
    holder.append(node("div", endpoint.kind.replace("_", " ") + " -- " + endpoint.ros_name, "kind"));
    holder.append(node("div", endpoint.type, "type"));

    var facts = ["roles: " + endpoint.roles.join(", ")];
    if (endpoint.stale_after_ms != null) facts.push("stale after " + endpoint.stale_after_ms + " ms");
    if (endpoint.timeout_ms != null) facts.push("timeout " + endpoint.timeout_ms + " ms");
    if (endpoint.sse) facts.push("on the event stream");
    holder.append(node("div", facts.join(" | "), "facts"));

    var params = {};
    if (endpoint.path_params.length) holder.append(paramInputs(endpoint, params));

    if (endpoint.kind === "topic_read") holder.append(readPanel(endpoint));
    else if (endpoint.kind === "topic_write") holder.append(writePanel(endpoint, params));
    else if (endpoint.kind === "service") holder.append(servicePanel(endpoint, params));
    else holder.append(node("p", "Goal submission and streaming arrive with the next phase.", "note"));

    holder.append(matrixPanel(endpoint, params));
    return holder;
  }

  function paramInputs(endpoint, params) {
    var box = node("div", "", "params");
    endpoint.path_params.forEach(function (name) {
      var mapped = [];
      Object.keys(endpoint.request_map || {}).forEach(function (target) {
        if (endpoint.request_map[target] === "{" + name + "}") mapped.push(target);
      });
      var field = node("label", name + (mapped.length ? " -> " + mapped.join(", ") : ""), "param");
      var input = document.createElement("input");
      input.placeholder = name;
      input.addEventListener("input", function () {
        params[name] = input.value;
        box.dispatchEvent(new CustomEvent("params", { bubbles: true }));
      });
      field.append(input);
      box.append(field);
    });
    return box;
  }

  // ---------------------------------------------------------------- topic reads

  function readPanel(endpoint) {
    var panel = node("div", "", "panel");
    var status = node("div", "not read yet", "sample-status");
    var body = node("pre", "", "sample");
    var received = null;
    var latest = null;

    function paint() {
      var elapsed = received === null ? 0 : performance.now() - received;
      var state = logic.staleness(latest, endpoint.stale_after_ms, elapsed);
      panel.className = "panel sample-" + state.state;
      var parts = [];
      if (latest) {
        parts.push("seq " + latest.seq);
        if (latest.stamp) parts.push(new Date(latest.stamp * 1000).toLocaleTimeString());
        parts.push(state.state);
        if (state.remainingMs != null) parts.push("stale in " + Math.ceil(state.remainingMs) + " ms");
      }
      status.textContent = parts.join(" | ") || "no sample yet";
    }

    function read() {
      return send("GET", endpoint.url).then(function (answer) {
        if (answer.status !== 200) {
          status.textContent = answer.status + " " + logic.explain(answer.outcome);
          panel.className = "panel sample-" + answer.outcome;
          return;
        }
        if (!latest || answer.body.seq !== latest.seq) received = performance.now();
        latest = answer.body;
        body.textContent = JSON.stringify(answer.body.data, null, 2);
        paint();
      });
    }

    var readButton = button("GET", read);
    var poll = document.createElement("label");
    var toggle = document.createElement("input");
    toggle.type = "checkbox";
    poll.append(toggle, document.createTextNode(" auto-poll"));
    toggle.addEventListener("change", function () {
      if (toggle.checked) {
        read();
        polls[endpoint.path] = window.setInterval(read, POLL_MS);
      } else {
        window.clearInterval(polls[endpoint.path]);
        delete polls[endpoint.path];
      }
    });
    // Repaints between polls so the countdown moves and the tile flips on time rather than on the
    // next response.
    polls[endpoint.path + ":paint"] = window.setInterval(paint, 250);

    panel.append(controls([readButton, poll]), status, body);
    return panel;
  }

  function stopPolling() {
    Object.keys(polls).forEach(function (key) {
      window.clearInterval(polls[key]);
      delete polls[key];
    });
  }

  // -------------------------------------------------------- writes and services

  function bodyEditor(endpoint, direction) {
    var key = endpoint.schemas[direction];
    var schema = key ? schemas[key] : null;
    var plan = schema ? logic.formPlan(schema, resolver) : { mode: "raw", reason: "no schema", fields: [] };
    var box = node("div", "", "editor");
    var problems = node("div", "", "problems");
    var values = {};
    var raw = document.createElement("textarea");
    raw.rows = 4;
    raw.value = "{}";
    var mode = plan.mode;

    function fields() {
      plan.fields.forEach(function (field) {
        var label = node("label", field.name, "field");
        var input;
        if (field.widget === "boolean") {
          input = document.createElement("input");
          input.type = "checkbox";
          input.addEventListener("change", function () {
            values[field.name] = input.checked;
          });
        } else if (field.widget === "enum") {
          input = document.createElement("select");
          input.append(new Option("", ""));
          field.options.forEach(function (option) {
            input.append(new Option(String(option), String(option)));
          });
          input.addEventListener("change", function () {
            values[field.name] = input.value;
          });
        } else {
          input = document.createElement("input");
          if (field.widget === "number") {
            input.type = "number";
            if (field.minimum !== undefined) input.min = field.minimum;
            if (field.maximum !== undefined) input.max = field.maximum;
            input.step = "any";
          }
          if (field.maxLength !== undefined) input.maxLength = field.maxLength;
          input.addEventListener("input", function () {
            values[field.name] = input.value;
          });
        }
        label.append(input);
        var hint = [];
        if (field.minimum !== undefined) hint.push(field.minimum + " .. " + field.maximum);
        if (field.nullable) hint.push("null allowed");
        if (hint.length) label.append(node("span", hint.join(", "), "hint"));
        box.append(label);
      });
    }

    if (mode === "form") {
      fields();
    } else {
      box.append(
        node("div", "Raw JSON: " + plan.reason + ". Validated here before it is sent.", "note"),
        raw
      );
    }

    return {
      element: box,
      problems: problems,
      read: function () {
        problems.textContent = "";
        var value = mode === "form" ? logic.assemble(plan.fields, values) : null;
        if (mode !== "form") {
          try {
            value = JSON.parse(raw.value);
          } catch (failure) {
            problems.textContent = "not valid JSON: " + failure.message;
            return null;
          }
        }
        if (schema) {
          var found = logic.validate(value, schema, resolver);
          if (found.length) {
            problems.textContent = found.join("; ");
            return null;
          }
        }
        return value;
      },
    };
  }

  function writePanel(endpoint, params) {
    var panel = node("div", "", "panel");
    var editor = bodyEditor(endpoint, "write");
    var result = resultBox();
    panel.append(
      editor.element,
      editor.problems,
      controls([
        button("Publish", function () {
          var body = editor.read();
          if (body === null) return Promise.resolve();
          return send("POST", logic.fill(endpoint.url, params), { body: body }).then(result.show);
        }),
      ]),
      result.element
    );
    return panel;
  }

  function servicePanel(endpoint, params) {
    var panel = node("div", "", "panel");
    var editor = bodyEditor(endpoint, "write");
    var result = resultBox();
    var gate = (endpoint.preconditions || []).length ? gateBox(endpoint, params) : null;

    function call() {
      var body = editor.read();
      if (body === null) return Promise.resolve();
      return send("POST", logic.fill(endpoint.url, params), { body: body }).then(result.show);
    }

    function hammer() {
      var body = editor.read();
      if (body === null) return Promise.resolve();
      var url = logic.fill(endpoint.url, params);
      var samples = [];
      var chain = Promise.resolve();
      for (var i = 0; i < 20; i += 1) {
        chain = chain.then(function () {
          return send("POST", url, { body: body }).then(function (answer) {
            samples.push(answer.ms);
            return answer;
          });
        });
      }
      return chain.then(function (last) {
        var stats = logic.percentiles(samples);
        result.show(last);
        result.note(
          stats.count + " calls: p50 " + stats.p50.toFixed(1) + " ms, p95 " + stats.p95.toFixed(1) + " ms"
        );
      });
    }

    panel.append(editor.element, editor.problems);
    if (gate) panel.append(gate.element);
    panel.append(
      controls([button("Call", call), button("Hammer x20", hammer, "secondary")]),
      result.element
    );
    return panel;
  }

  // A gate's live state, beside the button it guards, computed the way the gateway computes it.
  function gateBox(endpoint, params) {
    var box = node("div", "", "gate");
    var precondition = endpoint.preconditions[0];
    var state = node("span", "checking " + precondition.state_endpoint, "gate-state");
    var source = (manifest.endpoints.filter(function (other) {
      return other.path === precondition.state_endpoint;
    })[0] || {}).url;

    function refresh() {
      if (!source) {
        state.textContent = precondition.state_endpoint + " is not visible to this token";
        return;
      }
      send("GET", source).then(function (answer) {
        if (answer.status !== 200) {
          state.textContent = precondition.state_endpoint + " answered " + answer.status;
          return;
        }
        var key = precondition.key ? params[precondition.key] : null;
        var verdict = logic.gateVerdict(precondition, answer.body, key);
        box.className = "gate gate-" + verdict.verdict;
        state.textContent =
          precondition.field + " = " + JSON.stringify(logic.extract(answer.body.data, precondition.field)) +
          " -- " + verdict.verdict + " (" + verdict.reason + ")";
      });
    }

    box.append(node("span", "gated:", "gate-label"), state);
    polls[endpoint.path + ":gate"] = window.setInterval(refresh, POLL_MS);
    box.addEventListener("params", refresh);
    refresh();
    return { element: box, refresh: refresh };
  }

  // ------------------------------------------------------------- the auth matrix

  function matrixPanel(endpoint, params) {
    var panel = node("details", "", "matrix");
    panel.append(node("summary", "auth matrix"));
    var table = document.createElement("table");
    var note = node(
      "div",
      "Anything that is not a GET is probed with a body no converter accepts, so an accepted " +
        "token shows 400: authorised, nothing executed.",
      "note"
    );
    var run = button("Run", function () {
      return fire(endpoint, params, table);
    });
    panel.append(note, controls([run]), table);
    return panel;
  }

  function fire(endpoint, params, table) {
    var attempts = [
      { label: "no token", token: null },
      { label: "bad token", token: BAD_TOKEN },
    ].concat(
      wallet.tokens.map(function (entry) {
        return { label: entry.label, token: entry.token };
      })
    );
    var url = logic.fill(endpoint.url, params);
    var probe = endpoint.method === "GET" ? undefined : logic.PROBE;
    table.replaceChildren();
    var head = document.createElement("tr");
    head.append(node("th", "identity"), node("th", "status"), node("th", "meaning"));
    table.append(head);

    var chain = Promise.resolve();
    attempts.forEach(function (attempt) {
      chain = chain.then(function () {
        return send(endpoint.method, url, { token: attempt.token, body: probe }).then(function (answer) {
          var row = document.createElement("tr");
          row.className = "outcome-" + answer.outcome;
          row.append(
            node("td", attempt.label),
            node("td", String(answer.status || "-")),
            node("td", meaning(answer, probe !== undefined))
          );
          table.append(row);
        });
      });
    });
    return chain;
  }

  function meaning(answer, probed) {
    if (answer.status === 401) return "not authenticated";
    if (answer.status === 403) return "authenticated, wrong role";
    if (!probed) return logic.explain(answer.outcome);
    if (answer.status === 400) return "authorised -- probe body refused, nothing executed";
    // A gated endpoint refuses before the body is read, so the probe never gets that far. Still
    // proof the token was authorised, and still nothing executed.
    if (answer.outcome === "gated") return "authorised -- gate refused, nothing executed";
    return logic.explain(answer.outcome);
  }

  // -------------------------------------------------------------------- helpers

  function resultBox() {
    var element = node("div", "", "result");
    var line = node("div", "", "result-line");
    var body = node("pre", "", "result-body");
    var extra = node("div", "", "result-note");
    element.append(line, extra, body);
    return {
      element: element,
      show: function (answer) {
        element.className = "result outcome-" + answer.outcome;
        line.textContent =
          (answer.status || "-") + " -- " + logic.explain(answer.outcome) +
          " -- " + answer.ms.toFixed(1) + " ms";
        body.textContent = answer.body === null ? "" : JSON.stringify(answer.body, null, 2);
        extra.textContent = "";
        return answer;
      },
      note: function (text) {
        extra.textContent = text;
      },
    };
  }

  function controls(children) {
    var box = node("div", "", "controls");
    children.forEach(function (child) {
      box.append(child);
    });
    return box;
  }

  function button(text, action, className) {
    var element = node("button", text, className || "");
    element.type = "button";
    element.addEventListener("click", function () {
      element.disabled = true;
      Promise.resolve(action()).finally(function () {
        element.disabled = false;
      });
    });
    return element;
  }

  function node(tag, text, className) {
    var element = document.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  // ----------------------------------------------------------------- wiring up

  document.getElementById("connect").addEventListener("click", function () {
    var label = elements.label.value.trim() || "token " + (wallet.tokens.length + 1);
    var token = elements.token.value.trim();
    if (!token) {
      elements.error.textContent = "A token is required.";
      return;
    }
    addToken(label, token);
    elements.token.value = "";
    elements.label.value = "";
    connect();
  });
  elements.token.addEventListener("keydown", function (event) {
    if (event.key === "Enter") document.getElementById("connect").click();
  });

  renderWallet();
  if (activeToken()) connect();
  else elements.token.focus();
})(typeof window !== "undefined" ? window : globalThis);
