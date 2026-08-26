/* report.js -- report AI-generated content that went wrong.
 *
 * WHY THIS EXISTS
 *
 * The Microsoft Store rejected v2.16.1 for one reason: VeridianAI documented an
 * address to report problems and gave no MECHANISM inside the app. A way
 * written in a manual is not a way.
 *
 * WHAT THE PERSON SEES, AND WHY IN THIS ORDER
 *
 * 1. A loud warning that this is about to put conversation text into a file
 *    they will send to a real person. Said first, before any control, because
 *    consent given after the fact is not consent.
 * 2. THE ACTUAL TEXT that would be sent, in an editable box. Not a summary,
 *    not a promise -- the words. They can cut anything out of it first. This
 *    app is used with health information in it; "trust us about what we
 *    included" is not good enough.
 * 3. Tickboxes for everything else, all UNTICKED. The prompt, earlier turns
 *    and the reasoning trace are their own words, so each one is a decision
 *    they make rather than one they discover.
 * 4. Only then, the button that writes the file.
 *
 * NOTHING IS UPLOADED. The app writes one readable file into their downloads
 * folder and shows them the path. Sending it is a separate act they perform
 * with the file in front of them. An automatic reporter would be a channel out
 * of the building that they did not open.
 *
 * WORKS WITH NO MAIL CLIENT. The address and the file path are shown as
 * selectable text with copy buttons, and the folder can be opened. The email
 * draft is a convenience on top. A reviewer on a clean VM with no mail client
 * still sees a mechanism that plainly works -- which matters, because the
 * alternative is a second rejection for the same finding.
 *
 * Pure-ASCII source, matching the other frontend modules.
 */
(function () {
  "use strict";

  var OVERLAY_ID = "report-overlay";

  function V(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement)
        .getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) {
      return fallback;
    }
  }

  function el(tag, css, text) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text != null) e.textContent = text;   // never innerHTML for content
    return e;
  }

  /* The most recent assistant turn, and the user turn that produced it.
   *
   * chat.js declares `messages` as a top-level `let` in a classic script, so
   * it is a global LEXICAL binding and NOT a property of window -- reading
   * window.messages returns undefined and would silently hand every reporter
   * an empty box. Referenced by bare identifier instead, guarded by typeof so
   * this file still loads if chat.js ever stops defining it.
   *
   * Falls back to empty rather than guessing. An empty box the person can
   * paste into is honest; the wrong message quoted back at them is not. */
  function lastExchange() {
    var out = { flagged: "", prompt: "", reasoning: "", context: [] };
    var msgs = null;
    try {
      if (typeof messages !== "undefined" && messages && messages.length) {
        msgs = messages;
      }
    } catch (e) { msgs = null; }
    if (!msgs) return out;
    for (var i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i] && msgs[i].role === "assistant") {
        out.flagged = String(msgs[i].content || "");
        out.reasoning = String(msgs[i].reasoning || "");
        for (var j = i - 1; j >= 0; j--) {
          if (msgs[j] && msgs[j].role === "user") {
            out.prompt = String(msgs[j].content || "");
            break;
          }
        }
        out.context = msgs.slice(Math.max(0, i - 6), i).map(function (m) {
          return { role: String(m.role || ""), content: String(m.content || "") };
        });
        break;
      }
    }
    return out;
  }

  function currentModel() {
    try {
      var s = document.getElementById("model-select");
      if (s && s.selectedIndex >= 0 && s.options[s.selectedIndex]) {
        return s.options[s.selectedIndex].textContent.trim();
      }
    } catch (e) {}
    return "";
  }

  function currentBackend() {
    try {
      var s = document.getElementById("setting-backend");
      if (s && s.selectedIndex >= 0 && s.options[s.selectedIndex]) {
        return s.options[s.selectedIndex].textContent.trim();
      }
    } catch (e) {}
    return "";
  }

  function copyText(value, btn) {
    var done = function () {
      if (!btn) return;
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1400);
    };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(done, function () {});
        return;
      }
    } catch (e) {}
    try {
      var ta = document.createElement("textarea");
      ta.value = value;
      ta.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      done();
    } catch (e2) {}
  }

  window.openReportDialog = function () {
    if (document.getElementById(OVERLAY_ID)) return;   // never stack two
    var ex = lastExchange();
    var release = null;

    var back = el("div",
      "position:fixed;inset:0;background:rgba(2,5,12,0.72);z-index:100040;" +
      "display:flex;align-items:center;justify-content:center;padding:20px;" +
      "overflow:auto");
    back.id = OVERLAY_ID;

    var box = el("div",
      "width:min(94vw,640px);max-height:90vh;overflow:auto;padding:20px 22px;" +
      "border-radius:14px;background:" + V("--surface", "#0a1020") +
      ";border:1px solid " + V("--border-hi", "#3a4a6a") +
      ";font-family:var(--font-body);color:" + V("--text", "#e2e8f8"));
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-labelledby", "report-title");

    var title = el("div",
      "font-size:1.1em;font-weight:700;margin-bottom:10px",
      "⚠️ Report AI-generated content");
    title.id = "report-title";
    box.appendChild(title);

    /* THE WARNING, FIRST AND LOUD. Store reviewers look for exactly this, and
     * so should anybody about to mail their own conversation to a stranger. */
    var warn = el("div",
      "border:1px solid " + V("--warning", "#ffc14d") + ";border-radius:10px;" +
      "padding:11px 13px;margin-bottom:14px;background:rgba(201,162,39,0.10);" +
      "font-size:.93em;line-height:1.5");
    warn.appendChild(el("div", "font-weight:700;margin-bottom:4px",
      "This puts conversation text into a file you will send to a person."));
    warn.appendChild(el("div", null,
      "VeridianAI does not upload anything. It writes one readable file into " +
      "your downloads folder and shows you where it is. You choose whether to " +
      "send it, and to whom. Read what is below before you do — if any of " +
      "it is confidential, edit it out now."));
    box.appendChild(warn);

    box.appendChild(el("div", "font-weight:600;margin-bottom:4px",
      "What went wrong?"));
    var desc = document.createElement("textarea");
    desc.id = "report-desc";
    desc.rows = 3;
    desc.className = "report-textarea";
    desc.placeholder =
      "For example: this reply gave unsafe medical advice, or invented a " +
      "citation, or was offensive.";
    desc.style.cssText = "margin-bottom:14px";
    box.appendChild(desc);

    box.appendChild(el("div", "font-weight:600;margin-bottom:4px",
      "The reply being reported"));
    box.appendChild(el("div",
      "font-size:.85em;opacity:.8;margin-bottom:4px",
      "This is exactly what will be written to the file. Edit or delete any " +
      "part of it."));
    var flagged = document.createElement("textarea");
    flagged.id = "report-flagged";
    flagged.rows = 8;
    flagged.className = "report-textarea is-mono";
    flagged.value = ex.flagged;
    flagged.placeholder =
      "No recent reply was found. Paste the content you want to report here.";
    flagged.style.cssText = "margin-bottom:14px";
    box.appendChild(flagged);

    /* OPT-IN, ALL OF IT. Each of these is the person's own words. */
    box.appendChild(el("div", "font-weight:600;margin-bottom:2px",
      "Include anything else? (all optional, all off)"));
    box.appendChild(el("div", "font-size:.85em;opacity:.8;margin-bottom:8px",
      "Only tick what you are willing to send. Nothing here is required."));

    var OPTS = [
      ["prompt", "The prompt that produced it",
       "Usually needed to reproduce the problem. These are your own words.",
       !!ex.prompt],
      ["context", "The few turns before it",
       "More of this conversation, for context.", !!(ex.context || []).length],
      ["reasoning", "The model's reasoning trace",
       "The model's own working, including steps it discarded.",
       !!ex.reasoning],
      ["environment_detail", "Extra technical detail",
       "CPU architecture and Python version. No names, paths or addresses.",
       true],
    ];
    var boxes = {};
    OPTS.forEach(function (o) {
      var key = o[0], label = o[1], hint = o[2], available = o[3];
      var row = el("label",
        "display:flex;gap:9px;align-items:flex-start;margin-bottom:7px;" +
        "font-size:.9em;cursor:pointer" + (available ? "" : ";opacity:.45"));
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.id = "report-inc-" + key;
      cb.checked = false;                 // never pre-ticked
      cb.disabled = !available;
      cb.style.cssText = "margin-top:3px";
      boxes[key] = cb;
      var txt = el("span");
      txt.appendChild(el("div", "font-weight:600", label +
        (available ? "" : " (not available)")));
      txt.appendChild(el("div", "opacity:.75;font-size:.92em", hint));
      row.appendChild(cb);
      row.appendChild(txt);
      box.appendChild(row);
    });

    /* What is never included, stated plainly. A list of what you are NOT
     * taking is worth more than a promise to be careful. */
    var never = el("div",
      "margin:12px 0 14px;padding:9px 11px;border-radius:8px;font-size:.85em;" +
      "line-height:1.5;background:" + V("--surface-2", "#0d1526") +
      ";border:1px solid " + V("--border", "#2a3a5a"));
    never.appendChild(el("div", "font-weight:600;margin-bottom:3px",
      "Never included, whatever you tick"));
    never.appendChild(el("div", null,
      "Your username, machine name, IP address or install location. Other " +
      "conversations, archives, uploads or documents. Any other profile's " +
      "data. Passwords, keys or API tokens."));
    box.appendChild(never);

    var err = el("div");
    err.className = "report-err";
    err.setAttribute("role", "alert");
    box.appendChild(err);

    var row = el("div",
      "display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap");
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "voice-extras-btn";
    cancel.textContent = "Cancel";
    var go = document.createElement("button");
    go.type = "button";
    go.className = "voice-extras-btn";
    go.textContent = "Create the report file";
    row.appendChild(cancel);
    row.appendChild(go);
    box.appendChild(row);

    back.appendChild(box);
    document.body.appendChild(back);

    function close() {
      if (back.parentNode) back.parentNode.removeChild(back);
      if (release) { release(); release = null; }
    }

    cancel.addEventListener("click", close);
    back.addEventListener("click", function (e) {
      if (e.target === back) close();
    });
    if (window.modalA11y) {
      release = window.modalA11y(box, { onEscape: close });
    }
    setTimeout(function () { desc.focus(); }, 0);

    go.addEventListener("click", async function () {
      err.textContent = "";
      if (!flagged.value.trim() && !desc.value.trim()) {
        err.textContent =
          "Describe the problem, or paste the content you are reporting.";
        return;
      }
      go.disabled = true;
      go.textContent = "Writing...";
      var include = {};
      Object.keys(boxes).forEach(function (k) {
        if (boxes[k].checked) include[k] = true;
      });
      var d = null;
      try {
        var r = await fetch("/api/report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            flagged: flagged.value,
            description: desc.value,
            model: currentModel(),
            backend: currentBackend(),
            include: include,
            // Sent only when the matching box is ticked. The server checks
            // again, but there is no reason to put text on the wire that
            // nobody asked to include.
            prompt: include.prompt ? ex.prompt : "",
            reasoning: include.reasoning ? ex.reasoning : "",
            context_turns: include.context ? ex.context : null,
          }),
        });
        d = await r.json();
      } catch (e) {
        d = null;
      }
      go.disabled = false;
      go.textContent = "Create the report file";
      if (!d || !d.ok) {
        err.textContent = (d && d.error) ||
          "Could not write the report file. Try again.";
        return;
      }
      close();
      showResult(d);
    });
  };

  /* The file exists. Now make sending it as easy as possible WITHOUT
   * depending on anything being installed. */
  function showResult(d) {
    var back = el("div",
      "position:fixed;inset:0;background:rgba(2,5,12,0.72);z-index:100040;" +
      "display:flex;align-items:center;justify-content:center;padding:20px");
    back.id = OVERLAY_ID;
    var box = el("div",
      "width:min(94vw,620px);max-height:90vh;overflow:auto;padding:20px 22px;" +
      "border-radius:14px;background:" + V("--surface", "#0a1020") +
      ";border:1px solid " + V("--border-hi", "#3a4a6a") +
      ";font-family:var(--font-body);color:" + V("--text", "#e2e8f8"));
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "Report file created");

    box.appendChild(el("div", "font-size:1.05em;font-weight:700;margin-bottom:8px",
      "Report file created"));
    box.appendChild(el("div", "font-size:.92em;line-height:1.55;margin-bottom:12px",
      "Nothing has been sent. The file is on your computer and will stay " +
      "there until you send it. Open it and read it first if you like."));

    function field(label, value, tipText) {
      var wrap = el("div", "margin-bottom:12px");
      wrap.appendChild(el("div", "font-weight:600;font-size:.88em;margin-bottom:3px",
        label));
      var line = el("div",
        "display:flex;gap:8px;align-items:center;flex-wrap:wrap");
      var v = el("code", null, value);
      v.className = "report-value";
      var cp = document.createElement("button");
      cp.type = "button";
      cp.className = "voice-extras-btn";
      cp.textContent = "Copy";
      cp.addEventListener("click", function () { copyText(value, cp); });
      line.appendChild(v);
      line.appendChild(cp);
      wrap.appendChild(line);
      if (tipText) {
        wrap.appendChild(el("div", "font-size:.83em;opacity:.75;margin-top:4px",
          tipText));
      }
      return wrap;
    }

    box.appendChild(field("Send it to", d.support_email || "",
      "Attach the file to an email. That is the whole process."));
    box.appendChild(field("The file", d.path || d.filename || "",
      "Included: " + ((d.included || []).join(", ") || "nothing")));

    var row = el("div",
      "display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;margin-top:6px");

    // Convenience only. Everything above already works without it, which is
    // the point -- on a machine with no mail client this button does nothing
    // and the person has lost nothing.
    var mail = document.createElement("button");
    mail.type = "button";
    mail.className = "voice-extras-btn";
    mail.textContent = "Open an email draft";
    mail.addEventListener("click", function () {
      var subject = encodeURIComponent(d.subject || "VeridianAI content report");
      var body = encodeURIComponent(
        "Please attach the report file before sending.\n\n" +
        (d.path || d.filename || "") + "\n\n" +
        "(VeridianAI wrote that file locally. It is not attached " +
        "automatically.)\n");
      try {
        window.open("mailto:" + (d.support_email || "") +
                    "?subject=" + subject + "&body=" + body, "_blank");
      } catch (e) {}
    });

    var folder = document.createElement("button");
    folder.type = "button";
    folder.className = "voice-extras-btn";
    folder.textContent = "Open my data folder";
    folder.addEventListener("click", function () {
      try {
        if (window.electronAPI && window.electronAPI.send) {
          window.electronAPI.send("open-data-folder");
        }
      } catch (e) {}
    });

    var done = document.createElement("button");
    done.type = "button";
    done.className = "voice-extras-btn";
    done.textContent = "Done";

    row.appendChild(folder);
    row.appendChild(mail);
    row.appendChild(done);
    box.appendChild(row);
    back.appendChild(box);
    document.body.appendChild(back);

    var release = null;
    function close2() {
      if (back.parentNode) back.parentNode.removeChild(back);
      if (release) { release(); release = null; }
    }
    done.addEventListener("click", close2);
    back.addEventListener("click", function (e) {
      if (e.target === back) close2();
    });
    if (window.modalA11y) {
      release = window.modalA11y(box, { onEscape: close2 });
    }
    setTimeout(function () { done.focus(); }, 0);
  }
})();
