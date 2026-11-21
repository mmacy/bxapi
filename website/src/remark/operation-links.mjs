// A remark plugin that turns each mention of an operation in the reference, like
// `POST /v1/sessions`, into a link to that operation's section. The reference text mentions
// operations this way because the same text also appears in the interactive documentation at
// `/docs` and in generated clients, where a link to this site would be out of place.

import { readFileSync } from "node:fs";
import path from "node:path";

// A method and a path, with an optional query string, like `GET /v1/saves?session_id=`.
const MENTION = /^(GET|PUT|POST|DELETE|PATCH) (\/[^\s?]*)(\?\S*)?$/;
const HTTP_METHODS = ["get", "put", "post", "delete", "patch"];
// The id Docs MD gives an operation's heading.
const OPERATION_HEADING = /^operation-([^+]+)$/;

function escapeRegExp(text) {
  return text.replace(/[.*+?^$()|[\]\\]/g, "\\$&");
}

// Lists each operation with a pattern that matches its method and path, where a path parameter
// like `{name}` matches any value, so `GET /v1/rules/tables/treasure` names `get_table`.
function operationTargets(openapiFile) {
  const document = JSON.parse(readFileSync(openapiFile, "utf8"));
  const targets = [];
  for (const [operationPath, pathItem] of Object.entries(document.paths)) {
    for (const method of HTTP_METHODS) {
      const operation = pathItem[method];
      if (operation) {
        const segments = operationPath.split(/\{[^}]+\}/).map(escapeRegExp);
        targets.push({
          exact: `${method.toUpperCase()} ${operationPath}`,
          pattern: new RegExp(`^${method.toUpperCase()} ${segments.join("[^/]+")}$`),
          operationId: operation.operationId,
          url: `/reference/endpoint/${operation.tags[0]}#operation-${operation.operationId}`,
        });
      }
    }
  }
  return targets;
}

// The operation a mention names: the one whose path is the mention's, or else the one whose path
// template matches it.
function targetOf(targets, mention) {
  return targets.find((target) => target.exact === mention) ?? targets.find((target) => target.pattern.test(mention));
}

// The id of the operation whose section an `Operation` element holds, from its heading's id.
function operationIdOf(node) {
  let found;
  const search = (child) => {
    if (found) {
      return;
    }
    if (child.type === "heading" && child.depth === 2) {
      for (const part of child.children ?? []) {
        const match = part.type === "text" ? /\{#([^}]+)\}/.exec(part.value) : undefined;
        found ??= match && OPERATION_HEADING.exec(match[1])?.[1];
      }
      found ??= OPERATION_HEADING.exec(child.data?.hProperties?.id ?? child.data?.id ?? "")?.[1];
    }
    (child.children ?? []).forEach(search);
  };
  search(node);
  return found;
}

function linkMentions(node, targets, currentOperation, file) {
  if (node.type === "heading" || node.type === "link") {
    return;
  }
  if (node.type === "mdxJsxFlowElement" && node.name === "Operation") {
    currentOperation = operationIdOf(node);
  }
  node.children?.forEach((child, index) => {
    if (child.type === "inlineCode") {
      const match = MENTION.exec(child.value);
      if (!match) {
        return;
      }
      const target = targetOf(targets, `${match[1]} ${match[2]}`);
      if (!target) {
        throw new Error(`${file.path}: \`${child.value}\` names an operation the OpenAPI document doesn't have`);
      }
      if (target.operationId !== currentOperation) {
        node.children[index] = { type: "link", url: target.url, children: [child] };
      }
    } else {
      linkMentions(child, targets, currentOperation, file);
    }
  });
}

/**
 * @param {object} options
 * @param {string} options.generatedRoot The directory that contains the pages Docs MD generates.
 * @param {string} options.openapiFile The OpenAPI document the pages are generated from.
 */
export default function operationLinks({ generatedRoot, openapiFile }) {
  return (tree, file) => {
    if (path.relative(generatedRoot, file.path).startsWith("..")) {
      return;
    }
    linkMentions(tree, operationTargets(openapiFile), undefined, file);
  };
}
