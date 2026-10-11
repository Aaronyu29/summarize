---
title: "Releasing"
kicker: "project"
summary: "Release checklist, Chrome Web Store submission, and Homebrew/core verification."
---

# Releasing

## Goals

- Ship npm packages (core first, then CLI).
- Tag + GitHub release.
- Submit the matching extension package to the Chrome Web Store unless the release is daemon-only.
- Leave formula updates to Homebrew/core autobump; optionally verify after it catches up.

## Checklist

1. `scripts/release.sh all` (local gates → build proof artifacts → pack/install verification → signed annotated tag). The tag-triggered Release workflow builds, signs, notarizes, verifies both macOS archives, then smoke-installs and publishes npm tarballs through GitHub OIDC, verifies provenance, and publishes the GitHub Release. Both npm packages must trust `steipete/summarize`, workflow `release.yml`, with no environment.
2. After CI succeeds, run `scripts/release.sh smoke`, `scripts/release.sh promote` (read-only dist-tag check), and `scripts/release.sh github`. Verify the GitHub release notes and download all assets to check `SHA256SUMS`, signatures, and notarization.
3. Submit the Chrome extension update:
   - Skip this step only when every shipped change is daemon-side and the packaged extension and its companion contract are unchanged.
   - Using the existing authenticated Chrome profile, open the [Chrome Web Store Developer Dashboard](https://chrome.google.com/webstore/devconsole/) and select item `cejgnmmhbbpdmjnfppjdfkocebngehfg`.
   - Upload `dist-chrome/summarize-chrome-extension-v<version>.zip`.
   - Add source-backed Privacy-tab justifications for any newly declared permissions, then save the draft.
   - Submit for review with automatic publishing enabled. Verify the dashboard shows the exact draft version as `Pending review` or `Published`; the public listing can remain on the prior version until review completes.
4. Optional follow-up, not a release gate: after Homebrew/core autobump catches up, verify the formula reflects the new version:
   - `scripts/release.sh homebrew`
   - `brew install summarize`
   - `summarize --version` matches tag.
   - Run a feature added in the release (for example `summarize daemon install`).
5. If a required release step fails, fix it before closeout. Homebrew/core autobump lag does not require re-cutting or delaying a completed release.

## Common failure

- A Release job failed after partial npm publication.
  Rerun failed jobs in the same GitHub Actions Release run. Validation and publication accept an existing version only when its integrity and downloaded tarball match the frozen build exactly; other registry errors or mismatches fail closed. No local `publish` or `publish-cli` fallback exists. Never move the tag after anything was published. If source changes are required after publication, ship a patch release.
- Stable versions publish directly to `latest` only after the local-tarball smoke passes; prereleases use `next`. GitHub OIDC cannot move dist-tags afterward. `promote` only verifies the expected tag.

- NPM/GitHub release updated, but Homebrew/core still serves the old version.
  Expected while autobump is pending. Do not manually update the formula or block the release; step 4 can verify it later.
- GitHub Release contains the extension ZIP, but the Chrome Web Store still serves an old version.
  Fix: submit the matching ZIP in step 3 and verify the draft/review state before closeout.
