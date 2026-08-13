/* api-key.js -- rotate the API key from inside the app.
 *
 * WHY
 * The key lets external tools (Continue.dev, Claude Desktop, curl) reach this
 * machine. Until now the only way to replace it was a .bat file that the
 * package does not ship -- so a user who suspected their key was exposed had
 * no way to do anything about it from the application that issued it.
 *
 * SHOWN ONCE, AND THAT IS THE POINT
 * The keystore holds a HASH, never the token. That is what makes a stolen
 * keystore worthless. The same property means we genuinely cannot show the key
 * again later -- not "for security theatre", but because it does not exist
 * anywhere after this dialog closes. So the dialog says so plainly, and makes
 * copying the obvious action rather than an afterthought.
 *
 * DELIBERATELY MULTI-STEP
 * Confirm, then read, then close. Rotation is irreversible and breaks every
 * existing integration the moment it happens. A single click that quietly
 * severs someone's editor setup is a bad control no matter how it is labelled.
 *
 * Pure-ASCII source, matching the other frontend modules.
 */
(function () {
  "use strict";

  function showKeyOnce(token, note) {
    var back = document.createElement("div");
    back.id = "apikey-modal-backdrop";
    back.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;" +
      "display:flex;align-items:center;justify-content:center;padding:20px";

    var box = document.createElement("div");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "Your new API key");
    box.style.cssText =
      "background:var(--surface);border:1px solid var(--border-hi);" +
      "border-radius:var(--radius);padding:20px;max-width:600px;width:100%;" +
      "font-family:var(--font-body);color:var(--text)";

    var h = document.createElement("div");
    h.style.cssText = "font-weight:600;margin-bottom:10px";
    h.textContent = "Your new API key";

    var key = document.createElement("div");
    key.id = "apikey-value";
    key.textContent = token;
    // user-select:all so one click takes the whole token. A key you have to
    // drag-select is a key someone will copy half of.
    key.style.cssText =
      "font-family:var(--font-mono);font-size:0.95rem;background:var(--surface-3);" +
      "border:1px solid var(--border);border-radius:var(--radius-sm);" +
      "padding:10px;margin:8px 0;word-break:break-all;user-select:all;" +
      "color:var(--text)";

    var warn = document.createElement("div");
    warn.className = "voice-extras-note";
    warn.style.cssText = "margin:8px 0";
    warn.textContent = note ||
      "Copy this now. It is stored hashed and cannot be shown again.";

    var extra = document.createElement("div");
    extra.className = "voice-extras-note";
    extra.textContent =
      "Your previous key stopped working the moment this one was created. " +
      "Update anything that was using it. Other profiles on this install " +
      "keep their own keys.";

    var row = document.createElement("div");
    row.className = "voice-extras";
    row.style.cssText = "margin-top:12px";

    var copy = document.createElement("button");
    copy.type = "button";
    copy.id = "apikey-copy-btn";
    copy.className = "voice-extras-btn primary";
    copy.textContent = "Copy Key";
    copy.addEventListener("click", async function () {
      try {
        if (navigator.clipboard) {
          await navigator.clipboard.writeText(token);
          copy.textContent = "Copied";
        } else {
          var r = document.createRange();
          r.selectNodeContents(key);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(r);
          copy.textContent = "Selected - press Ctrl+C";
        }
      } catch (e) {
        copy.textContent = "Copy failed - select it manually";
      }
    });

    var done = document.createElement("button");
    done.type = "button";
    done.className = "voice-extras-btn";
    done.textContent = "I have saved it";
    done.addEventListener("click", function () {
      if (back.parentNode) back.parentNode.removeChild(back);
    });

    row.appendChild(copy);
    row.appendChild(done);
    box.appendChild(h);
    box.appendChild(key);
    box.appendChild(warn);
    box.appendChild(extra);
    box.appendChild(row);
    back.appendChild(box);
    document.body.appendChild(back);

    // NOT dismissible by backdrop click or Escape, unlike the export modal.
    // Closing this by accident loses the key permanently; the user has to say
    // they have it. No onEscape is passed, so Escape does nothing here.
    //
    // This is containment, not a WCAG 2.1.2 keyboard trap: "I have saved it"
    // is reachable by Tab and activates by Enter, so a keyboard user can
    // always leave -- they just cannot leave by ACCIDENT.
    var release = window.modalA11y
      ? window.modalA11y(box, { initialFocus: "#apikey-copy-btn" })
      : null;
    done.addEventListener("click", function () { if (release) release(); });
  }

  window.rotateApiKey = async function () {
    var ok = await oracleConfirm(
      "Rotate your API key?\n\n" +
        "- A new key is issued and YOUR current one stops working immediately.\n" +
        "- Anything using your old key (Continue.dev, Claude Desktop, scripts) " +
        "will stop until you update it.\n" +
        "- Other profiles on this install are NOT affected.\n" +
        "- The new key is shown ONCE and cannot be recovered afterwards, " +
        "because only a hash of it is stored.\n\n" +
        "Do this if the key may have been exposed, or on a routine schedule.",
      { title: "Rotate API key", okLabel: "Rotate" },
    );
    if (!ok) return;

    var btn = document.getElementById("rotate-key-btn");
    if (btn) btn.disabled = true;
    if (window.setStatus) window.setStatus("Rotating API key...");
    try {
      var r = await fetch("/api/auth/rotate-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: "ROTATE" }),
      });
      if (r.status === 404) {
        // v2.14: this is no longer an owner gate. Tokens are bound to
        // profiles, so rotation is a personal action -- the 404 now means
        // there is no profile to rotate FOR: signed out, or holding a key
        // issued before ownership existed that has not been bound yet.
        if (window.setStatusError)
          window.setStatusError(
            "Could not tell which profile to rotate a key for. Sign in and " +
            "try again; if you are using a key issued before this version, " +
            "restart the app once so it can be bound to your profile.");
        return;
      }
      var d = await r.json();
      if (!d || !d.ok || !d.token) {
        if (window.setStatusError)
          window.setStatusError("Rotation failed: " + ((d && d.error) || "unknown"));
        return;
      }
      showKeyOnce(d.token, d.note);
      if (window.setStatus) window.setStatus("API key rotated.");
    } catch (e) {
      if (window.setStatusError) window.setStatusError("Rotation failed: " + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  };
})();
