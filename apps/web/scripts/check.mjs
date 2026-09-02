/*
 * There's no bundler here on purpose - the API serves public/ as-is - so
 * "build" is really "prove these files parse". That's the check that used
 * to only happen by hand, and the one that actually catches a broken UI.
 */
import { execFileSync } from "node:child_process";
import { readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

const PUBLIC_DIR = new URL("../public/", import.meta.url).pathname;
const REQUIRED = ["index.html", "app.js", "graph.js", "voice.js", "markdown.js",
                  "pcm-worklet.js", "styles.css"];

let failed = false;

for (const name of REQUIRED) {
  if (!existsSync(join(PUBLIC_DIR, name))) {
    console.error(`missing: ${name}`);
    failed = true;
  }
}

for (const name of readdirSync(PUBLIC_DIR).filter((f) => f.endsWith(".js"))) {
  try {
    execFileSync(process.execPath, ["--check", join(PUBLIC_DIR, name)], { stdio: "pipe" });
    console.log(`ok  ${name}`);
  } catch (e) {
    console.error(`FAIL ${name}\n${e.stderr?.toString() ?? e.message}`);
    failed = true;
  }
}

if (process.argv.includes("--watch-note")) {
  console.log("web is static - the API at http://127.0.0.1:8080 serves these files.");
}
process.exit(failed ? 1 : 0);
