// Runs the console's pure logic against fixtures the Python side generates, and prints one JSON
// object of results for it to assert on.
//
// This exists because the interesting parts of the console are decisions -- which schema gets a
// form, what a response means, when a tile goes stale -- and those should be tested against the
// schemas the gateway actually emits rather than against hand-written imitations of them. There is
// no browser here, so console.js keeps its logic behind a DOM-free seam and this drives that.
//
// Usage: node test/console_logic.mjs <fixtures.json>

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const fixtures = JSON.parse(readFileSync(process.argv[2], "utf8"));

// console.js is a plain browser script, so give it a global to hang itself off and no `document`.
globalThis.window = undefined;
createRequire(import.meta.url)(fixtures.script);
const logic = globalThis.gatewayConsole;

const schemas = fixtures.schemas;
const resolve = (ref) => schemas[ref.replace("#/components/schemas/", "")];

const results = { plans: {}, validations: [], classifications: [], staleness: [], gates: [] };

for (const [name, key] of Object.entries(fixtures.plan_for)) {
  const plan = logic.formPlan(schemas[key], resolve);
  results.plans[name] = {
    mode: plan.mode,
    reason: plan.reason,
    fields: plan.fields.map((field) => ({
      name: field.name,
      widget: field.widget,
      nullable: field.nullable,
      minimum: field.minimum === undefined ? null : field.minimum,
      maximum: field.maximum === undefined ? null : field.maximum,
    })),
  };
}

for (const check of fixtures.validate) {
  results.validations.push({
    label: check.label,
    problems: logic.validate(check.value, schemas[check.schema], resolve),
  });
}

for (const check of fixtures.classify) {
  results.classifications.push({
    label: check.label,
    outcome: logic.classify(check.status, check.body),
    explanation: logic.explain(logic.classify(check.status, check.body)),
  });
}

for (const check of fixtures.staleness) {
  results.staleness.push({
    label: check.label,
    ...logic.staleness(check.sample, check.stale_after_ms, check.elapsed_ms),
  });
}

for (const check of fixtures.gates) {
  results.gates.push({
    label: check.label,
    ...logic.gateVerdict(check.precondition, check.sample, check.param),
  });
}

// The round trip a form actually performs: field values in, request body out.
results.assembled = fixtures.assemble.map((check) => ({
  label: check.label,
  body: logic.assemble(
    logic.formPlan(schemas[check.schema], resolve).fields,
    check.values
  ),
}));

results.filled = fixtures.fill.map((check) => logic.fill(check.url, check.params));
results.percentiles = logic.percentiles(fixtures.percentiles);
results.probe = logic.PROBE;

process.stdout.write(JSON.stringify(results));
