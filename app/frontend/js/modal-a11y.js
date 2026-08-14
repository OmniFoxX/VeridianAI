/* modal-a11y.js -- keyboard and screen-reader handling for dialogs.
 *
 * WHY THIS IS SHARED
 * Three modals were added in one release and all three got focus handling
 * subtly wrong in different ways: one never moved focus in, one never gave it
 * back, none contained Tab. Every dialog written from scratch reinvents this
 * and reinvents it slightly wrong, which is exactly the argument for one
 * implementation.
 *
 * WHAT IT DOES, AND WHY EACH PART MATTERS
 *
 *   Move focus IN     A sighted mouse user sees the dialog appear. A keyboard
 *                     or screen-reader user does not: focus is still on the
 *                     button behind it, so Tab walks the page they cannot see
 *                     while a dialog they were never told about sits on top.
 *                     (WCAG 2.4.3 Focus Order)
 *
 *   Contain Tab       Without it, Tab leaves the dialog and wanders the page
 *                     underneath. The user has no way to know they have left,
 *                     because visually nothing changed.
 *
 *   Give focus BACK   On close, focus returns to whatever opened the dialog.
 *                     Otherwise it falls to the top of the document and the
 *                     user has to walk the entire page back to where they were.
 *
 * NOT a focus TRAP in the WCAG 2.1.2 sense: every dialog using this has a
 * reachable control that closes it. Containment while open is the intended
 * behaviour; being unable to leave at all would be a failure.
 *
 * Pure-ASCII source, matching the other frontend modules.
 */
(function () {
  "use strict";

  var FOCUSABLE = [
    "a[href]", "button:not([disabled])", "input:not([disabled])",
    "select:not([disabled])", "textarea:not([disabled])",
    '[tabindex]:not([tabindex="-1"])',
  ].join(",");

  function focusable(container) {
    try {
      return Array.prototype.slice
        .call(container.querySelectorAll(FOCUSABLE))
        .filter(function (el) {
          return el.offsetParent !== null || el === document.activeElement;
        });
    } catch (e) {
      return [];
    }
  }

  /**
   * Make `container` behave like a dialog for keyboard users.
   * Returns a release() that restores everything. Call it on close.
   */
  window.modalA11y = function (container, opts) {
    opts = opts || {};
    var opener = document.activeElement;
    var onEscape = opts.onEscape || null;

    function keydown(e) {
      if (e.key === "Escape" && onEscape) {
        e.preventDefault();
        onEscape();
        return;
      }
      if (e.key !== "Tab") return;
      var items = focusable(container);
      if (!items.length) return;
      var first = items[0], last = items[items.length - 1];
      // Wrap at both ends so Tab and Shift+Tab stay inside.
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      } else if (!container.contains(document.activeElement)) {
        // Focus escaped some other way (a click outside, a script). Pull it back.
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", keydown, true);

    // Defer: the element may not be laid out yet, and focus() on a
    // zero-size element silently does nothing.
    setTimeout(function () {
      var items = focusable(container);
      var target = opts.initialFocus
        ? container.querySelector(opts.initialFocus)
        : null;
      (target || items[0] || container).focus();
    }, 30);

    return function release() {
      document.removeEventListener("keydown", keydown, true);
      try {
        // Only restore if the opener is still in the document -- returning
        // focus to a removed node drops it to <body>, which is worse than
        // leaving it alone.
        if (opener && document.contains(opener) && opener.focus) opener.focus();
      } catch (e) { /* focus is best-effort */ }
    };
  };
})();
