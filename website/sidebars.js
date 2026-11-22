// @ts-check

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  // The hand-written pages, in the order the README lists them.
  guides: [
    { type: "doc", id: "README", label: "Get started" },
    "docs/play-a-game",
    "docs/client-guide",
    "docs/configuration",
    { type: "doc", id: "examples/README", label: "Example client" },
  ],
  // The pages Docs MD generates from the OpenAPI document, in the workflow order `_TAGS` defines.
  reference: [
    "reference/index",
    "reference/endpoint/meta",
    "reference/endpoint/rules",
    "reference/endpoint/dice",
    "reference/endpoint/generate",
    "reference/endpoint/characters",
    "reference/endpoint/adventures",
    "reference/endpoint/sessions",
    "reference/endpoint/saves",
  ],
};

export default sidebars;
