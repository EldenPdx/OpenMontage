#!/usr/bin/env node
// Build official release source with its frozen dependency lock and model data.
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const lock = JSON.parse(readFileSync(join(root, "pi-runtime/source.lock.json"), "utf8"));
const nodeVersion = process.versions.node.split(".").map(Number);
const required = lock.node_minimum.split(".").map(Number);
if (nodeVersion[0] < required[0] || (nodeVersion[0] === required[0] && nodeVersion[1] < required[1])) {
  throw new Error(`Pi requires Node >=${lock.node_minimum}; found ${process.versions.node}`);
}
const runtime = join(root, ".runtime/pi");
const source = join(runtime, "source");
const candidate = join(runtime, "building");
const archive = join(runtime, `pi-${lock.version}-source.tar.gz`);
const sha256 = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");
const run = (command, args, cwd = runtime) => execFileSync(command, args, { cwd, stdio: "inherit" });
mkdirSync(runtime, { recursive: true });
if (!existsSync(archive) || sha256(archive) !== lock.source_sha256) {
  const temporary = `${archive}.download`;
  try {
    run("curl", ["--fail", "--location", "--retry", "2", "--output", temporary, lock.source_url]);
    if (sha256(temporary) !== lock.source_sha256) throw new Error("Official Pi source checksum mismatch");
    renameSync(temporary, archive);
  } finally {
    rmSync(temporary, { force: true });
  }
}
// Always rebuild clean release source; a modified previous checkout is not trusted.
rmSync(candidate, { recursive: true, force: true });
mkdirSync(candidate);
run("tar", ["-xzf", archive, "-C", candidate, "--strip-components=1"]);
if (sha256(join(candidate, "package-lock.json")) !== lock.package_lock_sha256) {
  throw new Error("Pi dependency lock differs from the official pinned release");
}
const packageInfo = JSON.parse(readFileSync(join(candidate, "packages/coding-agent/package.json"), "utf8"));
if (packageInfo.name !== lock.package || packageInfo.version !== lock.version) {
  throw new Error("Pi source package identity mismatch");
}
run("npm", ["ci", "--ignore-scripts", "--no-audit", "--no-fund"], candidate);
run("npm", ["run", "build:offline"], candidate);
const version = execFileSync("node", [join(candidate, lock.cli), "--offline", "--version"], {
  env: { PATH: process.env.PATH, HOME: join(runtime, "build-home"), PI_OFFLINE: "1" }, encoding: "utf8",
}).trim();
if (version !== lock.version) throw new Error(`Built Pi version mismatch: ${version}`);
rmSync(source, { recursive: true, force: true });
renameSync(candidate, source);
writeFileSync(join(runtime, "installed.json"), JSON.stringify({ ...lock, node: process.versions.node }, null, 2) + "\n");
console.log(`Official Pi ${version} ready: ${join(source, lock.cli)}`);
