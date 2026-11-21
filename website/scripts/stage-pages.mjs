// Copies the hand-written pages into generated/content, where Docusaurus reads them.
//
// The pages stay where GitHub renders them: README.md, docs/, and examples/README.md. A copy has
// the same path under generated/content as its page has under the repository's root, so a
// relative link between two pages works on the site as it does on GitHub. docs/spec.md is the
// API's contract for contributors, and this script doesn't copy it.

import { cpSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import path from "node:path";

const repoRoot = path.resolve(import.meta.dirname, "../..");
const contentRoot = path.resolve(import.meta.dirname, "../generated/content");

const guides = readdirSync(path.join(repoRoot, "docs"))
  .filter((name) => name.endsWith(".md") && name !== "spec.md")
  .map((name) => path.join("docs", name));
const pages = ["README.md", ...guides, path.join("examples", "README.md")];

// Docs MD generates generated/content/reference, and cleans it itself.
for (const staged of ["README.md", "docs", "examples"]) {
  rmSync(path.join(contentRoot, staged), { recursive: true, force: true });
}
for (const page of pages) {
  const target = path.join(contentRoot, page);
  mkdirSync(path.dirname(target), { recursive: true });
  cpSync(path.join(repoRoot, page), target);
}
console.log(`Staged ${pages.length} pages in ${path.relative(process.cwd(), contentRoot)}`);
