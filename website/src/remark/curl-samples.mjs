// A remark plugin that replaces each curl sample Docs MD writes with the tested one that
// scripts/prepare-openapi.mjs built for the same operation.

import { readFileSync } from "node:fs";
import path from "node:path";

// The id Docs MD gives the element that contains an operation's curl sample.
const SAMPLE_ID = /^operation-(.+)\+code-samples\+curl$/;

function findCode(node) {
  if (node.type === "code") {
    return node;
  }
  for (const child of node.children ?? []) {
    const code = findCode(child);
    if (code) {
      return code;
    }
  }
  return undefined;
}

function eachSample(node, visit) {
  if (node.type === "mdxJsxFlowElement" && node.name === "SectionContent") {
    const id = node.attributes.find((attribute) => attribute.name === "id")?.value;
    const operation = typeof id === "string" ? SAMPLE_ID.exec(id)?.[1] : undefined;
    if (operation) {
      visit(operation, findCode(node));
      return;
    }
  }
  for (const child of node.children ?? []) {
    eachSample(child, visit);
  }
}

/**
 * @param {object} options
 * @param {string} options.generatedRoot The directory that contains the pages Docs MD generates.
 * @param {string} options.samplesFile The JSON file that maps an operation's id to its curl command.
 */
export default function curlSamples({ generatedRoot, samplesFile }) {
  return (tree, file) => {
    if (path.relative(generatedRoot, file.path).startsWith("..")) {
      return;
    }
    const samples = JSON.parse(readFileSync(samplesFile, "utf8"));
    eachSample(tree, (operation, code) => {
      if (!code || !(operation in samples)) {
        throw new Error(`${file.path}: no tested curl sample replaces the generated one for ${operation}`);
      }
      code.lang = "bash";
      code.value = samples[operation];
    });
  };
}
