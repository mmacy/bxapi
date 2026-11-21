// Writes the two files the reference pages are built from:
//
// - generated/openapi.json: bxapi's OpenAPI document, adjusted for Docs MD 0.2.37.
// - generated/curl-samples.json: one curl command for each operation, built from the example
//   exchanges that bxapi's tests replay against a server.

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const repoRoot = path.resolve(import.meta.dirname, "../..");
const generatedRoot = path.resolve(import.meta.dirname, "../generated");

// The address of a server on the reader's machine, as the guides use it.
const SERVER_URL = "http://127.0.0.1:8640";
const HTTP_METHODS = ["get", "put", "post", "delete", "options", "head", "patch", "trace"];
const DATA_KEYS = new Set(["default", "enum", "example", "examples", "const"]);

// Save documents contain 128-bit PCG RNG integers, which plain `JSON.parse` rounds to floats such
// as `1.4762342539966353e+38` that the server then refuses with 422 `content_invalid`. `JSON.rawJSON`
// keeps exact integer tokens intact through `JSON.stringify`.
function parseJson(text) {
  return JSON.parse(text, (_key, value, context) =>
    typeof value === "number" && !Number.isSafeInteger(value) && context?.source && /^-?\d+$/.test(context.source)
      ? JSON.rawJSON(context.source)
      : value,
  );
}

// bxapi builds the OpenAPI document from its code, so the reference describes the checkout it's
// built from.
const document = parseJson(
  execFileSync("uv", ["run", "python", "-m", "bxapi.openapi"], {
    cwd: repoRoot,
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  }),
);

// bxapi's document has no `servers` entry, because each server runs at its own address. Docs MD
// writes a code sample only when the document has one.
document.servers = [{ url: SERVER_URL, description: "A bxapi server on your machine." }];

// Docs MD 0.2.37 drops an operation's entire Parameters section when the operation has more than
// four parameters unless `x-speakeasy-max-method-params` raises that threshold.
document["x-speakeasy-max-method-params"] = 20;

// Docs MD 0.2.37 hides query parameters that use JSON Schema `const` (such as `include=view`) and
// renders `const` object properties without their fixed value. Replacing `const` with a
// single-element `enum` preserves the exact schema meaning while making Docs MD render both.
function constToEnum(value) {
  if (Array.isArray(value)) {
    value.forEach(constToEnum);
    return;
  }
  if (value === null || typeof value !== "object" || JSON.isRawJSON(value)) {
    return;
  }
  if (Object.hasOwn(value, "const")) {
    value.enum = [value.const];
    delete value.const;
  }
  for (const [key, child] of Object.entries(value)) {
    if (!DATA_KEYS.has(key)) {
      constToEnum(child);
    }
  }
}

constToEnum(document.paths);
constToEnum(document.components?.schemas);

function resolvePointer(root, pointer) {
  let target = root;
  for (const token of pointer.slice(2).split("/")) {
    target = target?.[token.replaceAll("~1", "/").replaceAll("~0", "~")];
  }
  return target;
}

// Docs MD 0.2.37 stops with a nil pointer error when the value of an example has a `$ref` key.
// `GET /v1/rules/commands/{command_type}` returns a JSON Schema, so its example has them. This
// function replaces each `$ref` with the schema the `$ref` points to, and the two mean the same
// thing. `schemaRoot` is the nearest enclosing object that has `$defs`, which is where a
// `#/$defs/...` pointer starts.
function inlineRefs(value, schemaRoot, resolving = []) {
  if (Array.isArray(value)) {
    return value.map((item) => inlineRefs(item, schemaRoot, resolving));
  }
  if (value === null || typeof value !== "object" || JSON.isRawJSON(value)) {
    return value;
  }
  if (Object.hasOwn(value, "$defs")) {
    schemaRoot = value;
  }
  const { $ref: pointer, ...rest } = value;
  const inlined = Object.fromEntries(
    Object.entries(rest).map(([key, child]) => [key, inlineRefs(child, schemaRoot, resolving)]),
  );
  if (typeof pointer !== "string") {
    return inlined;
  }
  const target = pointer.startsWith("#/") ? resolvePointer(schemaRoot, pointer) : undefined;
  if (target === undefined || resolving.includes(pointer)) {
    throw new Error(`An example has a $ref that this script can't replace with its schema: ${pointer}`);
  }
  return { ...inlineRefs(target, schemaRoot, [...resolving, pointer]), ...inlined };
}

function inlineRefsInExamples(holder) {
  if (holder?.example !== undefined) {
    holder.example = inlineRefs(holder.example, holder.example);
  }
  for (const example of Object.values(holder?.examples ?? {})) {
    if (example.value !== undefined) {
      example.value = inlineRefs(example.value, example.value);
    }
  }
}

// Docs MD 0.2.37 gives the responses of an operation that have the same schema one set of
// examples, the first response's, and it shows only some of those responses. Every error response
// is a `Problem`, so each error tab showed the first error's example, and some statuses had no
// tab. This function puts each response's schema inside an `allOf` with a title of its own, and
// Docs MD then treats each response as distinct. The title doesn't appear on the page.
function separateSchema(media, operationId, status) {
  if (media.schema?.$ref) {
    media.schema = { allOf: [{ $ref: media.schema.$ref }], title: `${operationId}_${status}` };
  }
}

// Every error response is a problem document, and Docs MD shows a response's whole schema in its
// tab. With the `Problem` schema in each of an operation's error tabs, a page repeats it more than
// a hundred times. This function gives each error response a schema with no members, so the tab
// shows the response's codes and its example. It also adds a link to "Errors" in the API's
// overview, which lists the members once. Docs MD shows a
// response only when it has an object schema, and doesn't show the schema's description.
function summarizeProblem(response, media, operationId, status) {
  if (media.schema?.$ref === "#/components/schemas/Problem") {
    media.schema = { type: "object", title: `${operationId}_${status}` };
    response.description += "\n\nThe body is a problem document. [Errors](/reference#errors) lists its members.";
  }
}

const exchanges = parseJson(readFileSync(path.join(repoRoot, "src/bxapi/openapi_examples.json"), "utf8"));
const validRequestNames = new Set(
  exchanges
    .filter(
      ({ recorded }) =>
        recorded.status < 300 && !(typeof recorded.response === "object" && recorded.response?.valid === false),
    )
    .map(({ name }) => name),
);

// Docs MD renders every entry in `requestBody.content.*.examples` under "Request examples", so keep
// only valid 2xx request bodies with distinct payloads (error bodies belong to error tabs).
function keepValidRequestExamples(holder) {
  if (!holder?.examples) {
    return;
  }
  const seen = new Set();
  const kept = {};
  for (const [name, example] of Object.entries(holder.examples)) {
    const serialized = JSON.stringify(example.value);
    if (validRequestNames.has(name) && !seen.has(serialized)) {
      seen.add(serialized);
      kept[name] = example;
    }
  }
  if (Object.keys(kept).length > 0) {
    holder.examples = kept;
  } else {
    delete holder.examples;
  }
}

// The ids of the operations that need an API key.
const keyed = new Set();
for (const pathItem of Object.values(document.paths)) {
  for (const method of HTTP_METHODS) {
    const operation = pathItem[method];
    if (!operation) {
      continue;
    }
    if (operation.security?.length) {
      keyed.add(operation.operationId);
    }
    operation.parameters?.forEach(inlineRefsInExamples);
    for (const media of Object.values(operation.requestBody?.content ?? {})) {
      keepValidRequestExamples(media);
      inlineRefsInExamples(media);
    }
    for (const [status, response] of Object.entries(operation.responses ?? {})) {
      for (const media of Object.values(response.content ?? {})) {
        inlineRefsInExamples(media);
        summarizeProblem(response, media, operation.operationId, status);
        separateSchema(media, operation.operationId, status);
      }
    }
  }
}

// No operation uses `Problem` now, or `FieldError`, which only `Problem` refers to. Docs MD 0.2.37
// reports "unable to locate reference" for an unused schema, so the script removes each schema
// that the paths don't reach.
function schemaRefs(value, found = new Set()) {
  if (Array.isArray(value)) {
    value.forEach((item) => schemaRefs(item, found));
  } else if (value !== null && typeof value === "object" && !JSON.isRawJSON(value)) {
    for (const [key, child] of Object.entries(value)) {
      if (key === "$ref" && typeof child === "string" && child.startsWith("#/components/schemas/")) {
        found.add(child.slice("#/components/schemas/".length));
      } else {
        schemaRefs(child, found);
      }
    }
  }
  return found;
}

const reached = schemaRefs(document.paths);
for (const name of reached) {
  schemaRefs(document.components.schemas[name], reached);
}
for (const name of Object.keys(document.components.schemas)) {
  if (!reached.has(name)) {
    delete document.components.schemas[name];
  }
}

// Docs MD writes a curl sample from the schema alone: it leaves out every optional field and
// makes up the values, so some of its samples get a 422. An exchange in openapi_examples.json is
// a request that a server answered, so the sample for each operation is the first one that
// succeeded.
function curlCommand({ method, path: requestPath, query, body }, needsKey) {
  const search = query ? `?${new URLSearchParams(query)}` : "";
  const lines = [`curl -s${method === "GET" ? "" : ` -X ${method}`} "${SERVER_URL}${requestPath}${search}"`];
  if (needsKey) {
    lines.push('-H "Authorization: Bearer $KEY"');
  }
  if (body !== undefined) {
    lines.push('-H "Content-Type: application/json"');
    lines.push(`-d '${JSON.stringify(body).replaceAll("'", "'\\''")}'`);
  }
  return lines.join(" \\\n  ");
}

const curlSamples = {};
for (const { operation, recorded } of exchanges) {
  if (!(operation in curlSamples) && recorded.status < 300) {
    curlSamples[operation] = curlCommand(recorded, keyed.has(operation));
  }
}

mkdirSync(generatedRoot, { recursive: true });
writeFileSync(path.join(generatedRoot, "openapi.json"), JSON.stringify(document));
writeFileSync(path.join(generatedRoot, "curl-samples.json"), JSON.stringify(curlSamples, null, 2));
console.log(`Wrote the OpenAPI document and ${Object.keys(curlSamples).length} curl samples`);
