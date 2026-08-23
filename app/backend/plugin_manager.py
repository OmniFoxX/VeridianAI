"""
VeridianAI Plugin Manager
Loads JSON plugin definitions from the /plugins directory.
Each plugin can define pre/post processing hooks and metadata.

WHERE THE ON/OFF STATE LIVES, AND WHY IT MOVED (v2.16.2)
--------------------------------------------------------
Todd, from the outside: "the toggles in the 'Settings' tab persist states
between restarts of VeridianAI, but the toggles in the 'Plugins' tab do not..
which I don't understand."

The difference was the destination. Settings writes config.json, which follows
STATE_DIR into sage_data. Plugin state was written back into the plugin's own
JSON -- inside the INSTALL directory. On a Store install that is
C:\\Program Files\\WindowsApps, which is read-only; main.py's own docs-publish
routine already says so in as many words. The write raised, the handler printed
to a console nobody is looking at, and toggle_plugin returned {"status": "ok"}.
The UI was told it worked. The next restart said otherwise.

This is the same rule config.json was moved under in v2.13, stated in
state_paths and forgotten here: STATE THAT IS WRITTEN TO MUST FOLLOW STATE_DIR.
The shipped plugins/*.json are package content -- read-only defaults describing
what a plugin IS. Whether the user has it switched on is a fact about this
INSTALL, so it lives in sage_data/plugin_state.json, exactly as ui_prefs.json
holds the machine-scoped UI preferences next to it.

Two consequences worth stating, both wanted:
  * shipping a new plugin enabled (ai-disclosure) still reaches every install,
    because the overlay only records the ids somebody has actually toggled.
  * the plugins directory stays byte-identical to what was signed, so nothing
    here can arm the build-integrity tamper switch.

AND IT NO LONGER CLAIMS SUCCESS IT DID NOT HAVE. If the overlay cannot be
written, the in-memory flag is put back and the caller is told. A toggle that
reports "ok" for a write that failed is worse than one that refuses: the person
believes a plugin is off for the rest of the session.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List


class PluginManager:

    def __init__(self, plugins_dir: Path, state_file=None):
        self.plugins_dir = plugins_dir
        self.state_file = Path(state_file) if state_file else self._default_state_file()
        self._plugins: Dict[str, Dict] = {}
        self._load_all()

    @staticmethod
    def _default_state_file() -> Path:
        """sage_data/plugin_state.json -- resolved the same way ui_prefs does,
        so the two land side by side and move together if DATA_DIR ever does."""
        try:
            from config import DATA_DIR
            return Path(DATA_DIR) / "plugin_state.json"
        except Exception:
            # backend/ -> project root -> sibling sage_data (the real layout)
            return (Path(__file__).resolve().parent.parent.parent
                    / "sage_data" / "plugin_state.json")

    def _load_state(self) -> Dict[str, bool]:
        """The overlay: {plugin_id: enabled}. Never raises -- an unreadable or
        absent overlay means 'nobody has changed anything yet', which is
        exactly the shipped defaults."""
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {str(k): bool(v) for k, v in data.items()}
        except Exception:
            return {}

    def _load_all(self):
        if not self.plugins_dir.exists():
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            return
        state = self._load_state()
        for path in self.plugins_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    plugin = json.load(f)
                plugin_id = plugin.get("id", path.stem)
                plugin.setdefault("enabled", True)
                plugin.setdefault("file", str(path))
                # The overlay wins where it has an opinion, and only there.
                # An id the user has never toggled keeps the shipped default,
                # so a newly bundled plugin arrives in the state it shipped in
                # rather than inheriting a stale "off" from an old file.
                if plugin_id in state:
                    plugin["enabled"] = state[plugin_id]
                self._plugins[plugin_id] = plugin
            except Exception as e:
                print(f"[PluginManager] Failed to load {path.name}: {e}")

    def _save_plugin(self, plugin_id: str) -> bool:
        """Write the enabled-state overlay. Returns whether it actually landed.

        The whole overlay is rewritten rather than one key patched, because it
        is a handful of booleans and a read-modify-write of a tiny file is
        easier to reason about than a partial update that can half-apply.

        Entries for plugins that no longer exist are dropped on the way past --
        a plugin deleted from the install (example_plugin.json, this release)
        should not leave a preference behind to confuse the next reader.
        """
        plugin = self._plugins.get(plugin_id)
        if not plugin:
            return False
        try:
            overlay = self._load_state()
            overlay = {k: v for k, v in overlay.items() if k in self._plugins}
            overlay[plugin_id] = bool(plugin.get("enabled", True))
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-replace: a crash mid-write must not leave a truncated
            # overlay, which _load_state would read as "no opinions" and
            # silently reset every toggle the user has ever set.
            tmp = self.state_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(overlay, indent=2, ensure_ascii=False),
                encoding="utf-8")
            tmp.replace(self.state_file)
            return True
        except Exception as e:
            print(f"[PluginManager] Failed to save {plugin_id}: {e}")
            return False

    def list_plugins(self) -> List[Dict]:
        return [
            {
                "id":          pid,
                "name":        p.get("name", pid),
                "description": p.get("description", ""),
                "version":     p.get("version", ""),
                "enabled":     p.get("enabled", True),
                "author":      p.get("author", "VeridianAI"),
                "hooks":       list(p.get("hooks", {}).keys()),
            }
            for pid, p in self._plugins.items()
        ]

    def toggle_plugin(self, plugin_id: str) -> Dict:
        """Flip a plugin, and report the state it is ACTUALLY in.

        "ok" now means the change reached disk. When the write fails the
        in-memory flag is put back, so what this object reports, what the UI
        shows and what survives a restart are the same answer -- which is the
        thing that was broken.
        """
        if plugin_id not in self._plugins:
            return {"status": "error", "message": f"Plugin '{plugin_id}' not found"}
        was = self._plugins[plugin_id].get("enabled", True)
        self._plugins[plugin_id]["enabled"] = not was
        if not self._save_plugin(plugin_id):
            self._plugins[plugin_id]["enabled"] = was      # nothing happened
            return {
                "status": "error",
                "id": plugin_id,
                "enabled": was,
                "message": ("Could not save the plugin setting. It has been "
                            "left as it was."),
            }
        return {
            "status":  "ok",
            "id":      plugin_id,
            "enabled": self._plugins[plugin_id]["enabled"],
        }

    def preprocess(self, messages: List[Dict]) -> List[Dict]:
        """Run all enabled plugins' pre-chat hooks."""
        for plugin in self._plugins.values():
            if not plugin.get("enabled", True):
                continue
            hooks = plugin.get("hooks", {})
            if "prepend_system" in hooks:
                extra = hooks["prepend_system"]
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = extra + "\n" + messages[0]["content"]
                else:
                    messages.insert(0, {"role": "system", "content": extra})
        return messages

    def postprocess(self, response: str) -> str:
        """Run all enabled plugins' post-response hooks.

        APPENDING IS IDEMPOTENT, and that is not defensive tidiness -- it is
        required the moment a footer ships enabled by default.

        The appended text is saved into chat memory as part of the assistant
        message, so it comes BACK to the model as its own previous turn on the
        next request. Models imitate the shape of their own prior output; one
        that has seen "AI-generated by Toga" ending its last three replies
        starts writing it itself. Appending unconditionally then yields the
        disclosure twice, stacked -- a bug in the very line that exists to be
        trusted. Todd saw exactly that, "after a few turns of having it on",
        which is the imitation taking hold.

        THE COMPARISON IGNORES WHITESPACE, and that is the whole point. The
        first version used response.rstrip().endswith(footer.strip()) and would
        NOT have stopped what he saw: a model reproducing the footer writes the
        same words, not the same bytes. One blank line more between the rule
        and the sentence and an exact match sees no duplicate at all. Comparing
        on collapsed whitespace is what makes this hold against a reproduction
        rather than a copy.

        It also covers the plainer case of two code paths calling postprocess
        on the same string.

        Note the footer STAYS in the saved history rather than being stripped
        on the way back to the model, which would remove the imitation at its
        source. Deliberate: chat memory is what Export and Print read, and a
        transcript that leaves the app is exactly where the disclosure has to
        survive. Keeping it costs an imitation risk, which this handles.
        """
        for plugin in self._plugins.values():
            if not plugin.get("enabled", True):
                continue
            hooks = plugin.get("hooks", {})
            footer = hooks.get("append_footer")
            if not footer:
                continue
            if self._already_ends_with(response, footer):
                continue
            response += "\n" + footer
        return response

    @staticmethod
    def _already_ends_with(response: str, footer: str) -> bool:
        """True when `response` already closes with `footer`, give or take the
        whitespace. See postprocess's docstring for why byte equality is the
        wrong test here."""
        collapse = re.compile(r"\s+")
        want = collapse.sub(" ", str(footer)).strip()
        if not want:
            return False
        # Only the tail is normalised -- a long reply need not be rewritten in
        # full to answer a question about its last line.
        tail = response[-(len(footer) + 200):] if response else ""
        return collapse.sub(" ", tail).strip().endswith(want)
