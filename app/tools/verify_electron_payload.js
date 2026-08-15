#!/usr/bin/env node
/**
 * Does resources/app.asar actually contain the code in electron/ ?
 *
 * WHY THIS EXISTS
 * ---------------
 * The portable release ships TWO copies of the Electron app and only one of
 * them runs:
 *
 *   electron/main.js          <- the source. Edited constantly. Also COPIED
 *                                into the release, where it looks current.
 *   resources/app.asar        <- what VeridianAI.exe actually executes.
 *
 * make_release.ps1 checked that app.asar EXISTED and copied it along. Nothing
 * ever rebuilt it. It sat in the project root from 2026-07-26 onward, so every
 * portable release from then on shipped a 2.9.10-era shell while the loose
 * electron/main.js beside it showed the current code.
 *
 * The cost was five debugging sessions. Symptoms that had already been fixed
 * kept reproducing; the boot log that was supposed to explain them was never
 * written, because the shipped main.js had no boot-log code in it at all. The
 * Store build was fine throughout, because `build-store` runs electron-builder
 * and rebuilds the asar every time.
 *
 * A decoy copy of the source, sitting next to the stale artifact that really
 * runs, is close to the worst possible arrangement for diagnosis: every check
 * you make on the files in front of you says the fix is present.
 *
 *   node tools/verify_electron_payload.js [projectRoot]
 *
 * Exit 0 in sync, 1 stale, 2 could not tell.
 */
'use strict';

const fs   = require('fs');
const path = require('path');
const crypto = require('crypto');

const root  = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const asarP = path.join(root, 'resources', 'app.asar');
const srcD  = path.join(root, 'electron');

function readAsar(file) {
  const d = fs.readFileSync(file);
  // asar: [4] pickle size, [4] header pickle, [4] header string len, header JSON
  const hdrSize = d.readUInt32LE(12);
  const header  = JSON.parse(d.slice(16, 16 + hdrSize).toString('utf8'));
  // The header string is PADDED to a 4-byte boundary before the data section
  // begins. Omitting that shifts every extracted file by 1-3 bytes, which
  // yields the right LENGTH and the wrong CONTENT -- so files compare as
  // different while their sizes match exactly. Caught precisely that way.
  const base    = 16 + hdrSize + ((4 - (hdrSize % 4)) % 4);
  const out = {};
  (function walk(node, prefix) {
    for (const [name, v] of Object.entries(node.files || {})) {
      if (v.files) walk(v, prefix + name + '/');
      else if (typeof v.offset !== 'undefined') {
        const off = parseInt(v.offset, 10);
        out[prefix + name] = d.slice(base + off, base + off + v.size);
      }
    }
  })(header, '');
  return out;
}

const sha = (b) => crypto.createHash('sha256').update(b).digest('hex').slice(0, 12);

// Does this tree actually SHIP resources/ ? The Store tree keeps a stale
// app.asar too, but its extraFiles list has no 'resources' entry, so
// electron-builder generates a fresh one at build time and the stale file is
// never packaged. That is the control that proves the diagnosis: the same
// rotten artifact sat in both trees, and only the one that shipped it broke.
// Saying so here stops this script raising a false alarm in the Store tree.
let shipsResources = true;
try {
  const pj = JSON.parse(fs.readFileSync(path.join(srcD, 'package.json'), 'utf8'));
  const ef = (pj.build && pj.build.extraFiles) || [];
  if (ef.length) shipsResources = ef.some((e) => e && e.to === 'resources');
} catch (e) { /* assume it ships; a false alarm beats a missed one */ }

if (!fs.existsSync(asarP)) {
  if (!shipsResources) {
    console.log('No resources/app.asar, and this tree does not ship one ' +
                '(electron-builder generates it). Nothing to check.');
    process.exit(0);
  }
  console.error('[payload] resources/app.asar not found at ' + asarP);
  process.exit(2);
}

let packed;
try {
  packed = readAsar(asarP);
} catch (e) {
  console.error('[payload] could not read app.asar: ' + e.message);
  process.exit(2);
}

// package.json is rewritten by electron-builder, so it is not comparable.
// The .js files are copied verbatim, so they are.
const CHECK = ['main.js', 'preload.js', 'first_run.js'];
const stale = [];
const ok    = [];

for (const f of CHECK) {
  const srcP = path.join(srcD, f);
  if (!fs.existsSync(srcP)) continue;
  const src = fs.readFileSync(srcP);
  const pk  = packed[f];
  if (!pk) { stale.push([f, 'MISSING from app.asar', sha(src), '-']); continue; }
  if (Buffer.compare(src, pk) === 0) ok.push([f, sha(src)]);
  else stale.push([f, `${src.length} bytes in electron/, ${pk.length} in app.asar`,
                   sha(src), sha(pk)]);
}

console.log('Electron payload check -- ' + root);
console.log('  app.asar: ' + fs.statSync(asarP).mtime.toISOString());
for (const [f, h] of ok)  console.log(`  in sync  ${f}  (${h})`);
for (const r of stale)    console.log(`  STALE    ${r[0]}  ${r[1]}  src=${r[2]} packed=${r[3]}`);

if (!stale.length) {
  console.log('\napp.asar matches electron/. The shipped app is the code you edited.');
  process.exit(0);
}

if (!shipsResources) {
  console.log(`\n${stale.length} file(s) differ, but this tree's extraFiles does ` +
    `NOT include resources/,\nso this app.asar is never packaged -- ` +
    `electron-builder builds a fresh one.\nIt is a leftover. Harmless to the ` +
    `build, misleading to a human reading the tree:\nconsider deleting it.`);
  process.exit(0);
}

console.error(`
${stale.length} file(s) in app.asar do NOT match electron/.

VeridianAI.exe runs app.asar, NOT the loose electron/ folder -- so the release
would ship code you have not edited since the asar was last built, while the
electron/ folder in that same release displays the current source.

Fix -- normally you do NOT have to do this by hand:

    powershell -ExecutionPolicy Bypass -File tools\\make_release.ps1

make_release.ps1 repacks a stale asar itself, in the source tree, before it
takes the staging snapshot. If you want to do it manually anyway:

    node tools/build_asar.js .
    node tools/verify_electron_payload.js .

That repacks the asar directly and takes seconds. A full \`npm run pack-win\`
also works, but downloads the ~100 MB Electron runtime and drives the whole
Windows toolchain to regenerate one 500 KB archive of files already on disk --
and the Electron runtime is not the part that goes stale here.

CORRECTION (v2.15): an earlier version of this message said to repack "BEFORE
genmanifest, so the manifest hashes the artifact that ships". The manifest does
NOT hash the asar and never did -- build_integrity.py excludes the whole
electron/ directory, and .asar is not in INCLUDE_EXT. The two are independent:
the manifest covers loose source, and Electron covers the asar. Repack first
anyway, because then there is one order to remember instead of two rules and an
exception.`);
process.exit(1);
