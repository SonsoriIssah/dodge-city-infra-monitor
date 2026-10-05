/**
 * Entry point for `node --test dashboard/tests`.
 *
 * Node 22 and later treat the arguments of `--test` as files or glob patterns, so a bare directory is run as a
 * module: this file is what the directory resolves to. It loads every `*.test.js` next to it, so the command of
 * the build contract runs the whole suite. `node --test "dashboard/tests/*.test.js"` runs the same files, one
 * process per file.
 */

import { readdirSync } from 'node:fs';

const here = new URL('./', import.meta.url);
const files = readdirSync(here)
  .filter((name) => name.endsWith('.test.js'))
  .sort();
for (const name of files) {
  await import(new URL(name, here).href);
}
