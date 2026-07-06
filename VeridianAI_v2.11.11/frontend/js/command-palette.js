(function () {
    'use strict';

    // State
    let paletteVisible = false;
    let activeIndex = -1;

    // ── Action helpers (v2.1.11) ──────────────────────────────────────
    // Wire each command palette entry to the existing global function
    // that the matching button/handler in the UI already calls. We don't
    // duplicate logic — we reuse the same code paths so toolbar clicks
    // and command-palette picks behave identically.
    //
    // Globals referenced (defined elsewhere in the frontend):
    //   switchPanel, togglePrivacy, archiveChat, showLoadArchive,
    //   printChat, clearChat, reloadModels, toggleSidebar, toggleTheme,
    //   openGamePanel, closeGamePanel, toggleVibeBar, sendMessage
    //
    // All looked up at invocation time (when the user picks a command),
    // not at IIFE-load time, so script order is forgiving. Guarded with
    // typeof checks where a function might not exist (e.g. if a future
    // refactor drops one) so a single missing function doesn't break
    // every other palette entry.

    const SETTINGS_TABS = ['hardware', 'plugins', 'settings'];

    function safeCall(fnName, ...args) {
        const fn = window[fnName];
        if (typeof fn === 'function') {
            try { return fn(...args); }
            catch (e) { console.warn(`[CmdPalette] ${fnName}() threw:`, e); }
        } else {
            console.warn(`[CmdPalette] ${fnName} is not defined`);
        }
    }

    function navTabButtonFor(name) {
        // Find the toolbar tab button matching this panel name so
        // switchPanel can highlight it. Falls back to null which
        // switchPanel handles gracefully (switches without the
        // active-class update — minor visual cost, not a bug).
        return document.querySelector(`.nav-tab[onclick*="switchPanel('${name}'"]`);
    }

    function switchToPanel(name) {
        safeCall('switchPanel', name, navTabButtonFor(name));
    }

    function cycleSettingsTabs() {
        // Cycle the active settings panel: hardware → plugins → settings → hardware.
        // Reads the current state from the DOM rather than from a local
        // variable so a user clicking a tab between palette opens stays
        // in sync.
        const active   = document.querySelector('.panel.active');
        const current  = active?.id?.replace('panel-', '') || SETTINGS_TABS[0];
        const idx      = SETTINGS_TABS.indexOf(current);
        const next     = SETTINGS_TABS[(idx + 1) % SETTINGS_TABS.length];
        switchToPanel(next);
    }

    function toggleGamesPanel() {
        // Single command for both open and close — checks the DOM state.
        const panel = document.getElementById('oracle-panel');
        const isOpen = panel?.classList.contains('visible');
        if (isOpen) safeCall('closeGamePanel');
        else        safeCall('openGamePanel');
    }

    function openSocials() {
        // Open the Oracle panel, then select the Socials tab within it.
        safeCall('openGamePanel');
        setTimeout(function () {
            const tab = document.querySelector('.game-tab[onclick*="switchGame(\'socials\'"]');
            if (tab) tab.click();
        }, 60);
    }

    function focusElement(id) {
        // setTimeout defers focus until after the palette finishes
        // hiding — otherwise hidePalette's CSS display:none would
        // grab focus first and the dropdown would lose it immediately.
        setTimeout(() => {
            const el = document.getElementById(id);
            if (el) {
                el.focus();
                if (typeof el.scrollIntoView === 'function') {
                    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
                }
            }
        }, 60);
    }

    function attachFileViaPicker() {
        const input = document.getElementById('file-input');
        if (input) input.click();
    }

    function cancelOrStop() {
        // If a generation is streaming, sendMessage() (called with no
        // input) hits the abort path. If nothing is streaming, the
        // palette just closes (default behavior of executeActive).
        if (typeof sendMessage === 'function') {
            // sendMessage checks the `streaming` global; if true →
            // abort path. If false, it'll try to send the (empty)
            // input and early-return at the empty-text check. Safe
            // either way.
            try { sendMessage(); }
            catch (e) { console.warn('[CmdPalette] cancel failed:', e); }
        }
    }

    async function pasteIntoUserInput() {
        // Modern clipboard API needs the call to happen within a
        // user-gesture window. Selecting a palette command qualifies
        // as a gesture, but the await + setTimeout combo can break
        // that chain in some browsers. Best-effort: try the modern
        // API, fall back to focusing the input so Ctrl+V works.
        const input = document.getElementById('user-input');
        if (!input) return;
        setTimeout(async () => {
            input.focus();
            try {
                const text = await navigator.clipboard.readText();
                const start = input.selectionStart ?? input.value.length;
                const end   = input.selectionEnd   ?? input.value.length;
                input.value = input.value.slice(0, start) + text + input.value.slice(end);
                input.dispatchEvent(new Event('input', { bubbles: true }));
                // Move caret to end of pasted text
                const caret = start + text.length;
                input.setSelectionRange(caret, caret);
            } catch (_e) {
                // Clipboard permission denied or unsupported — leave the
                // input focused so the user can Ctrl+V themselves.
            }
        }, 60);
    }

    function copyViaExecCommand() {
        setTimeout(() => {
            try { document.execCommand('copy'); }
            catch (_e) {}
        }, 50);
    }

    function cutViaExecCommand() {
        setTimeout(() => {
            try { document.execCommand('cut'); }
            catch (_e) {}
        }, 50);
    }

    // ── Command list (v2.1.11): labels preserved exactly as Todd defined ──
    // them so muscle memory survives the wiring change. Order also kept.
    const commands = [
        { label: 'Cycle Settings Tabs',     action: cycleSettingsTabs },
        { label: 'Hardware',                action: () => switchToPanel('hardware') },
        { label: 'Plugins',                 action: () => switchToPanel('plugins') },
        { label: 'Settings',                action: () => switchToPanel('settings') },
        { label: 'Privacy',                 action: () => safeCall('togglePrivacy') },
        { label: 'Archive',                 action: () => safeCall('archiveChat') },
        { label: 'Load',                    action: () => safeCall('showLoadArchive') },
        { label: 'Print',                   action: () => safeCall('printChat') },
        { label: 'Clear Chat',              action: () => safeCall('clearChat') },
        { label: 'Select Primary Model',    action: () => focusElement('model-select') },
        { label: 'Select Secondary Model',  action: () => focusElement('setting-secondary-model') },
        { label: 'Select Tertiary Model',   action: () => focusElement('setting-tertiary-model') },
        { label: 'Refresh Models',          action: () => safeCall('reloadModels') },
        { label: 'Toggle Sidebar',          action: () => safeCall('toggleSidebar') },
        { label: 'Toggle Themes',           action: () => safeCall('toggleTheme') },
        { label: 'Open/Close Games',        action: toggleGamesPanel },
        { label: 'Open Socials',            action: openSocials },
        { label: 'Vibe Prompts',            action: () => safeCall('toggleVibeBar') },
        { label: 'Voice Input (push to talk)', action: () => safeCall('voicePushToTalk') },
        { label: 'Generate Image',          action: () => safeCall('generateImageManual') },
        { label: 'Send Nudge to Sage',      action: () => safeCall('handleNudgeClick') },
        { label: 'Attach',                  action: attachFileViaPicker },
        { label: 'Cancel',                  action: cancelOrStop },
        { label: 'Copy',                    action: copyViaExecCommand },
        { label: 'Cut',                     action: cutViaExecCommand },
        { label: 'Paste',                   action: pasteIntoUserInput },
    ];

    // Create palette elements
    const overlay = document.createElement('div');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'command-palette-title');
    overlay.setAttribute('tabindex', '-1');
    overlay.classList.add('command-palette-overlay');

    const title = document.createElement('div');
    title.id = 'command-palette-title';
    title.textContent = 'Command Palette';
    title.style.padding = '0.5rem 1rem';
    title.style.borderBottom = '1px solid var(--border, #333)';
    title.style.background = 'var(--bg, #060a14)';
    title.style.color = 'var(--fg, #fff)';

    // v2.1.11 fix: input is a combobox-pattern searchbox. The aria-controls
    // points at the listbox so screen readers announce options as the user
    // navigates, and aria-activedescendant is updated to the currently-
    // highlighted option's id WITHOUT moving DOM focus away from the input.
    // This is the WAI-ARIA combobox pattern and is what fixes the previous
    // "can only type one letter at a time" bug: the old code called
    // opt.focus() on every selection update, which stole focus from the
    // input so the next keystroke never reached the keydown handler that
    // owns Enter / Arrow / Home / End / Escape.
    const input = document.createElement('input');
    input.type = 'text';
    input.id   = 'command-palette-input';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-label', 'Search commands');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'true');
    input.setAttribute('aria-controls', 'command-palette-listbox');
    input.placeholder = 'Type to filter commands...';
    input.style.width = '100%';
    input.style.padding = '0.5rem 1rem';
    input.style.boxSizing = 'border-box';
    input.style.border = 'none';
    input.style.background = 'var(--bg, #060a14)';
    input.style.color = 'var(--fg, #fff)';
    input.style.fontSize = '1rem';

    const listbox = document.createElement('div');
    listbox.id = 'command-palette-listbox';     // matches input's aria-controls
    listbox.setAttribute('role', 'listbox');
    listbox.setAttribute('aria-label', 'Available commands');
    listbox.style.maxHeight = '30vh';
    listbox.style.overflowY = 'auto';
    listbox.style.padding = '0.5rem 0';
    listbox.style.boxSizing = 'border-box';

    overlay.appendChild(title);
    overlay.appendChild(input);
    overlay.appendChild(listbox);
    document.body.appendChild(overlay);

    // Styles (using CSS variables)
    const style = document.createElement('style');
    style.textContent = `
        .command-palette-overlay {
            position: fixed;
            inset: 0;
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            background: rgba(6, 10, 20, 0.92);
        }
        .command-palette-overlay[visible] {
            display: flex;
        }
        .command-palette-option {
            display: flex;
            align-items: center;
            padding: 0.25rem 1rem;
            cursor: pointer;
        }
        .command-palette-option[aria-selected="true"],
        .command-palette-option:hover {
            background: var(--accent, #0066ff);
            color: var(--fg, #fff);
        }
        .command-palette-option[aria-selected="false"] {
            color: var(--fg, #fff);
        }
    `;
    document.head.appendChild(style);

    // v2.1.11 fix: the currently-displayed (post-filter) command list.
    // activeIndex refers to a position in THIS list, not the master
    // commands[] list, so executeActive and the keyboard bounds need to
    // see the same array renderOptions just rendered. Previously the
    // keyboard handler used commands.length for bounds and executeActive
    // looked up commands[activeIndex] — both wrong as soon as the user
    // typed any filter text.
    let currentFiltered = commands.slice();

    function renderOptions(filtered) {
        currentFiltered = Array.isArray(filtered) ? filtered : [];
        listbox.innerHTML = '';
        if (currentFiltered.length === 0) {
            const empty = document.createElement('div');
            empty.setAttribute('role', 'option');
            empty.id = 'command-palette-option-empty';
            empty.textContent = 'No matches';
            empty.style.padding = '0.5rem 1rem';
            empty.style.color = 'var(--fg-muted, #888)';
            listbox.appendChild(empty);
            activeIndex = -1;
            input.removeAttribute('aria-activedescendant');
            return;
        }
        currentFiltered.forEach((cmd, idx) => {
            const opt = document.createElement('div');
            opt.setAttribute('role', 'option');
            opt.setAttribute('aria-selected', 'false');
            // Stable id per displayed position so aria-activedescendant
            // on the input can reference it. New IDs on every render is
            // fine — they're transient and only live as long as the
            // palette is open.
            opt.id = `command-palette-option-${idx}`;
            opt.tabIndex = -1;
            opt.textContent = cmd.label;
            opt.dataset.idx = idx;
            opt.classList.add('command-palette-option');
            // mousedown (not click) so the option fires BEFORE the
            // overlay's click-to-close handler robs the event.
            opt.addEventListener('mousedown', (e) => {
                e.preventDefault();   // don't blur the input
                activeIndex = idx;
                executeActive();
            });
            opt.addEventListener('mouseenter', () => {
                activeIndex = idx;
                updateOptionSelection();
            });
            listbox.appendChild(opt);
        });
        activeIndex = 0;
        updateOptionSelection();
    }

    function filterOptions(query) {
        const lower = query.trim().toLowerCase();
        if (lower === '') {
            return commands.slice();
        }
        return commands.filter(c => c.label.toLowerCase().includes(lower));
    }

    function updateOptionSelection() {
        // v2.1.11 fix: do NOT call opt.focus() — that was the bug that
        // broke typing after one character. Instead we use the WAI-ARIA
        // combobox pattern: focus stays on the input forever; we mark
        // which option is "active" via aria-selected on the option and
        // aria-activedescendant on the input. Screen readers honor this
        // pattern and announce the active option as the user arrows
        // through, even though DOM focus never moves.
        const options = listbox.querySelectorAll('[role="option"]');
        let activeEl = null;
        options.forEach((opt, idx) => {
            const selected = (idx === activeIndex);
            opt.setAttribute('aria-selected', selected ? 'true' : 'false');
            if (selected) activeEl = opt;
        });
        if (activeEl) {
            input.setAttribute('aria-activedescendant', activeEl.id);
            // Keep the highlighted option visible in the scrolling list.
            // block:'nearest' avoids jumpy scrolling when the option is
            // already in view.
            if (typeof activeEl.scrollIntoView === 'function') {
                activeEl.scrollIntoView({ block: 'nearest' });
            }
        } else {
            input.removeAttribute('aria-activedescendant');
        }
    }

    function executeActive() {
        // v2.1.11 fix: look up the picked command in the FILTERED list
        // (currentFiltered), not in the unfiltered master commands list.
        // The old code did commands[activeIndex] which only happened to
        // work when the filter was empty.
        if (activeIndex >= 0 && activeIndex < currentFiltered.length) {
            const cmd = currentFiltered[activeIndex];
            if (cmd && typeof cmd.action === 'function') {
                try { cmd.action(); }
                catch (e) { console.warn('[CmdPalette] action threw:', e); }
            }
        }
        hidePalette();
    }

    function showPalette() {
        overlay.setAttribute('visible', '');
        paletteVisible = true;
        input.value = '';
        // v2.1.11 fix: render the FULL command list on open so the user
        // can see all 20 commands immediately. Old code called
        // renderOptions([]) which showed "No matches" until the user
        // typed something — confusing and unnecessary.
        renderOptions(filterOptions(''));
        // Focus AFTER render so layout is settled — minor but avoids a
        // visible blink in some browsers.
        setTimeout(() => input.focus(), 0);
    }

    function hidePalette() {
        overlay.removeAttribute('visible');
        paletteVisible = false;
        activeIndex = -1;
        // Optionally return focus to trigger (button or last active element)
    }

    // Event listeners
    input.addEventListener('input', (e) => {
        renderOptions(filterOptions(e.target.value));
    });

    input.addEventListener('keydown', (e) => {
        // v2.1.11 fix: bounds now use currentFiltered.length (what's
        // actually rendered) instead of commands.length (the master
        // list). The old code let ArrowDown step past the visible
        // options into invalid indices when a filter was active.
        switch (e.key) {
            case 'Enter':
                e.preventDefault();
                executeActive();
                break;
            case 'Escape':
                e.preventDefault();
                hidePalette();
                break;
            case 'ArrowDown':
                e.preventDefault();
                if (activeIndex < currentFiltered.length - 1) activeIndex++;
                updateOptionSelection();
                break;
            case 'ArrowUp':
                e.preventDefault();
                if (activeIndex > 0) activeIndex--;
                updateOptionSelection();
                break;
            case 'Home':
                e.preventDefault();
                if (currentFiltered.length > 0) {
                    activeIndex = 0;
                    updateOptionSelection();
                }
                break;
            case 'End':
                e.preventDefault();
                if (currentFiltered.length > 0) {
                    activeIndex = currentFiltered.length - 1;
                    updateOptionSelection();
                }
                break;
        }
    });

    // Click outside to close
    overlay.addEventListener('mousedown', (e) => {
        if (e.target === overlay) {
            hidePalette();
        }
    });

    // Global Ctrl+Shift+Space — open the command palette.
    //
    // v2.1.11 fix: this handler was dead in two ways before:
    //   1. The whole file failed to parse due to a stray `|` at EOF, so
    //      no listeners on this script ever attached. (Fixed below.)
    //   2. Even if the file parsed, this line compared `e.key.toLowerCase()`
    //      against the string `'Space'`. For the spacebar, `e.key` is the
    //      single space character (" "), NOT the word "Space". `e.code`
    //      is the one that returns "Space". And the comment claimed Ctrl+
    //      Shift+X but the code only checked ctrlKey, not shiftKey.
    //
    // Correct check: use e.code (physical key, layout-independent) and
    // require both Ctrl AND Shift. metaKey kept for Cmd+Shift+Space on
    // macOS users for forward compatibility.
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.code === 'Space') {
            e.preventDefault();
            // Toggle: if already open, close; lets the same combo dismiss it.
            if (paletteVisible) {
                hidePalette();
            } else {
                showPalette();
            }
        }
    });

    // At the bottom of your command palette IIFE, after the Ctrl+Shift+Space listener:
	if (window.electronAPI?.onCommandPalette) {
      window.electronAPI.onCommandPalette(() => showPalette());
	}
	
	// Toolbar button (assume element with id="command-palette-trigger" or data-trigger)
    const triggerBtn = document.getElementById('command-palette-trigger') ||
                       document.querySelector('[data-command-palette-trigger]');
    if (triggerBtn) {
        triggerBtn.addEventListener('click', (e) => {
            e.preventDefault();
            showPalette();
        });
    }

    // Hide on scroll (optional)
    let scrollTimeout;
    window.addEventListener('scroll', () => {
        clearTimeout(scrollTimeout);
        scrollTimeout = setTimeout(() => {
            if (paletteVisible) hidePalette();
        }, 150);
    });
})();
