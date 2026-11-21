// @ts-check

import path from "node:path";

import { themes as prismThemes } from "prism-react-renderer";

import curlSamples from "./src/remark/curl-samples.mjs";
import docsMdFixes from "./src/remark/docs-md-fixes.mjs";
import operationLinks from "./src/remark/operation-links.mjs";
import repoLinks from "./src/remark/repo-links.mjs";
import unescapeCodeBraces from "./src/remark/unescape-code-braces.mjs";

const repoUrl = "https://github.com/mmacy/bxapi";

// Docusaurus loads this file as CommonJS, whatever its syntax, so `import.meta.dirname` isn't
// available here and `__dirname` is.
const siteDir = __dirname;
const contentRoot = path.resolve(siteDir, "generated/content");
const referenceRoot = path.join(contentRoot, "reference");

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: "bxapi",
  tagline: "An HTTP API for the B/X rules",

  url: "https://mmacy.github.io",
  baseUrl: "/bxapi/",

  future: {
    v4: true,
  },

  onBrokenLinks: "throw",
  onBrokenAnchors: "throw",

  markdown: {
    // A .md file is CommonMark, as GitHub reads it, and a .mdx file is MDX. The hand-written
    // pages are .md, and Docs MD generates .mdx.
    format: "detect",
    hooks: {
      onBrokenMarkdownLinks: "throw",
    },
  },

  i18n: {
    defaultLocale: "en",
    locales: ["en"],
  },

  presets: [
    [
      "classic",
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          // `npm run generate` fills this directory: scripts/stage-pages.mjs copies the
          // hand-written pages into it, and Docs MD generates the reference pages in it.
          path: "generated/content",
          routeBasePath: "/",
          sidebarPath: "./sidebars.js",
          beforeDefaultRemarkPlugins: [
            [repoLinks, { contentRoot, repoRoot: path.resolve(siteDir, ".."), blobUrl: `${repoUrl}/blob/main` }],
            [unescapeCodeBraces, { generatedRoot: referenceRoot }],
            [docsMdFixes, { generatedRoot: referenceRoot }],
            [
              operationLinks,
              { generatedRoot: referenceRoot, openapiFile: path.resolve(siteDir, "generated/openapi.json") },
            ],
            [
              curlSamples,
              { generatedRoot: referenceRoot, samplesFile: path.resolve(siteDir, "generated/curl-samples.json") },
            ],
          ],
        },
        blog: false,
        pages: false,
        theme: {
          customCss: "./src/css/custom.css",
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      colorMode: {
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: "bxapi",
        items: [
          { type: "docSidebar", sidebarId: "guides", position: "left", label: "Guides" },
          { type: "docSidebar", sidebarId: "reference", position: "left", label: "API reference" },
          { href: repoUrl, label: "GitHub", position: "right" },
        ],
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
        additionalLanguages: ["bash", "json"],
      },
    }),
};

export default config;
