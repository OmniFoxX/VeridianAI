/**
 * OracleAI — App Initialization v2
 */

document.addEventListener('DOMContentLoaded', async () => {
  // 1. Config first
  await loadSettings();

  // 2. WebSocket for chat
  connectWS();

  // 3. Hardware panel
  loadHardware();

  // 4. Model list
  reloadModels();

  // 4b. Sage Network status (fingerprint, toggle, remote url)
  snLoadStatus();

  // 5. Plugins
  loadPlugins();

  // 6. Init games (runs in background, scoped keyboard)
  GameManager.init();

  // 7. Focus input
  const input = document.getElementById('user-input');
  if (input) {
    input.focus();
    input.addEventListener('paste', () => setTimeout(() => autoResize(input), 0));
  }

  // 8. Fit game canvas when panel opens
  window.addEventListener('resize', fitGameCanvas);
  
  // 9. Default page title (WCAG 2.4.2)
  document.title = "OracleAI Chat";

  console.log('%cOracleAI v2.9.10 ready', 'color:#f0a500;font-size:14px;font-weight:bold;font-family:Georgia,serif');
});

function fitGameCanvas() {
  const panel = document.getElementById('oracle-panel');
  const canvas = document.getElementById('game-canvas');
  if (!panel || !canvas) return;
  const w = parseInt(getComputedStyle(document.documentElement)
                .getPropertyValue('--oracle-w')) || 300;
  canvas.width = w;
  canvas.height = Math.round(w * 1.07);
}
