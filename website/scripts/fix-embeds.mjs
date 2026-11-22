// Cleans the MDX schema-embed files that Docs MD writes to `generated/embeds/contents/`.
//
// Those files sit outside Docusaurus's `docs.path`, so `docs.beforeDefaultRemarkPlugins` doesn't
// run on them when `.tsx` embed loaders import them. This script removes the backslashes Docs MD
// puts before braces inside code spans and links any `METHOD /path` mentions to their operations.

import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const embedsDir = path.resolve(import.meta.dirname, "../generated/embeds/contents");
const openapiFile = path.resolve(import.meta.dirname, "../generated/openapi.json");

const ESCAPED_BRACE = /(?<!\\)\\([{}])/g;
const INLINE_CODE = /`([^`\n]+)`/g;
const FENCED_CODE = /^```[^\n]*\n[\s\S]*?^```$/gm;
const MENTION = /^(GET|PUT|POST|DELETE|PATCH) (\/[^\s?]*)(\?\S*)?$/;
const HTTP_METHODS = ["get", "put", "post", "delete", "patch"];

function escapeRegExp(text) {
  return text.replace(/[.*+?^$()|[\]\\]/g, "\\$&");
}

function operationTargets(document) {
  const targets = [];
  for (const [operationPath, pathItem] of Object.entries(document.paths ?? {})) {
    for (const method of HTTP_METHODS) {
      const operation = pathItem[method];
      if (operation) {
        const segments = operationPath.split(/\{[^}]+\}/).map(escapeRegExp);
        targets.push({
          exact: `${method.toUpperCase()} ${operationPath}`,
          pattern: new RegExp(`^${method.toUpperCase()} ${segments.join("[^/]+")}$`),
          url: `/reference/endpoint/${operation.tags[0]}#operation-${operation.operationId}`,
        });
      }
    }
  }
  return targets;
}

if (existsSync(embedsDir)) {
  const targets = operationTargets(JSON.parse(readFileSync(openapiFile, "utf8")));
  const files = readdirSync(embedsDir).filter((name) => name.endsWith(".mdx"));
  for (const name of files) {
    const filePath = path.join(embedsDir, name);
    let content = readFileSync(filePath, "utf8");
    content = content.replace(FENCED_CODE, (block) => block.replace(ESCAPED_BRACE, "$1"));
    content = content.replace(INLINE_CODE, (_whole, code, offset, full) => {
      const unescaped = code.replace(ESCAPED_BRACE, "$1");
      const match = MENTION.exec(unescaped);
      if (!match || full[offset - 1] === "[") {
        return `\`${unescaped}\``;
      }
      const key = `${match[1]} ${match[2]}`;
      const target = targets.find((item) => item.exact === key) ?? targets.find((item) => item.pattern.test(key));
      if (!target) {
        throw new Error(`${filePath}: \`${unescaped}\` names an operation the OpenAPI document doesn't have`);
      }
      return `[\`${unescaped}\`](${target.url})`;
    });
    writeFileSync(filePath, content);
  }
  console.log(`Cleaned ${files.length} embed MDX files`);
}
