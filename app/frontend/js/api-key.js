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

  // --- Scoped keys ---------------------------------------------------
  // Rotate replaces the ONE default key -- right for the common case. This is
  // for the other one: a narrow key for a specific integration, so a tool that
  // only needs to read cannot write. The scope machinery has been in the
  // backend since v2.2 and nothing ever minted anything but full access.

  function esc(t) {
    var d = document.createElement("div");
    d.textContent = String(t == null ? "" : t);
    return d.innerHTML;
  }

  /* The list collapsed in v2.16.2 (it has no upper bound and was pushing the
   * rest of Settings off the panel), so the COUNT has to be maintained
   * separately -- it is the part that stays visible, and these are live
   * credentials. Every return path below sets it, including the failures:
   * a stale "4" left over from the last successful load, sitting above a
   * collapsed list that could not actually be read, is worse than no number.
   *
   * Fails to a visible "?" rather than to 0. Zero is a claim ("you have no
   * keys"); the truth when the read fails is that we do not know. */
  function setKeyCount(n) {
    var c = document.getElementById("apikeys-count");
    if (c) c.textContent = String(n);
    var det = document.getElementById("apikeys-details");
    // Nothing to expand into: don't offer a disclosure that opens onto a
    // single line of explanatory text.
    if (det && typeof n === "number" && n === 0) det.open = false;
  }

  window.loadApiKeys = async function () {
    var box = document.getElementById("apikeys-list");
    var sel = document.getElementById("apikey-preset");
    if (!box) return;
    box.textContent = "Loading...";
    try {
      var d = await (await fetch("/api/auth/keys")).json();
      if (!d.ok) {
        box.textContent = "Could not read your keys.";
        setKeyCount("?");
        return;
      }

      if (sel && !sel.options.length) {
        (d.presets || []).forEach(function (p) {
          var o = document.createElement("option");
          o.value = p.id;
          o.textContent = p.label;
          sel.appendChild(o);
        });
      }

      var keys = d.keys || [];
      setKeyCount(keys.length);
      if (!keys.length) {
        box.innerHTML = '<div class="voice-extras-note">No additional keys. ' +
          'Your default key is the one the Rotate button manages.</div>';
        return;
      }
      box.innerHTML = "";
      keys.forEach(function (k) {
        var row = document.createElement("div");
        row.className = "hw-toggle";
        row.style.cssText =
          "display:flex;gap:8px;align-items:center;justify-content:space-between;" +
          "margin:4px 0;padding:6px 8px;border:1px solid var(--border);" +
          "border-radius:var(--radius-sm)";
        // Prefix, last-used and scope are the three things you need to decide
        // whether a key is still wanted. "never used" is the useful signal.
        row.innerHTML =
          '<span class="hw-toggle-label" style="flex:1">' +
            "<strong>" + esc(k.label) + "</strong>" +
            '<span style="color:var(--text-muted)"> &mdash; ' +
              esc(k.preset ? k.preset.replace("_", " ") : (k.scopes || []).join(", ")) +
              " &middot; " + esc(k.prefix) + "&hellip;" +
              " &middot; " + (k.last_used ? "last used " + esc(k.last_used)
                                          : "never used") +
            "</span>" +
          "</span>";
        var del = document.createElement("button");
        del.type = "button";
        del.className = "voice-extras-btn";
        del.textContent = "Revoke";
        del.setAttribute("data-tip", "Revoke this key immediately");
        del.addEventListener("click", function () { revokeKey(k); });
        row.appendChild(del);
        box.appendChild(row);
      });
    } catch (e) {
      box.textContent = "Could not read your keys.";
      setKeyCount("?");
    }
  };

  async function revokeKey(k) {
    var ok = await oracleConfirm(
      'Revoke the key "' + k.label + '"?\n\n' +
        "- It stops working immediately.\n" +
        "- Anything using it will start failing until you issue a new one.\n" +
        "- Your other keys are unaffected.",
      { title: "Revoke key", okLabel: "Revoke" },
    );
    if (!ok) return;
    try {
      var r = await fetch("/api/auth/keys/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prefix: k.prefix }),
      });
      var d = await r.json();
      if (d.ok) {
        if (window.setStatus) window.setStatus("Key revoked.");
        window.loadApiKeys();
      } else if (window.setStatusError) {
        window.setStatusError("Revoke failed: " + (d.error || "unknown"));
      }
    } catch (e) {
      if (window.setStatusError) window.setStatusError("Revoke failed: " + e.message);
    }
  }

  window.createApiKey = async function () {
    var label = (document.getElementById("apikey-label") || {}).value || "";
    var preset = (document.getElementById("apikey-preset") || {}).value || "";
    label = label.trim();
    if (!label) {
      if (window.setStatusError)
        window.setStatusError("Give the key a name, so you can tell later what "
                              + "it was for and revoke the right one.");
      return;
    }
    var btn = document.getElementById("apikey-create-btn");
    if (btn) btn.disabled = true;
    try {
      var r = await fetch("/api/auth/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: label, preset: preset }),
      });
      var d = await r.json();
      if (!d || !d.ok || !d.token) {
        if (window.setStatusError)
          window.setStatusError("Could not create the key: " +
                                ((d && d.error) || "unknown"));
        return;
      }
      showKeyOnce(d.token, d.note);
      var lbl = document.getElementById("apikey-label");
      if (lbl) lbl.value = "";
      // Open the list on a create. You just issued a credential; the result of
      // that action should not land inside a collapsed section where the only
      // feedback is a number quietly going up by one.
      var det = document.getElementById("apikeys-details");
      if (det) det.open = true;
      window.loadApiKeys();
    } catch (e) {
      if (window.setStatusError)
        window.setStatusError("Could not create the key: " + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  };

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
