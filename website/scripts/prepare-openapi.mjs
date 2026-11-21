// Writes the two files the reference pages are built from:
//
// - generated/openapi.json: bxapi's OpenAPI document, changed in the three ways Docs MD needs.
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

// bxapi builds the OpenAPI document from its code, so the reference describes the checkout it's
// built from.
const document = JSON.parse(
  execFileSync("uv", ["run", "python", "-m", "bxapi.openapi"], {
    cwd: repoRoot,
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  }),
);

// bxapi's document has no `servers` entry, because each server runs at its own address. Docs MD
// writes a code sample only when the document has one.
document.servers = [{ url: SERVER_URL, description: "A bxapi server on your machine." }];

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
  if (value === null || typeof value !== "object") {
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
    Object.values(operation.requestBody?.content ?? {}).forEach(inlineRefsInExamples);
    for (const [status, response] of Object.entries(operation.responses ?? {})) {
      for (const media of Object.values(response.content ?? {})) {
        inlineRefsInExamples(media);
        separateSchema(media, operation.operationId, status);
      }
    }
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

const exchanges = JSON.parse(readFileSync(path.join(repoRoot, "src/bxapi/openapi_examples.json"), "utf8"));
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
