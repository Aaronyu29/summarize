#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const registry = "https://registry.npmjs.org";
const packages = ["@steipete/summarize-core", "@steipete/summarize"];
export const integrityOf = (bytes) =>
  `sha512-${createHash("sha512").update(bytes).digest("base64")}`;

// Only a registry 404 means absent. Auth, network, and server errors fail closed.
export async function checkExisting(pkg, request = fetch) {
  const response = await request(`${registry}/${encodeURIComponent(pkg.name)}/${pkg.version}`);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`${pkg.name}: registry returned HTTP ${response.status}`);
  const metadata = await response.json();
  if (
    metadata.name !== pkg.name ||
    metadata.version !== pkg.version ||
    metadata.dist?.integrity !== pkg.integrity
  ) {
    throw new Error(`${pkg.name}@${pkg.version}: published bytes do not match this build`);
  }
  const tarball = await request(metadata.dist.tarball);
  if (!tarball.ok || integrityOf(Buffer.from(await tarball.arrayBuffer())) !== pkg.integrity) {
    throw new Error(`${pkg.name}@${pkg.version}: downloaded tarball does not match this build`);
  }
  return metadata;
}

export async function checkAll(built, request = fetch) {
  const existing = [];
  for (const pkg of built) existing.push(await checkExisting(pkg, request));
  return existing;
}

export async function waitForPackage(pkg) {
  // npm processes new publications asynchronously, sometimes for several minutes.
  for (let attempt = 0; attempt < 120; attempt++) {
    const metadata = await checkExisting(pkg);
    if (metadata) return metadata;
    await new Promise((done) => setTimeout(done, 5000));
  }
  throw new Error(`${pkg.name}@${pkg.version}: not visible after publishing`);
}

function npm(args, options = {}) {
  return execFileSync("npm", args, { stdio: "inherit", ...options });
}

async function install(built, operation) {
  const prefix = await mkdtemp(join(tmpdir(), "summarize-npm-release-"));
  try {
    npm(["install", "--prefix", prefix, "--ignore-scripts", "--no-audit", ...built]);
    await operation(prefix);
  } finally {
    await rm(prefix, { recursive: true, force: true });
  }
}

async function main() {
  const [mode, directory] = process.argv.slice(2);
  if (!["check", "publish", "verify"].includes(mode) || !directory) {
    throw new Error("Usage: node scripts/npm-release.mjs check|publish|verify <pack-directory>");
  }
  const { version } = JSON.parse(await readFile("package.json", "utf8"));
  const built = await Promise.all(
    packages.map(async (name) => {
      const tarball = resolve(directory, `${name.slice(1).replace("/", "-")}-${version}.tgz`);
      return { name, version, tarball, integrity: integrityOf(await readFile(tarball)) };
    }),
  );
  const existing = await checkAll(built);
  if (mode === "check") {
    for (let i = 0; i < built.length; i++) {
      console.log(
        `${built[i].name}@${version}: ${existing[i] ? "identical, resumable" : "absent"}`,
      );
    }
    return;
  }
  const tag = version.includes("-") ? "next" : "latest";
  if (mode === "publish") {
    if (
      process.env.GITHUB_ACTIONS !== "true" ||
      process.env.GITHUB_REPOSITORY !== "steipete/summarize" ||
      !process.env.ACTIONS_ID_TOKEN_REQUEST_URL ||
      !process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN ||
      process.env.NODE_AUTH_TOKEN ||
      process.env.NPM_TOKEN
    ) {
      throw new Error("Publishing requires GitHub Actions OIDC without npm tokens");
    }
    // Smoke both exact local tarballs before either stable package can reach latest.
    await install(
      built.map((pkg) => pkg.tarball),
      async (prefix) => {
        const actual = execFileSync(
          process.execPath,
          [join(prefix, "node_modules/@steipete/summarize/dist/cli.js"), "--version"],
          { encoding: "utf8" },
        ).trim();
        if (actual !== version) throw new Error(`Smoke version ${actual} != ${version}`);
      },
    );
    for (let i = 0; i < built.length; i++) {
      if (!existing[i]) {
        npm(["publish", built[i].tarball, "--access", "public", "--tag", tag, "--ignore-scripts"]);
      }
      await waitForPackage(built[i]);
    }
  }
  for (const pkg of built) {
    const metadata = await waitForPackage(pkg);
    if (
      !metadata.dist?.attestations?.url ||
      metadata.dist.attestations.provenance?.predicateType !== "https://slsa.dev/provenance/v1"
    ) {
      throw new Error(`${pkg.name}: missing npm provenance attestation`);
    }
    const response = await fetch(`${registry}/-/package/${encodeURIComponent(pkg.name)}/dist-tags`);
    if (!response.ok || (await response.json())[tag] !== version) {
      throw new Error(`${pkg.name}: ${tag} does not point at ${version}`);
    }
    console.log(`${pkg.name}@${version}: integrity, ${tag}, and provenance metadata verified`);
  }
  // npm verifies registry signatures and Sigstore provenance cryptographically.
  await install(
    built.map((pkg) => `${pkg.name}@${version}`),
    async (prefix) => {
      npm(["audit", "signatures"], { cwd: prefix });
    },
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
