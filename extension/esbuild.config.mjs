// esbuild build config for the UnifiedCollector Bridge Chrome MV3 extension.
//
// Step 1 of the bundler rollout (docs/plans/extension-bundler.md): the entry
// list is intentionally empty here. Step 2 wires the five entry points
// (content, background, inject, popup, tabs) that MV3 references from
// manifest.json.
//
// Design notes:
//   - format: 'iife' per entry. MV3 CSP forbids eval / new Function; iife
//     inlines everything at bundle time and needs no runtime module loader.
//   - bundle: true. content.js / background.js today are single-file, but the
//     later refactor sprint extracts shared modules under src/shared/ that
//     must be inlined.
//   - minify: false for this first sprint so diffs against the pre-bundle
//     files remain human-readable.
//   - sourcemap: 'linked' for content + background only (largest bundles);
//     omitted for the smaller three to keep dist/ tight.
//   - dist/ is committed (see extension/.gitignore) so Chrome Web Store
//     submissions are reproducible and GitHub reviewers see what actually
//     ships.

import { build, context } from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * @typedef {Object} EntryConfig
 * @property {string} name      Output basename (without .js).
 * @property {string} entry     Absolute path to the entry source file.
 * @property {"linked" | false} sourcemap  esbuild sourcemap value for this entry.
 */

/** @type {EntryConfig[]} */
const entries = [
  { name: "content", entry: resolve(__dirname, "src/content.js"), sourcemap: "linked" },
  { name: "background", entry: resolve(__dirname, "background.js"), sourcemap: "linked" },
  { name: "inject", entry: resolve(__dirname, "inject.js"), sourcemap: false },
  { name: "popup", entry: resolve(__dirname, "popup.js"), sourcemap: false },
  { name: "tabs", entry: resolve(__dirname, "tabs.js"), sourcemap: false },
];

const watch = process.argv.includes("--watch");

const commonOptions = {
  bundle: true,
  format: "iife",
  target: "es2022",
  platform: "browser",
  minify: false,
  logLevel: "info",
  legalComments: "none",
  outdir: resolve(__dirname, "dist"),
};

async function buildAll() {
  if (entries.length === 0) {
    console.log(
      "[esbuild] entry list is empty — nothing to build (step 1 scaffold).",
    );
    return;
  }
  if (watch) {
    for (const entry of entries) {
      const ctx = await context({
        ...commonOptions,
        entryPoints: { [entry.name]: entry.entry },
        sourcemap: entry.sourcemap,
      });
      await ctx.watch();
    }
    console.log("[esbuild] watching for changes…");
    return;
  }
  await Promise.all(
    entries.map((entry) =>
      build({
        ...commonOptions,
        entryPoints: { [entry.name]: entry.entry },
        sourcemap: entry.sourcemap,
      }),
    ),
  );
  console.log(`[esbuild] built ${entries.length} entr${entries.length === 1 ? "y" : "ies"} → dist/`);
}

buildAll().catch((err) => {
  console.error(err);
  process.exit(1);
});
