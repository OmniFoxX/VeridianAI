import ast
import importlib.util
import sys
from pathlib import Path

def extract_imports(file_path):
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Could not parse {file_path}: {e}")
        return []
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split('.')[0]
                imports.append(mod)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split('.')[0]
                imports.append(mod)
    return imports

def check_module(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False

def main():
    # v2.2 (2026-05-29): was hardcoded to Path(r"E:\OracleAI_v2.1.6\backend"),
    # a Todd-specific leftover from v2.1.6. Self-locate now -- this file
    # lives in backend/, so __file__'s parent IS backend_dir.
    backend_dir = Path(__file__).resolve().parent
    py_files = list(backend_dir.rglob("*.py"))
    installed = set()
    missing = set()
    for py_file in py_files:
        mods = extract_imports(py_file)
        for mod in mods:
            if check_module(mod):
                installed.add(mod)
            else:
                missing.add(mod)
    print("=== Installed Packages ===")
    for m in sorted(installed):
        print(m)
    print("\n=== Missing Packages ===")
    for m in sorted(missing):
        print(m)

if __name__ == "__main__":
    main()