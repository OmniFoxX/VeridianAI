/**
 * VeridianAI Haptic Manager
 * Wraps the Web Vibration API with enable/disable toggle.
 * Patterns map to meaningful UI events.
 */

const Haptic = (() => {
  let _enabled = true;
  const _supported = 'vibrate' in navigator;

  const PATTERNS = {
    send:      [15],           // short tap — message sent
    receive:   [10, 40, 10],  // double tap — response started
    done:      [20, 30, 60],  // success pattern — generation complete
    error:     [80, 40, 80],  // error pattern
    gameScore: [30],           // game point scored
    gameDie:   [60, 30, 60],  // game over
    toggle:    [10],           // UI toggle
  };

  function vibrate(pattern) {
    if (!_enabled || !_supported) return;
    navigator.vibrate(pattern);
  }

  function setEnabled(val) {
    _enabled = Boolean(val);
  }

  function isEnabled()   { return _enabled; }
  function isSupported() { return _supported; }

  return { vibrate, setEnabled, isEnabled, isSupported, PATTERNS };
})();

function toggleHaptic() {
  Haptic.setEnabled(!Haptic.isEnabled());
  const btn = document.getElementById('haptic-btn');
  if (btn) {
    btn.classList.toggle('active', Haptic.isEnabled());
    btn.title = Haptic.isEnabled() ? 'Haptic ON (click to disable)' : 'Haptic OFF (click to enable)';
  }
  Haptic.vibrate(Haptic.PATTERNS.toggle);
  // Persist preference
  updateSetting && updateSetting('haptic', Haptic.isEnabled());
}
