/**
 * VeridianAI Electron Preload
 * Exposes a minimal, safe API to the renderer via contextBridge.
 *
 * Security model:
 *   - nodeIntegration: false  → renderer cannot require() Node modules
 *   - contextIsolation: true  → renderer JS runs in a separate V8 context
 *   - This file is the ONLY bridge between those two worlds.
 *     Keep it small. Every line here is an attack surface.
 */

const { contextBridge, ipcRenderer } = require('electron');

// The send allowlist, hoisted so it can be BOTH enforced and advertised.
//
// v2.16.2: advertising it is the point. The renderer is served live from
// frontend/ by the Python backend, but this file ships inside app.asar and only
// changes when the Electron shell is repackaged. So a new renderer can be
// running against an old shell -- and when it asked for a channel that shell
// had never heard of, send() dropped it and returned undefined, exactly as
// designed. Silently.
//
// That cost Todd a full round of testing on the Developer Mode quit: the
// dialog appeared, the sign-out happened, and nothing else did. There was no
// way for the page to find out, because "refused" and "delivered" looked
// identical from the other side of the bridge.
//
// Now the renderer can ask what this shell actually supports and say something
// honest when the answer is no. The list is not a secret -- it is a fixed
// allowlist compiled into the app -- and publishing it removes a whole class
// of silent failure at the cost of nothing.
const ALLOWED_SEND = Object.freeze([
  'command-palette-action',
  'app-ready',
  'oracle-unstick',
  'open-data-folder',
  'veridian-decline-exit',
  'veridian-devmode-quit',
]);

contextBridge.exposeInMainWorld('electronAPI', {

  // Read-only platform string ('win32', 'darwin', 'linux').
  // Useful for renderer-side conditional UI (e.g. hiding .bat-specific hints).
  platform: process.platform,

  // What this shell will actually deliver. Absent on any build older than
  // v2.16.2 -- which is itself the useful signal: a renderer that finds this
  // missing knows it is talking to an older shell and must not assume.
  supportedChannels: ALLOWED_SEND,

  // --- Reload ---------------------------------------------------
  // Replaces any prior 'reload' listener before registering the new
  // one. Without removeAllListeners(), every hot-reload or re-mount
  // of the renderer that calls onReload() stacks another callback —
  // eventually firing the handler N times per event. One listener,
  // always.
  onReload: (cb) => {
    ipcRenderer.removeAllListeners('reload');
    ipcRenderer.on('reload', (_event, ...args) => cb(...args));
  },

  // --- Command Palette ------------------------------------------
  // Allows main.js to trigger the command palette from menu items
  // or global shortcuts without the renderer needing IPC knowledge.
  onCommandPalette: (cb) => {
    ipcRenderer.removeAllListeners('open-command-palette');
    ipcRenderer.on('open-command-palette', (_event, ...args) => cb(...args));
  },

  // --- Generic IPC send (renderer → main) -----------------------
  // Whitelist-only. The renderer can only send channels that are
  // explicitly listed here. This prevents a compromised renderer
  // from firing arbitrary IPC events into main.js.
  send: (channel, data) => {
    // 'open-data-folder' carries NO payload, deliberately. main.js resolves
    // the path itself, so the renderer can ask to open the user's own data
    // folder and cannot ask to open anything else. A channel that accepted a
    // path would be a file-explorer-anywhere primitive handed to web content.
    // 'veridian-decline-exit' carries NO payload either. It is sent only when
    // someone declines the first-run terms, and main.js decides what quitting
    // means -- the renderer cannot pass an exit code or anything else.
    // 'veridian-devmode-quit' is the same shape for the same reason: a
    // Developer Mode session cannot be allowed to end at the login screen,
    // because the log terminals belong to the LAUNCH and would be left open
    // for whoever signs in next. Payload-less -- the renderer asks to leave,
    // main.js decides what leaving means, and nothing about HOW to quit
    // crosses the bridge.
    //
    // Returns whether it was actually sent, so a caller that cares can tell
    // the difference. Callers should still check supportedChannels first --
    // this only helps the ones that look at the result.
    if (ALLOWED_SEND.includes(channel)) {
      ipcRenderer.send(channel, data);
      return true;
    }
    return false;
  },

});