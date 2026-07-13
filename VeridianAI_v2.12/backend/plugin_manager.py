"""
OracleAI Plugin Manager
Loads JSON plugin definitions from the /plugins directory.
Each plugin can define pre/post processing hooks and metadata.
"""

import json
from pathlib import Path
from typing import Any, Dict, List


class PluginManager:

    def __init__(self, plugins_dir: Path):
        self.plugins_dir = plugins_dir
        self._plugins: Dict[str, Dict] = {}
        self._load_all()

    def _load_all(self):
        if not self.plugins_dir.exists():
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            return
        for path in self.plugins_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    plugin = json.load(f)
                plugin_id = plugin.get("id", path.stem)
                plugin.setdefault("enabled", True)
                plugin.setdefault("file", str(path))
                self._plugins[plugin_id] = plugin
            except Exception as e:
                print(f"[PluginManager] Failed to load {path.name}: {e}")
                
    def _save_plugin(self, plugin_id: str):
        """Persist plugin enabled state back to its JSON file."""
        plugin = self._plugins.get(plugin_id)
        if not plugin or "file" not in plugin:
            return
        try:
            path = Path(plugin["file"])
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["enabled"] = plugin["enabled"]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[PluginManager] Failed to save {plugin_id}: {e}")

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
        if plugin_id not in self._plugins:
            return {"status": "error", "message": f"Plugin '{plugin_id}' not found"}
        self._plugins[plugin_id] ["enabled"] = not self._plugins[plugin_id].get("enabled", True)
        self._save_plugin(plugin_id)  # ← add this line
        return {
            "status":  "ok",
            "id":      plugin_id,
            "enabled": self._plugins[plugin_id] ["enabled"],
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
        """Run all enabled plugins' post-response hooks."""
        for plugin in self._plugins.values():
            if not plugin.get("enabled", True):
                continue
            hooks = plugin.get("hooks", {})
            if "append_footer" in hooks:
                response += "\n" + hooks["append_footer"]
        return response
