/* a11y-tooltip.js -- accessible replacement for native title= tooltips.
 *
 * WCAG 1.4.13 (Content on Hover or Focus): a native `title` tooltip is not
 * keyboard-reachable, cannot be dismissed, and is not persistent/hoverable.
 * This module renders ONE shared tooltip element, driven by [data-tip], that:
 *   - appears on mouse hover AND on keyboard focus,
 *   - is dismissible with Escape,
 *   - stays put while you hover the trigger OR the tooltip itself,
 *   - is linked via aria-describedby so screen readers announce it.
 *
 * The build step renames every static title="..." to data-tip="..." (kills the
 * native tooltip + the audit fail); icon-only controls also get an aria-label
 * for their name. To avoid a screen reader reading the same string twice, we
 * only wire aria-describedby when the tip adds info beyond the accessible name.
 *
 * Hidden state uses visibility:hidden + pointer-events:none so the tooltip can
 * never become an invisible click-blocker (see the unclickable-UI history).
 * Pure-ASCII, dependency-free.
 */
(function () {
  "use strict";

  var TIP_ID = "a11y-tip";
  var tip = null;          // the single shared tooltip element
  var current = null;      // the trigger the tooltip currently describes
  var hideTimer = null;

  function ensureTip() {
    if (tip) return tip;
    tip = document.createElement("div");
    tip.id = TIP_ID;
    tip.className = "a11y-tip";
    tip.setAttribute("role", "tooltip");
    tip.setAttribute("aria-hidden", "true");
    tip.addEventListener("mouseenter", function () { clearTimeout(hideTimer); });
    tip.addEventListener("mouseleave", scheduleHide);
    document.body.appendChild(tip);
    return tip;
  }

  function trigger(node) {
    return (node && node.closest) ? node.closest("[data-tip]") : null;
  }

  function show(el) {
    var text = el.getAttribute("data-tip");
    if (!text) return;
    var t = ensureTip();
    clearTimeout(hideTimer);
    t.textContent = text;
    t.setAttribute("aria-hidden", "false");
    current = el;
    // Only describe when the tip says something the name doesn't already.
    if ((el.getAttribute("aria-label") || "") !== text) {
      el.setAttribute("aria-describedby", TIP_ID);
    }
    position(el, t);
  }

  function hide() {
    clearTimeout(hideTimer);
    if (!tip) return;
    tip.setAttribute("aria-hidden", "true");
    if (current) {
      // Only strip the describedby WE added (it points at our shared tip).
      if (current.getAttribute("aria-describedby") === TIP_ID) {
        current.removeAttribute("aria-describedby");
      }
      current = null;
    }
  }
  function scheduleHide() { clearTimeout(hideTimer); hideTimer = setTimeout(hide, 120); }

  function position(el, t) {
    var r = el.getBoundingClientRect();
    // Measure at origin first so width/height are real, then place.
    t.style.left = "0px";
    t.style.top = "0px";
    var tr = t.getBoundingClientRect();
    var gap = 8;
    var left = r.left + (r.width - tr.width) / 2;
    var top = r.top - tr.height - gap;            // prefer above the trigger
    if (top < 4) top = r.bottom + gap;            // flip below if no room above
    left = Math.max(6, Math.min(left, window.innerWidth - tr.width - 6));
    top = Math.max(6, Math.min(top, window.innerHeight - tr.height - 6));
    t.style.left = Math.round(left) + "px";
    t.style.top = Math.round(top) + "px";
  }

  // --- Delegated events (capture phase, so dynamically-added nodes work too) --
  document.addEventListener("mouseover", function (e) {
    var el = trigger(e.target);
    if (el && el !== current) show(el);
  }, true);
  document.addEventListener("mouseout", function (e) {
    var el = trigger(e.target);
    if (el && el === current) scheduleHide();
  }, true);
  document.addEventListener("focusin", function (e) {
    var el = trigger(e.target);
    if (el) show(el); else hide();
  }, true);
  document.addEventListener("focusout", function (e) {
    var el = trigger(e.target);
    if (el && el === current) hide();
  }, true);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && current && tip && tip.getAttribute("aria-hidden") === "false") {
      hide();   // dismissible without moving focus
    }
  }, true);
  // A click (e.g. opening a dialog) or a scroll should drop the tooltip.
  document.addEventListener("mousedown", hide, true);
  window.addEventListener("scroll", hide, true);

  window.a11yTooltipHide = hide;   // exposed for any code that needs to force-hide
})();
