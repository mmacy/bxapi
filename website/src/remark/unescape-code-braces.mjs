// A remark plugin that removes the backslash Docs MD 0.2.37 puts before each brace in a
// description. MDX needs the backslash in text, where a brace starts an expression. In code, the
// backslash is literal, so without this plugin a reader sees `GET /v1/sessions/\{session_id\}`.

import path from "node:path";

// A backslash before a brace, unless another backslash escapes that backslash.
const ESCAPED_BRACE = /(?<!\\)\\([{}])/g;

function eachCode(node, visit) {
  if (node.type === "inlineCode" || node.type === "code") {
    visit(node);
  }
  for (const child of node.children ?? []) {
    eachCode(child, visit);
  }
}

/**
 * @param {object} options
 * @param {string} options.generatedRoot The directory that contains the pages Docs MD generates.
 */
export default function unescapeCodeBraces({ generatedRoot }) {
  return (tree, file) => {
    if (path.relative(generatedRoot, file.path).startsWith("..")) {
      return;
    }
    eachCode(tree, (code) => {
      code.value = code.value.replace(ESCAPED_BRACE, "$1");
    });
  };
}
