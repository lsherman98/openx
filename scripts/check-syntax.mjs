import { readdirSync } from "node:fs";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

function check(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (entry.name === "node_modules") continue;
    const path = join(directory, entry.name);
    if (entry.isDirectory()) check(path);
    else if (entry.isFile() && entry.name.endsWith(".mjs")) {
      execFileSync(process.execPath, ["--check", path], { stdio: "inherit" });
    }
  }
}

check(fileURLToPath(new URL(".", import.meta.url)));
