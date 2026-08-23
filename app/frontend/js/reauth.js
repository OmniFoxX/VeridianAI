/* reauth.js -- prove it is still you, before data leaves or dies.
 *
 * v2.16.1. Print, Export and Burn were reachable by anyone sitting at an
 * unlocked, signed-in app. Being SIGNED IN is not the same as being allowed to
 * copy everything out of the building, or to destroy it -- a lent laptop, a
 * borrowed account, a curious child who does not understand that the thing
 * they are clicking is permanent.
 *
 * THE PROMPT COMES BEFORE THE PANEL, NOT INSIDE IT. The export panel lists
 * every section with file counts and sizes; that is a map of the target, and
 * someone who reads it has learned something even if the export itself is
 * refused a moment later. So callers await requireUnlock() BEFORE they fetch
 * anything or render anything.
 *
 * THE GATE LIVES IN THE ACTION, NOT ON THE BUTTON. printChat() is reachable
 * from the toolbar AND from the command palette; a check attached to the
 * toolbar's onclick would be walked straight around by Ctrl-K. Put it inside
 * the function every path already calls.
 *
 * A SECOND FACTOR ONLY IF THE ACCOUNT HAS ONE. The server decides, from
 * mfa.enabled_methods(); this just renders what it is told. Nobody is asked
 * for a code they cannot produce.
 *
 * Pure-ASCII source, matching the other frontend modules.
 */
(function () {
  "use strict";

  var _open = null;          // the in-flight prompt, if one is already up

  function el(tag, css, text) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text != null) e.textContent = text;      // never innerHTML for copy
    return e;
  }

  async function status() {
    try {
      return await (await fetch("/api/reauth/status")).json();
    } catch (e) {
      return null;
    }
  }

  function prompt(opts) {
    return new Promise(function (resolve) {
      var release = null;

      // ABOVE EVERY DIALOG THAT CAN TRIGGER IT. This was z-index 10000, which
      // is BELOW the profile overlays in auth.js (99999, 100000, 100002 and
      // 100003). So "Password required for this action", raised by clicking
      // Create in the profile submenu, rendered BEHIND the submenu that raised
      // it. From the person's side the button simply did nothing -- so they
      // clicked it again, and again. Todd made three accounts that way.
      //
      // A prompt that gates an action must outrank whatever surface the action
      // was started from, and the number has to leave room: pick one clearly
      // above the highest overlay rather than one greater than it, or the next
      // dialog added at 100004 quietly puts this back underneath.
      var back = el("div",
        "position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100050;" +
        "display:flex;align-items:center;justify-content:center;padding:20px");

      var box = el("div",
        "background:var(--surface);border:1px solid var(--border-hi);" +
        "border-radius:var(--radius);padding:18px 20px;max-width:420px;" +
        "width:100%;font-family:var(--font-body);color:var(--text)");
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-modal", "true");
      box.setAttribute("aria-label", "Unlock to continue");

      box.appendChild(el("div", "font-weight:600;margin-bottom:6px",
                         "Unlock to continue"));
      box.appendChild(el("div", "margin-bottom:10px;font-size:.9em;opacity:.8",
                         opts.reason));

      var pw = document.createElement("input");
      pw.type = "password";
      pw.autocomplete = "current-password";
      pw.placeholder = "Your password";
      pw.setAttribute("aria-label", "Your password");
      pw.style.cssText = "width:100%;padding:7px;margin-bottom:8px";
      box.appendChild(pw);

      var code = document.createElement("input");
      code.type = "text";
      code.inputMode = "numeric";
      code.autocomplete = "one-time-code";
      code.placeholder = "Authentication code";
      code.setAttribute("aria-label", "Authentication code");
      code.style.cssText = "width:100%;padding:7px;margin-bottom:8px";
      code.hidden = !opts.needsCode;
      box.appendChild(code);

      var useRecovery = document.createElement("label");
      useRecovery.className = "hw-toggle";
      useRecovery.style.cssText =
        "display:flex;gap:8px;align-items:center;margin-bottom:8px;font-size:.85em";
      var rc = document.createElement("input");
      rc.type = "checkbox";
      useRecovery.appendChild(rc);
      useRecovery.appendChild(el("span", null, "Use a recovery code instead"));
      useRecovery.hidden = !opts.needsCode;
      box.appendChild(useRecovery);

      var err = el("div", "font-size:.85em;min-height:1.1em;margin-bottom:8px;" +
                          "color:var(--danger, #c62828)");
      err.setAttribute("role", "alert");
      box.appendChild(err);

      box.appendChild(el("div", "font-size:.8em;opacity:.7;margin-bottom:10px",
        "Unlocks Print, Export and Burn for " + (opts.ttl || 300) / 60 +
        " minutes. It ends when you sign out."));

      var row = el("div", "display:flex;gap:8px;justify-content:flex-end");
      var cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "voice-extras-btn";
      cancel.textContent = "Cancel";
      var go = document.createElement("button");
      go.type = "button";
      go.className = "voice-extras-btn";
      go.textContent = "Unlock";
      row.appendChild(cancel);
      row.appendChild(go);
      box.appendChild(row);

      back.appendChild(box);
      document.body.appendChild(back);

      function done(result) {
        if (back.parentNode) back.parentNode.removeChild(back);
        if (release) { release(); release = null; }
        _open = null;
        resolve(result);
      }

      async function submit() {
        err.textContent = "";
        go.disabled = true;
        go.textContent = "Checking...";
        var body = { password: pw.value };
        if (!code.hidden && code.value) {
          body.code = code.value;
          body.method = rc.checked ? "recovery" : "totp";
        }
        var d;
        try {
          d = await (await fetch("/api/reauth", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          })).json();
        } catch (e) {
          d = null;
        }
        go.disabled = false;
        go.textContent = "Unlock";
        if (d && d.ok) { done(true); return; }
        if (d && d.needs_code) {
          // The server may know about a second factor the status call did not
          // surface. Reveal the field rather than failing at the person.
          code.hidden = false;
          useRecovery.hidden = false;
          code.focus();
        }
        err.textContent = (d && d.error) || "Could not verify. Try again.";
        if (d && d.retry_in) {
          // A growing delay, not a lockout -- count it down so the wait is a
          // known quantity rather than a dead button.
          var left = d.retry_in;
          go.disabled = true;
          var tick = setInterval(function () {
            left -= 1;
            if (left <= 0) {
              clearInterval(tick);
              go.disabled = false;
              err.textContent = "";
            } else {
              err.textContent = "Too many attempts. Try again in " + left + "s.";
            }
          }, 1000);
        }
        pw.select();
      }

      go.addEventListener("click", submit);
      cancel.addEventListener("click", function () { done(false); });
      back.addEventListener("click", function (e) {
        if (e.target === back) done(false);
      });
      box.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); submit(); }
      });
      if (window.modalA11y) {
        release = window.modalA11y(box, {
          onEscape: function () { done(false); },
        });
      }
      setTimeout(function () { pw.focus(); }, 0);
    });
  }

  /* Returns {ok, joined}. `joined` is true only when this caller piggybacked
   * on a prompt somebody else had already raised -- which is the one case an
   * automatic retry must NOT treat as its own authorisation. See the fetch
   * interceptor below.
   *
   * The second `_open` test is not redundant. `await status()` yields, so two
   * clicks a few milliseconds apart can BOTH pass the first test, and then
   * both open a prompt and both replay. Re-testing after the await, with no
   * await between the test and the assignment, makes claiming the prompt
   * atomic: exactly one caller owns it and the rest join. */
  async function unlockInternal(reason) {
    if (_open) return { ok: await _open, joined: true };
    var st = await status();
    if (!st || !st.ok) {
      // Fail OPEN, deliberately. This gate protects against someone at the
      // keyboard, and the server enforces the export and burn endpoints on
      // its own regardless of what this file decides. Failing closed here
      // would mean a hiccup in a status call locks a person out of printing
      // their own notes, while adding nothing -- the real boundary is the
      // endpoint, and it is still there.
      return { ok: true, joined: false };
    }
    if (!st.required || st.elevated) return { ok: true, joined: false };
    if (_open) return { ok: await _open, joined: true };   // lost the race
    _open = prompt({
      reason: reason || "This action needs your password.",
      needsCode: !!st.needs_code,
      ttl: st.ttl,
    });
    return { ok: await _open, joined: false };
  }

  /* The only entry point for callers outside this file. Resolves true when the
   * caller may proceed.
   *
   * Resolves TRUE without prompting when no account is configured -- with
   * multi-user off there is no password to ask for, and locking someone out
   * of their own data on a single-user install would be a cost with no
   * security to show for it.
   *
   * Deliberately keeps the plain-boolean contract. `joined` is a fact about
   * the AUTOMATIC retry, not about whether the person is allowed to act, and
   * an explicit caller who asked for an unlock and got one should proceed. */
  window.requireUnlock = async function (reason) {
    return (await unlockInternal(reason)).ok;
  };

  window.reauthDrop = async function () {
    try {
      await fetch("/api/reauth/drop", { method: "POST" });
    } catch (e) { /* nothing to do; it expires on its own */ }
  };

  /* ------------------------------------------------------------------------
   * v2.16.1: one interception point for every gated endpoint.
   *
   * Eleven auth endpoints now refuse without an unlock -- turning a second
   * factor off, minting an API token, resetting somebody else's password, and
   * so on. Wiring a prompt into each of their callers by hand would be eleven
   * chances to miss one, and the twelfth endpoint added next year would arrive
   * unwired. That is the same "right code, wrong coverage" shape that has
   * bitten this project six times; the lesson each time was to put the check
   * where every path already goes.
   *
   * DELIBERATELY NARROW. It acts only on a 401 whose JSON says needs_reauth,
   * and only when the request body is replayable. It never touches streaming
   * responses, never inspects a body it does not have to, and skips
   * /api/reauth itself so a refusal cannot recurse.
   *
   * Retries ONCE. If the second attempt also refuses, that is a real answer
   * and the caller should see it rather than being asked again in a loop.
   * ---------------------------------------------------------------------- */
  var _native = window.fetch.bind(window);

  window.fetch = async function (input, init) {
    var res = await _native(input, init);
    try {
      if (res.status !== 401) return res;
      var url = (typeof input === "string" ? input : (input && input.url) || "");
      if (url.indexOf("/api/") === -1 || url.indexOf("/api/reauth") !== -1) {
        return res;
      }
      // Body must be replayable. A string is; a stream or FormData is not,
      // and silently re-sending something else would be worse than the error.
      var body = init && init.body;
      if (body != null && typeof body !== "string") return res;

      var data = null;
      try { data = await res.clone().json(); } catch (e) { return res; }
      var needs = data && (data.needs_reauth ||
                           (data.detail && data.detail.needs_reauth));
      if (!needs) return res;

      var msg = (data.detail && data.detail.error) || data.error ||
                "This action needs your password.";
      if (!window.requireUnlock) return res;

      // ONE UNLOCK REPLAYS ONE REQUEST -- the one that asked for it.
      //
      // requireUnlock already refuses to stack two prompts: a second call
      // joins the first one's promise. But EVERY joiner was then replayed on
      // the single unlock, so three clicks on a Create button became three
      // real accounts -- which is exactly what happened to Todd, because the
      // prompt was rendering behind the submenu (fixed above) and the button
      // looked dead. The z-index was why he clicked three times; this is why
      // three clicks became three accounts, and both had to be fixed. Only the
      // first is a UI bug; a gated request replaying on somebody else's
      // authorisation is a correctness one, and it would have survived.
      //
      // So: only the request that RAISED the prompt is replayed. Anything that
      // arrived while a prompt was already up gets its 401 back and its caller
      // reports the refusal. Fails CLOSED -- the failure mode is an action the
      // person has to click again once, never one that happens twice.
      var r = await unlockInternal(msg);
      if (!r.ok || r.joined) return res;
      return await _native(input, init);
    } catch (e) {
      return res;               // never let the guard break the request
    }
  };
})();
