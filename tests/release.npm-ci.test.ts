import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { checkAll, checkExisting, integrityOf, waitForPackage } from "../scripts/npm-release.mjs";

const bytes = Buffer.from("synthetic package");
const pkg = { name: "@steipete/summarize-core", version: "0.0.0", integrity: integrityOf(bytes) };
const metadata = {
  ...pkg,
  dist: { integrity: pkg.integrity, tarball: "https://registry.npmjs.org/synthetic.tgz" },
};

function registry(body = metadata, tarball = bytes) {
  return vi
    .fn()
    .mockResolvedValueOnce(Response.json(body))
    .mockResolvedValueOnce(new Response(tarball));
}

describe("npm release recovery", () => {
  it("accepts only a 404 as an absent version", async () => {
    await expect(
      checkExisting(pkg, vi.fn().mockResolvedValue(new Response(null, { status: 404 }))),
    ).resolves.toBeNull();
  });

  it.each([401, 403, 429, 500, 503])("fails closed on HTTP %s", async (status) => {
    await expect(
      checkExisting(pkg, vi.fn().mockResolvedValue(new Response(null, { status }))),
    ).rejects.toThrow(`HTTP ${status}`);
  });

  it("fails closed on network errors", async () => {
    await expect(
      checkExisting(pkg, vi.fn().mockRejectedValue(new Error("network failed"))),
    ).rejects.toThrow("network failed");
  });

  it("accepts identical metadata and downloaded bytes", async () => {
    await expect(checkExisting(pkg, registry())).resolves.toEqual(metadata);
  });

  it("refuses a different integrity without downloading", async () => {
    const request = registry({
      ...metadata,
      dist: { ...metadata.dist, integrity: "sha512-other" },
    });
    await expect(checkExisting(pkg, request)).rejects.toThrow("published bytes do not match");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("refuses tarball bytes inconsistent with registry integrity", async () => {
    await expect(checkExisting(pkg, registry(metadata, Buffer.from("different")))).rejects.toThrow(
      "downloaded tarball does not match",
    );
  });

  it("checks both packages before allowing a partial-publish resume", async () => {
    const request = registry();
    request.mockResolvedValueOnce(new Response(null, { status: 404 }));
    await expect(
      checkAll([pkg, { ...pkg, name: "@steipete/summarize" }], request),
    ).resolves.toEqual([metadata, null]);
  });

  it("rejects a mismatching CLI even when core already matches", async () => {
    const request = registry();
    request.mockResolvedValueOnce(Response.json(metadata));
    await expect(checkAll([pkg, { ...pkg, name: "@steipete/summarize" }], request)).rejects.toThrow(
      "published bytes do not match",
    );
  });

  it.each(["publish", "publish-cli"])("retires local %s without executing npm", (phase) => {
    const result = spawnSync("bash", [resolve("scripts/release.sh"), phase], { encoding: "utf8" });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("publishing is owned by .github/workflows/release.yml");
  });

  it("refuses source-directory publication", () => {
    const result = spawnSync(process.execPath, [resolve("scripts/refuse-source-publish.mjs")], {
      encoding: "utf8",
    });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("Source-directory publishing is disabled");
  });

  it("keeps dist-tag verification read-only", () => {
    const source = readFileSync(resolve("scripts/release.sh"), "utf8");
    expect(source).not.toContain("npm dist-tag add");
    expect(source).not.toContain("pnpm publish");
  });
});

describe("npm publication visibility", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("waits beyond a minute for an asynchronously processed publication", async () => {
    vi.useFakeTimers();
    const request = vi.fn();
    for (let i = 0; i < 24; i++) request.mockResolvedValueOnce(new Response(null, { status: 404 }));
    request
      .mockResolvedValueOnce(Response.json(metadata))
      .mockResolvedValueOnce(new Response(bytes));
    vi.stubGlobal("fetch", request);
    const waiting = waitForPackage(pkg);
    await vi.advanceTimersByTimeAsync(120_000);
    await expect(waiting).resolves.toEqual(metadata);
  });

  it("bounds the processing wait at ten minutes", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    const waiting = expect(waitForPackage(pkg)).rejects.toThrow("not visible after publishing");
    await vi.advanceTimersByTimeAsync(600_000);
    await waiting;
  });
});
