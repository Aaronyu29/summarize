#!/usr/bin/env node
console.error(
  "Source-directory publishing is disabled. Release CI publishes verified pnpm tarballs using npm trusted publishing.",
);
process.exit(1);
