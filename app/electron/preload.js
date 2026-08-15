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

contextBridge.exposeInMainWorld('electronAPI', {

  // Read-only platform string ('win32', 'darwin', 'linux').
  // Useful for renderer-side conditional UI (e.g. hiding .bat-specific hints).
  platform: process.platform,

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
    const allowed = ['command-palette-action', 'app-ready', 'oracle-unstick',
                     'open-data-folder', 'veridian-decline-exit'];
    if (allowed.includes(channel)) {
      ipcRenderer.send(channel, data);
    }
  },

});