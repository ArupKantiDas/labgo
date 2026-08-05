// Copies the (gitignored, regenerable) code graph and co-change data into
// public/ so Vite's dev server can serve them as static assets. Only used
// by `npm run dev` — the production build (`npm run build`) does NOT run
// this, because `dist/` ships in the repo and must not bake in whichever
// corpus the maintainer happened to have ingested. `labgo view` serves
// /graph.json and /cochange.json from the user's own data/ directory at
// request time instead.
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const destDir = resolve(here, "../public");
mkdirSync(destDir, { recursive: true });

const graphSrc = resolve(here, "../../data/graph.json");
if (!existsSync(graphSrc)) {
  console.error(
    `[sync-data] ${graphSrc} not found. Run \`labgo ingest <repo>\` first (see repo README).`,
  );
  process.exit(1);
}
copyFileSync(graphSrc, resolve(destDir, "graph.json"));
console.log(`[sync-data] copied ${graphSrc} -> public/graph.json`);

const cochangeSrc = resolve(here, "../../data/cochange.json");
if (existsSync(cochangeSrc)) {
  copyFileSync(cochangeSrc, resolve(destDir, "cochange.json"));
  console.log(`[sync-data] copied ${cochangeSrc} -> public/cochange.json`);
} else {
  console.log(`[sync-data] ${cochangeSrc} not found — skipping (run \`labgo history <repo>\` for co-change data)`);
}
