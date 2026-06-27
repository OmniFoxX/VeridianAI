WCAG Auditor Report
===================

Tool Version: 1.0.0
Target: E:\OracleAI_v2.11.11\frontend\index.html
Timestamp: 2026-06-27T03:47:54.330678+00:00

Summary
-------
| Metric | Count |
|--------|-------|
| Total  |    55 |
| Pass   |    42 |
| Fail   |     6 |
| Review |     7 |
| Manual |     0 |
| Error  |     0 |

FAIL
====

**1.3.2 - Meaningful Sequence - Correct reading sequence determinable**
4 issue(s) may disrupt meaningful reading sequence:
  1. [DIV] id='build-battle-rounds-row' | Meaningful content hidden with inline style.
         Text preview: 'Build Battle Rounds1 (quick)2 (deeper)3 (thorough)'
         Suggested fix: Use aria-hidden='true' for decorative, or ensure hidden content is not part of the reading sequence.
  2. [DIV] id='sn-token-box' | Meaningful content hidden with inline style.
         Text preview: 'Copy'
         Suggested fix: Use aria-hidden='true' for decorative, or ensure hidden content is not part of the reading sequence.
  3. [DIV] id='socials-view' | Meaningful content hidden with inline style.
         Text preview: '📡 Socials —…RefreshChannel settings (token / host)Sage auto-'
         Suggested fix: Use aria-hidden='true' for decorative, or ensure hidden content is not part of the reading sequence.
  4. [DIV] id='socials-deleteall-warn' | Meaningful content hidden with inline style.
         Text preview: '⚠ Heads up: the next click ofDeleteclears messages fromevery'
         Suggested fix: Use aria-hidden='true' for decorative, or ensure hidden content is not part of the reading sequence.

**1.3.5 - Identify Input Purpose - Input field purpose programmatically determinable**
2 input(s) missing appropriate autocomplete tokens:
  1. [INPUT] id='setting-node-name' name='' | Likely purpose 'name' but no specific autocomplete token set.
         Suggested fix: autocomplete="name"
  2. [INPUT] id='sk-relay-target' name='' | Likely purpose 'name' but no specific autocomplete token set.
         Suggested fix: autocomplete="name"

**2.4.6 - Headings and Labels - Headings and labels describe topic or purpose**
17 heading(s) or label(s) are empty or non-descriptive:
  1. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  2. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  3. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  4. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  5. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  6. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  7. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  8. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  9. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  10. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  11. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  12. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  13. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  14. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  15. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  16. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.
  17. [LABEL] id='' for='' | Label is empty or contains only whitespace.
         Suggested fix: Add descriptive text that identifies the purpose of the associated input.

**2.5.3 - Label in Name - Programmatic name includes visible label text**
14 element(s) have aria-label that excludes visible text:
  1. [BUTTON] id='privacy-btn' | Visible text '🔒' not found in aria-label='privacy mode'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='🔒 <additional context>'.
  2. [BUTTON] id='haptic-btn' | Visible text '📳' not found in aria-label='haptic feedback'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='📳 <additional context>'.
  3. [BUTTON] id='theme-btn' | Visible text '○' not found in aria-label='toggle light/dark theme'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='○ <additional context>'.
  4. [BUTTON] id='sidebar-btn' | Visible text '≡' not found in aria-label='toggle sidebar'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='≡ <additional context>'.
  5. [LABEL] id='' | Visible text '📎' not found in aria-label='attach file'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='📎 <additional context>'.
  6. [BUTTON] id='gen-image-btn' | Visible text '🎨' not found in aria-label='generate image'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='🎨 <additional context>'.
  7. [BUTTON] id='nudge-btn' | Visible text '👋' not found in aria-label='send nudge to sage'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='👋 <additional context>'.
  8. [BUTTON] id='send-btn' | Visible text '▶' not found in aria-label='send message'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='▶ <additional context>'.
  9. [BUTTON] id='vibe-toggle' | Visible text '✨' not found in aria-label='vibe coding prompts'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='✨ <additional context>'.
  10. [BUTTON] id='' | Visible text '✕' not found in aria-label='close games'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='✕ <additional context>'.
  11. [BUTTON] id='' | Visible text '☄' not found in aria-label='asteroids'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='☄ <additional context>'.
  12. [BUTTON] id='' | Visible text '🐍' not found in aria-label='snake'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='🐍 <additional context>'.
  13. [BUTTON] id='' | Visible text '✕○' not found in aria-label='tic-tac-toe'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='✕○ <additional context>'.
  14. [BUTTON] id='' | Visible text '📡' not found in aria-label='socials'.
         Suggested fix: Ensure aria-label starts with or contains the visible text, e.g. aria-label='📡 <additional context>'.

**3.3.3 - Error Suggestion - Error correction suggestions provided where known**
5 constrained input(s) lack error suggestion regions:
  1. [INPUT] id='setting-auto-reprompt-max' name='' type='number' | No error suggestion region detected for constrained input.
         Suggested fix: Add aria-describedby='setting-auto-reprompt-max-hint' and a sibling <span id='setting-auto-reprompt-max-hint'> explaining the expected format, e.g. 'Enter a valid email address'.
  2. [INPUT] id='setting-max-tokens' name='' type='number' | No error suggestion region detected for constrained input.
         Suggested fix: Add aria-describedby='setting-max-tokens-hint' and a sibling <span id='setting-max-tokens-hint'> explaining the expected format, e.g. 'Enter a valid email address'.
  3. [INPUT] id='setting-n-ctx' name='' type='number' | No error suggestion region detected for constrained input.
         Suggested fix: Add aria-describedby='setting-n-ctx-hint' and a sibling <span id='setting-n-ctx-hint'> explaining the expected format, e.g. 'Enter a valid email address'.
  4. [INPUT] id='setting-gpu-layers' name='' type='number' | No error suggestion region detected for constrained input.
         Suggested fix: Add aria-describedby='setting-gpu-layers-hint' and a sibling <span id='setting-gpu-layers-hint'> explaining the expected format, e.g. 'Enter a valid email address'.
  5. [INPUT] id='tavily-input' name='' type='password' | No error suggestion region detected for constrained input.
         Suggested fix: Add aria-describedby='tavily-input-hint' and a sibling <span id='tavily-input-hint'> explaining the expected format, e.g. 'Enter a valid email address'.

**4.1.3 - Status Messages - Status messages programmatically determinable without focus**
2 status region issue(s) detected:
  1. [SPAN] id='' class='key-status none' | Looks like a status region (matched: ['status']) but has no role='status'/'alert' or aria-live attribute.
         Suggested fix: Add role='status' and aria-live='polite' for non-urgent messages, or role='alert' and aria-live='assertive' for urgent ones.
  2. [DIV] role='status' | Live region has no id attribute.
         Suggested fix: Add a unique id so JavaScript can reliably target and update this region.

REVIEW — Needs Human Verification
==================================

**2.1.2 - No Keyboard Trap - Focus can always move away from any component**
2 focus-managed widget(s) require keyboard trap testing:
  1. [DIV] id='oai-disclaimer-overlay' role='dialog' class=''
         Focusable children: 1
         Escape mechanism: NOT DETECTED
         Suggested test: Tab into this widget using only the keyboard. Verify focus can exit via Tab, Shift+Tab, or Escape without requiring a mouse.
         Suggested fix: Add an onkeydown handler that closes/exits on Escape, and ensure a visible close button is present and keyboard reachable.
  2. [DIV] id='socials-threads' role='tablist' class='socials-threads'
         Focusable children: 0
         Escape mechanism: NOT DETECTED
         Suggested test: Tab into this widget using only the keyboard. Verify focus can exit via Tab, Shift+Tab, or Escape without requiring a mouse.
         Suggested fix: Add an onkeydown handler that closes/exits on Escape, and ensure a visible close button is present and keyboard reachable.

**2.4.11 - Focus Not Obscured Minimum - Focused component not entirely hidden by other content**
38 fixed/sticky element(s) may obscure focused components:
  1. [DIV] id='oai-disclaimer-overlay' class='' | position:fixed z-index:99999
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this fixed element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  2. [HEADER] id='' class='app-header' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  3. [DIV] id='' class='header-brand' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  4. [DIV] id='' class='header-controls' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  5. [DIV] id='' class='header-toggles' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  6. [H2] id='' class='panel-header' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  7. [H2] id='' class='panel-header' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  8. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  9. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  10. [BUTTON] id='' class='toolbar-btn danger' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  11. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  12. [BUTTON] id='' class='toolbar-btn danger' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  13. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  14. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  15. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  16. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  17. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  18. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  19. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  20. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  21. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  22. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  23. [BUTTON] id='' class='toolbar-btn danger' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  24. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  25. [BUTTON] id='' class='toolbar-btn danger' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  26. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  27. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  28. [H2] id='' class='panel-header' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  29. [DIV] id='' class='chat-toolbar' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  30. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  31. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  32. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  33. [BUTTON] id='privacy-toolbar-btn' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  34. [DIV] id='' class='input-footer' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  35. [DIV] id='' class='oracle-panel-header' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  36. [BUTTON] id='socials-clear-thread' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  37. [BUTTON] id='socials-delete-all' class='toolbar-btn danger' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.
  38. [BUTTON] id='' class='toolbar-btn' | position:class-inferred
         This element may obscure focused components beneath it.
         Suggested test: Tab through all interactive elements on the page and verify none are completely hidden behind this class-inferred element when focused.
         Suggested fix: Add scroll-margin-top or scroll-padding-top equal to the height of fixed headers, so focused elements scroll into view clear of the obstruction.

**2.4.5 - Multiple Ways - More than one way to locate a page**
Fewer than 2 navigation mechanisms detected:
  1. Navigation mechanisms present (1):
         ✓ Navigation landmark (<nav> aria-label='Settings panels')
         Missing mechanisms (3):
         ✗ Search input (type='search', role='search', or search placeholder)
         ✗ Sitemap or table of contents link
         ✗ Breadcrumb navigation (aria-label='breadcrumb' or .breadcrumb class)
         Suggested test: Verify at least 2 of the above mechanisms are available and functional across all pages of the site.
         Suggested fix: Implement at least one missing mechanism. The most impactful additions would be: ["Search input (type='search', role='search', or search placeholder)", 'Sitemap or table of contents link', "Breadcrumb navigation (aria-label='breadcrumb' or .breadcrumb class)"].

**3.1.2 - Language of Parts - Language of each passage programmatically determinable**
No sub-page lang attributes found. If the page contains passages in a language different from the page default, those elements need a lang attribute.
  Suggested test: Review page content for any foreign language passages, quotes, or phrases and ensure each has a lang attribute.
  Suggested fix: Add lang='xx' to any element containing content in a different language, e.g. <span lang='fr'>Bonjour</span>.

**3.2.3 - Consistent Navigation - Navigation consistent across pages**
1 nav landmark(s) inventoried — verify consistency across all pages:
  1. [NAV] id='' aria-label='Settings panels'
         Links (0): []
         Suggested test: Compare this nav structure and link order against all other pages of the site. Order and labels must be consistent unless the user has changed them.

**3.2.4 - Consistent Identification - Components with same function identified consistently**
5 functional group(s) have inconsistent identification:
  1. [FUNCTION: CLOSE] 8 element(s) with inconsistent labels:
         [INPUT] id='setting-auto-reprompt-max' label='auto-continue max'
         [INPUT] id='setting-auto-reprompt-text' label='auto-continue text'
         [INPUT] id='setting-max-tokens' label='max tokens'
         [INPUT] id='setting-n-ctx' label='context window (n_ctx)'
         [INPUT] id='toggle-codeexec' label='enable codeexec'
         [A] id='' label='canonical source: github.com/omnifoxx'
         [BUTTON] id='' label='✕  clear chat'
         [BUTTON] id='' label='close games'
         Unique labels found: {('A', '', 'canonical source: github.com/omnifoxx'), ('INPUT', 'setting-max-tokens', 'max tokens'), ('INPUT', 'setting-n-ctx', 'context window (n_ctx)'), ('INPUT', 'setting-auto-reprompt-max', 'auto-continue max'), ('INPUT', 'toggle-codeexec', 'enable codeexec'), ('INPUT', 'setting-auto-reprompt-text', 'auto-continue text'), ('BUTTON', '', '✕ \xa0clear chat'), ('BUTTON', '', 'close games')}
         Suggested test: Verify all 'close' controls across the site use the same label consistently.
         Suggested fix: Standardise to one label for this function, e.g. always use '['close', 'dismiss', '×', '✕', 'x']' or its capitalised form across all instances.
  2. [FUNCTION: SUBMIT] 10 element(s) with inconsistent labels:
         [INPUT] id='setting-max-tokens' label='max tokens'
         [INPUT] id='toggle-browser-cookies' label='enable browser cookies'
         [BUTTON] id='' label='reveal token to copy'
         [BUTTON] id='' label='reset (new token)'
         [INPUT] id='sn-token' label='sn token'
         [INPUT] id='sn-paste-token' label='pair: paste a token from another node'
         [BUTTON] id='' label='set token'
         [BUTTON] id='nudge-btn' label='send nudge to sage'
         [BUTTON] id='send-btn' label='send message'
         [BUTTON] id='' label='send to channel'
         Unique labels found: {('INPUT', 'setting-max-tokens', 'max tokens'), ('BUTTON', '', 'reset (new token)'), ('BUTTON', 'nudge-btn', 'send nudge to sage'), ('INPUT', 'sn-token', 'sn token'), ('BUTTON', '', 'reveal token to copy'), ('INPUT', 'sn-paste-token', 'pair: paste a token from another node'), ('INPUT', 'toggle-browser-cookies', 'enable browser cookies'), ('BUTTON', 'send-btn', 'send message'), ('BUTTON', '', 'set token'), ('BUTTON', '', 'send to channel')}
         Suggested test: Verify all 'submit' controls across the site use the same label consistently.
         Suggested fix: Standardise to one label for this function, e.g. always use '['submit', 'send', 'go', 'confirm', 'ok']' or its capitalised form across all instances.
  3. [FUNCTION: CANCEL] 6 element(s) with inconsistent labels:
         [BUTTON] id='haptic-btn' label='haptic feedback'
         [A] id='' label='canonical source: github.com/omnifoxx'
         [INPUT] id='toggle-node-server' label='enable node server'
         [INPUT] id='sn-paste-token' label='pair: paste a token from another node'
         [INPUT] id='setting-remote-node-url' label='remote node url (machine to offload to)'
         [INPUT] id='sk-import-file' label='import a .skill file (offline -- no network needed)'
         Unique labels found: {('A', '', 'canonical source: github.com/omnifoxx'), ('INPUT', 'toggle-node-server', 'enable node server'), ('INPUT', 'sk-import-file', 'import a .skill file (offline -- no network needed)'), ('BUTTON', 'haptic-btn', 'haptic feedback'), ('INPUT', 'sn-paste-token', 'pair: paste a token from another node'), ('INPUT', 'setting-remote-node-url', 'remote node url (machine to offload to)')}
         Suggested test: Verify all 'cancel' controls across the site use the same label consistently.
         Suggested fix: Standardise to one label for this function, e.g. always use '['cancel', 'abort', 'back', 'no']' or its capitalised form across all instances.
  4. [FUNCTION: DELETE] 3 element(s) with inconsistent labels:
         [BUTTON] id='' label='delete saved tavily key'
         [INPUT] id='socials-deleteall-arm' label='arm delete-all: clears every channel's messages'
         [BUTTON] id='socials-delete-all' label='delete'
         Unique labels found: {('BUTTON', '', 'delete saved tavily key'), ('BUTTON', 'socials-delete-all', 'delete'), ('INPUT', 'socials-deleteall-arm', "arm delete-all: clears every channel's messages")}
         Suggested test: Verify all 'delete' controls across the site use the same label consistently.
         Suggested fix: Standardise to one label for this function, e.g. always use '['delete', 'remove', 'trash', 'erase']' or its capitalised form across all instances.
  5. [FUNCTION: NEXT] 2 element(s) with inconsistent labels:
         [INPUT] id='setting-auto-reprompt-max' label='auto-continue max'
         [INPUT] id='setting-auto-reprompt-text' label='auto-continue text'
         Unique labels found: {('INPUT', 'setting-auto-reprompt-max', 'auto-continue max'), ('INPUT', 'setting-auto-reprompt-text', 'auto-continue text')}
         Suggested test: Verify all 'next' controls across the site use the same label consistently.
         Suggested fix: Standardise to one label for this function, e.g. always use '['next', 'forward', 'continue', '›', '»']' or its capitalised form across all instances.

**3.2.6 - Consistent Help - Help mechanisms appear in consistent location**
0 help mechanism(s) inventoried — verify consistent placement across all pages:
  1. No help mechanisms detected on this page.
         Suggested fix: If help is available (contact, FAQ, live chat), add a consistently positioned link or button. WCAG 3.2.6 requires it appears in the same location across all pages — typically in the header or footer.

PASS
====

- 1.1.1 - Non-text Content - All non-text content has a text alternative
- 1.2.1 - Audio-only and Video-only Prerecorded - Alternatives provided
- 1.2.2 - Captions Prerecorded - Captions for all prerecorded audio
- 1.2.3 - Audio Description or Media Alternative Prerecorded
- 1.2.4 - Captions Live - Captions for all live audio
- 1.2.5 - Audio Description Prerecorded
- 1.3.1 - Info and Relationships - Structure programmatically determinable
- 1.3.3 - Sensory Characteristics - Instructions don't rely solely on shape, color, size, location, or sound
- 1.3.4 - Orientation - Content not restricted to single orientation
- 1.4.1 - Use of Color - Color not used as ONLY visual means of conveying information
- 1.4.10 - Reflow - Content reflows at 320px width without horizontal scrolling
- 1.4.11 - Non-text Contrast - UI components and graphics have 3:1 contrast ratio
- 1.4.12 - Text Spacing - No loss of content when text spacing is adjusted
- 1.4.13 - Content on Hover or Focus - Hoverable, dismissible, persistent tooltips/popups
- 1.4.2 - Audio Control - Mechanism to pause/stop auto-playing audio
- 1.4.3 - Contrast Minimum - Text contrast ratio at least 4.5:1 (3:1 for large text)
- 1.4.4 - Resize Text - Text resizable to 200% without loss of content or functionality
- 1.4.5 - Images of Text - Text used instead of images of text where possible
- 2.1.1 - Keyboard - ALL functionality operable via keyboard
- 2.1.4 - Character Key Shortcuts - Single character shortcuts can be turned off or remapped
- 2.2.1 - Timing Adjustable - Time limits can be turned off, adjusted, or extended
- 2.2.2 - Pause Stop Hide - Moving/blinking/scrolling content can be paused
- 2.3.1 - Three Flashes or Below Threshold - Nothing flashes more than 3 times per second
- 2.4.1 - Bypass Blocks - Mechanism to skip repeated content blocks (skip links)
- 2.4.2 - Page Titled - Pages have descriptive titles
- 2.4.3 - Focus Order - Focus order preserves meaning and operability
- 2.4.4 - Link Purpose In Context - Link purpose determinable from text or context
- 2.4.7 - Focus Visible - Keyboard focus indicator visible
- 2.5.1 - Pointer Gestures - Multipoint gestures have single pointer alternative
- 2.5.2 - Pointer Cancellation - Functions don't trigger on down-event alone
- 2.5.4 - Motion Actuation - Motion-operated functions have UI alternative and can be disabled
- 2.5.7 - Dragging Movements - Dragging functions have single pointer alternative
- 2.5.8 - Target Size Minimum - Touch targets at least 24x24 CSS pixels
- 3.1.1 - Language of Page - Default language programmatically determinable
- 3.2.1 - On Focus - Focus doesn't trigger context change
- 3.2.2 - On Input - Input doesn't auto-trigger context change
- 3.3.1 - Error Identification - Errors identified and described in text
- 3.3.2 - Labels or Instructions - Labels provided for all user inputs
- 3.3.4 - Error Prevention Legal Financial Data - Submissions reversible, checked, or confirmable
- 3.3.7 - Redundant Entry - Previously entered info auto-populated or selectable
- 3.3.8 - Accessible Authentication Minimum - No cognitive function tests required for authentication without alternative
- 4.1.2 - Name Role Value - UI components have programmatic name, role, and state
