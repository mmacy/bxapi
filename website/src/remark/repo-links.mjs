// A remark plugin that points a link at GitHub when its target is a repository file that isn't a
// page on the site, like AGENTS.md or examples/play_barrow.py. Without it, Docusaurus reports the
// link as broken. A link to a staged page is left for Docusaurus to resolve.

import { existsSync } from "node:fs";
import path from "node:path";

const HAS_SCHEME_OR_ROOT = /^([a-z][a-z0-9+.-]*:|\/|#)/i;

function eachLink(node, visit) {
  if (node.type === "link" || node.type === "definition") {
    visit(node);
  }
  for (const child of node.children ?? []) {
    eachLink(child, visit);
  }
}

/**
 * @param {object} options
 * @param {string} options.contentRoot The directory that contains the staged pages.
 * @param {string} options.repoRoot The repository's root.
 * @param {string} options.blobUrl The URL of the repository's root on GitHub, at a branch.
 * @param {string} [options.siteUrl] The published site root, stripped from Markdown links so Docusaurus checks them.
 */
export default function repoLinks({ contentRoot, repoRoot, blobUrl, siteUrl }) {
  return (tree, file) => {
    const pageDir = path.relative(contentRoot, path.dirname(file.path));
    if (pageDir.startsWith("..")) {
      return;
    }
    eachLink(tree, (link) => {
      const isAutolink =
        link.children?.length === 1 && link.children[0].type === "text" && link.children[0].value === link.url;
      if (siteUrl && !isAutolink && link.url?.startsWith(`${siteUrl}/`)) {
        link.url = link.url.slice(siteUrl.length);
        return;
      }
      if (!link.url || HAS_SCHEME_OR_ROOT.test(link.url)) {
        return;
      }
      const [target, fragment] = link.url.split("#", 2);
      const repoPath = path.normalize(path.join(pageDir, decodeURI(target)));
      if (repoPath.startsWith("..") || existsSync(path.join(contentRoot, repoPath))) {
        return;
      }
      if (existsSync(path.join(repoRoot, repoPath))) {
        link.url = `${blobUrl}/${repoPath}${fragment ? `#${fragment}` : ""}`;
      }
    });
  };
}
