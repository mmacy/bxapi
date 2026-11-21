// The configuration of Docs MD, Speakeasy's generator of API reference pages. `npm run generate`
// runs `docs-md`, which reads this file and writes one MDX page for each tag of the OpenAPI
// document.

export default {
  // scripts/prepare-openapi.mjs writes this file.
  spec: "./generated/openapi.json",
  output: {
    pageOutDir: "./generated/content/reference",
    // Docs MD puts a schema nested deeper than `display.maxNestingLevel` in a file here, and the
    // page loads the file when the reader opens the schema.
    embedOutDir: "./generated/embeds",
    framework: "docusaurus",
    // Show only the tested examples from `openapi.json`, never synthetic `{}` or schema placeholders.
    generateRequestBodyExamples: false,
    generateResponseExamples: false,
  },
  display: {
    maxNestingLevel: 2,
  },
  // src/remark/curl-samples.mjs replaces each sample Docs MD writes with a tested one.
  codeSamples: [{ language: "curl" }],
};
