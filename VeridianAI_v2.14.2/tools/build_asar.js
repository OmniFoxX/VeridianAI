#!/usr/bin/env node
/**
 * Rebuild resources/app.asar from electron/, without electron-builder.
 *
 * WHY THIS EXISTS
 * ---------------
 * The portable release ships a prebuilt `resources\app.asar` that
 * make_release.ps1 copies along. Nothing rebuilt it, so it sat at its
 * 2.12.16-era contents while electron\ was edited for weeks -- and
 * `VeridianAI.exe` runs the asar, not the loose folder.
 *
 * The obvious fix, `npm run pack-win`, drags in a full electron-builder run:
 * it downloads the ~100 MB Electron runtime into a cache, invokes the Windows
 * toolchain, and writes a complete dist\win-unpacked tree -- all to produce
 * one 2 MB archive of six files that are already sitting on disk.
 *
 * The asar is just a container. `@electron/asar` is already in node_modules
 * (electron-builder depends on it), so this packs it directly: no download, no
 * toolchain, no network. Seconds instead of a build.
 *
 * This does NOT replace electron-builder. It regenerates exactly one artifact,
 * which is the one that goes stale. The Electron runtime itself -- the exe,
 * the DLLs, locales -- is unchanged by editing main.js, which is precisely why
 * it has survived being stale for so long without anyone noticing.
 *
 *   node tools/build_asar.js [projectRoot]
 *
 * Verify afterwards with tools/verify_electron_payload.js.
 */
'use strict';

const fs   = require('fs');
const path = require('path');
const os   = require('os');

// path.resolve, not the raw argument: a relative root like "." would make
// the require() below resolve against THIS file's directory rather than the
// project, and report the dependency missing when it is present.
const root  = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const srcD  = path.join(root, 'electron');
const outP  = path.join(root, 'resources', 'app.asar');

let asar;
try {
  asar = require(path.join(srcD, 'node_modules', '@electron', 'asar'));
} catch (e) {
  console.error('Could not load @electron/asar from electron/node_modules.\n' +
                'Run `npm ci` in electron/ first.\n  ' + e.message);
  process.exit(2);
}

// What electron-builder puts in the asar: the app's own files, never its
// devDependencies. Mirrors the file list of the previously shipped archive.
const INCLUDE = ['main.js', 'preload.js', 'first_run.js', 'boot.html',
                 '.backend_mode'];
const INCLUDE_DIRS = ['assets'];

const stage = fs.mkdtempSync(path.join(os.tmpdir(), 'vai-asar-'));

let copied = [];
for (const f of INCLUDE) {
  const s = path.join(srcD, f);
  if (!fs.existsSync(s)) { console.log('  (skip, absent) ' + f); continue; }
  fs.copyFileSync(s, path.join(stage, f));
  copied.push(f);
}
for (const d of INCLUDE_DIRS) {
  const s = path.join(srcD, d);
  if (!fs.existsSync(s)) continue;
  fs.mkdirSync(path.join(stage, d), { recursive: true });
  for (const f of fs.readdirSync(s)) {
    const fp = path.join(s, f);
    if (fs.statSync(fp).isFile()) {
      fs.copyFileSync(fp, path.join(stage, d, f));
      copied.push(d + '/' + f);
    }
  }
}

// package.json is REWRITTEN, exactly as electron-builder does: the runtime
// needs `main` and the identity fields, and must not carry build config,
// scripts or dependency lists into the shipped archive.
const pkg = JSON.parse(fs.readFileSync(path.join(srcD, 'package.json'), 'utf8'));
const KEEP = ['name', 'version', 'description', 'author', 'homepage',
              'repository', 'main'];
const outPkg = {};
for (const k of KEEP) if (k in pkg) outPkg[k] = pkg[k];
if (!outPkg.main) outPkg.main = 'main.js';
fs.writeFileSync(path.join(stage, 'package.json'),
                 JSON.stringify(outPkg, null, 2) + '\n');
copied.push('package.json');

fs.mkdirSync(path.dirname(outP), { recursive: true });

asar.createPackage(stage, outP).then(() => {
  const sz = fs.statSync(outP).size;
  console.log(`Packed ${copied.length} file(s) -> resources/app.asar ` +
              `(${sz.toLocaleString()} bytes)`);
  for (const f of copied) console.log('   ' + f);
  console.log(`\napp.asar version: ${outPkg.version}`);
  console.log('Now run:  node tools/verify_electron_payload.js .');
  try { fs.rmSync(stage, { recursive: true, force: true }); } catch (e) {}
}).catch((e) => {
  console.error('asar pack failed: ' + e.message);
  process.exit(1);
});
