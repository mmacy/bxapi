// A remark plugin that corrects defects in the MDX pages Docs MD 0.2.37 writes:
//
// - Titles an operation's responses "Responses" instead of "Server-Side Events", and converts
//   Docs MD's section headings ("Responses Examples", "Request Examples", "Request Body") to
//   sentence case.
// - Removes " Endpoints" from each tag page's title so it matches the sidebar label.
// - Removes the SDK-only `HttpMeta` property that Docs MD adds to every error response.
// - Removes the redundant `Servers` and `Global Security` tail that Docs MD appends to the About
//   page, which already covers both under "Base URL" and "Authentication".
// - Restores each request and response example's `summary` and `description` from the OpenAPI
//   document above its JSON block, which Docs MD otherwise discards.

import { readFileSync } from "node:fs";
import path from "node:path";

const HTTP_METHODS = ["get", "put", "post", "delete", "patch"];
const OPERATION_HEADING = /^operation-([^+]+)$/;
const RESPONSE_EXAMPLE_ID = /^operation-([^+]+)\+responses-examples\+(\d{3})\+/;

function eachHeading(node, visit) {
  if (node.type === "heading") {
    visit(node);
    return;
  }
  for (const child of node.children ?? []) {
    eachHeading(child, visit);
  }
}

function replaceText(heading, pattern, replacement) {
  for (const child of heading.children) {
    if (child.type === "text") {
      child.value = child.value.replace(pattern, replacement);
    }
  }
}

function idOf(node) {
  return node.attributes?.find((attribute) => attribute.name === "id")?.value;
}

function isElement(node, name) {
  return node.type === "mdxJsxFlowElement" && node.name === name;
}

function plainText(node) {
  if (node.type === "text") {
    return node.value;
  }
  return (node.children ?? []).map(plainText).join("");
}

// Removes each `HttpMeta` property, and each section that has nothing else in it.
function removeHttpMeta(node) {
  if (!node.children) {
    return;
  }
  node.children = node.children.filter((child) => {
    if (isElement(child, "ExpandableProperty") && idOf(child) === "HttpMeta") {
      return false;
    }
    removeHttpMeta(child);
    return !(isElement(child, "ExpandableSection") && child.children.length === 0);
  });
}

function removeAboutTail(tree) {
  const index = tree.children.findIndex(
    (child, position) =>
      child.type === "paragraph" &&
      plainText(child).trim() === "Servers" &&
      tree.children[position + 1]?.type === "list",
  );
  if (index !== -1) {
    tree.children.splice(index);
  }
}

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

function parseJson(text) {
  return JSON.parse(text, (_key, value, context) =>
    typeof value === "number" && !Number.isSafeInteger(value) && context?.source && /^-?\d+$/.test(context.source)
      ? JSON.rawJSON(context.source)
      : value,
  );
}

function exampleEntry(entry) {
  const exact = JSON.stringify(entry.value, null, 2);
  return {
    summary: entry.summary ?? "",
    description: entry.description ?? "",
    serialized: JSON.stringify(JSON.parse(exact)),
    formatted: exact,
  };
}

function loadOperationExamples(openapiFile) {
  const document = parseJson(readFileSync(openapiFile, "utf8"));
  const byOperation = new Map();
  for (const pathItem of Object.values(document.paths ?? {})) {
    for (const method of HTTP_METHODS) {
      const operation = pathItem[method];
      if (!operation?.operationId) {
        continue;
      }
      const requestMedia = Object.values(operation.requestBody?.content ?? {})[0];
      const request = Object.values(requestMedia?.examples ?? {}).map(exampleEntry);
      const responses = new Map();
      for (const [status, response] of Object.entries(operation.responses ?? {})) {
        const media = Object.values(response.content ?? {})[0];
        if (media?.examples) {
          responses.set(status, Object.values(media.examples).map(exampleEntry));
        }
      }
      byOperation.set(operation.operationId, { request, responses });
    }
  }
  return byOperation;
}

function parseInlineCode(text) {
  const nodes = [];
  for (const part of text.split(/(`[^`]+`)/g)) {
    if (!part) {
      continue;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      nodes.push({ type: "inlineCode", value: part.slice(1, -1) });
    } else {
      nodes.push({ type: "text", value: part });
    }
  }
  return nodes;
}

function captionParagraph(summary, description) {
  const children = [];
  if (summary) {
    const ended = /[.!?]$/.test(summary) ? summary : `${summary}.`;
    children.push({ type: "strong", children: parseInlineCode(ended) });
  }
  if (description) {
    if (children.length > 0) {
      children.push({ type: "text", value: " " });
    }
    const ended = /[.!?]$/.test(description) ? description : `${description}.`;
    children.push(...parseInlineCode(ended));
  }
  return children.length > 0 ? { type: "paragraph", children } : undefined;
}

function captionCodeBlocks(sectionContent, entries) {
  if (!entries?.length || !sectionContent.children) {
    return;
  }
  const unused = [...entries];
  const nextChildren = [];
  for (const child of sectionContent.children) {
    if (child.type === "code") {
      let serialized;
      try {
        serialized = JSON.stringify(JSON.parse(child.value));
      } catch {
        serialized = undefined;
      }
      let matchIndex = serialized === undefined ? -1 : unused.findIndex((entry) => entry.serialized === serialized);
      if (matchIndex === -1 && unused.length > 0) {
        matchIndex = 0;
      }
      if (matchIndex !== -1) {
        const [matched] = unused.splice(matchIndex, 1);
        child.value = matched.formatted;
        const paragraph = captionParagraph(matched.summary, matched.description);
        if (paragraph) {
          nextChildren.push(paragraph);
        }
      }
    }
    nextChildren.push(child);
  }
  sectionContent.children = nextChildren;
}

function annotateExamples(node, byOperation, currentOperation) {
  if (isElement(node, "Operation")) {
    currentOperation = operationIdOf(node);
  }
  if (currentOperation && isElement(node, "OperationRequestBodyExamplesSection")) {
    const entries = byOperation.get(currentOperation)?.request;
    const walk = (child) => {
      if (isElement(child, "SectionContent")) {
        captionCodeBlocks(child, entries);
        return;
      }
      (child.children ?? []).forEach(walk);
    };
    walk(node);
    return;
  }
  if (currentOperation && isElement(node, "OperationResponseBodyExamplesSection")) {
    const responses = byOperation.get(currentOperation)?.responses;
    const walk = (child) => {
      if (isElement(child, "SectionContent")) {
        const match = RESPONSE_EXAMPLE_ID.exec(idOf(child) ?? "");
        if (match) {
          captionCodeBlocks(child, responses?.get(match[2]));
        }
        return;
      }
      (child.children ?? []).forEach(walk);
    };
    walk(node);
    return;
  }
  for (const child of node.children ?? []) {
    annotateExamples(child, byOperation, currentOperation);
  }
}

/**
 * @param {object} options
 * @param {string} options.generatedRoot The directory that contains the pages Docs MD generates.
 * @param {string} options.openapiFile The OpenAPI document the pages are generated from.
 */
export default function docsMdFixes({ generatedRoot, openapiFile }) {
  return (tree, file) => {
    if (path.relative(generatedRoot, file.path).startsWith("..")) {
      return;
    }
    eachHeading(tree, (heading) => {
      if (heading.depth === 1) {
        replaceText(heading, / Endpoints$/, "");
      } else if (heading.depth === 3) {
        replaceText(heading, /^Server-Side Events(?= |$)/, "Responses");
        replaceText(heading, /^Responses Examples(?= |$)/, "Response examples");
        replaceText(heading, /^Request Examples(?= |$)/, "Request examples");
        replaceText(heading, /^Request Example(?= |$)/, "Request example");
        replaceText(heading, /^Request Body(?= |$)/, "Request body");
      }
    });
    removeHttpMeta(tree);
    if (path.basename(file.path) === "index.mdx") {
      removeAboutTail(tree);
    } else if (openapiFile) {
      annotateExamples(tree, loadOperationExamples(openapiFile), undefined);
    }
  };
}
