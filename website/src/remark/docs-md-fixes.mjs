// A remark plugin that corrects three things Docs MD 0.2.37 writes on each reference page:
//
// - Docs MD titles an operation's responses "Server-Side Events" when the operation has no 200
//   response, as for an operation that returns 201 or 204. bxapi has no event streams, so this
//   plugin titles every responses section "Responses".
// - Docs MD titles a tag's page "<Tag> Endpoints", like "Sessions Endpoints". This plugin removes
//   " Endpoints", so the title matches the page's name in the sidebar.
// - Docs MD adds a required `HttpMeta` member to the body of every error response. It's a member
//   of the error classes that Speakeasy generates for an SDK, and the server never sends it, so
//   this plugin removes it.

import path from "node:path";

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

/**
 * @param {object} options
 * @param {string} options.generatedRoot The directory that contains the pages Docs MD generates.
 */
export default function docsMdFixes({ generatedRoot }) {
  return (tree, file) => {
    if (path.relative(generatedRoot, file.path).startsWith("..")) {
      return;
    }
    eachHeading(tree, (heading) => {
      if (heading.depth === 1) {
        replaceText(heading, / Endpoints$/, "");
      } else if (heading.depth === 3) {
        replaceText(heading, /^Server-Side Events(?= |$)/, "Responses");
      }
    });
    removeHttpMeta(tree);
  };
}
