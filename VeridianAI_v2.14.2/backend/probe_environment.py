import sys
import subprocess
import json
import os

def main():
    results = {}
    # 1. Basic Python & paths
    results["python_version"] = sys.version
    results["executable"] = sys.executable
    results["cwd"] = os.getcwd()
    
    # 2. Check for common AI/ML packages via pip list (non‑blocking, timeout)
    try:
        pip_list = subprocess.check_output(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            stderr=subprocess.STDOUT,
            timeout=15
        )
        results["installed_packages"] = json.loads(pip_list)
    except Exception as e:
        results["pip_error"] = str(e)
    
    # 3. Look for likely local model servers (simple port check via netstat/nmap if available—skip if not)
    # Instead, just note common localhost ports people use for local LLMs
    common_ports = [11434, 8080, 8000, 7860, 5000, 1234]
    results["suggested_local_llm_ports"] = common_ports
    
    # 4. Attempt to import Hermes‑related names if they exist (won’t fail if missing)
    optional_modules = [
        "hermes", "hermes_agent", "MCP", "model_context_protocol",
        "continue", "openai"
    ]
    imports = {}
    for mod in optional_modules:
        try:
            m = __import__(mod)
            imports[mod] = {"status": "success", "version": getattr(m, "__version__", "unknown")}
        except ImportError as e:
            imports[mod] = {"status": "not_found", "error": str(e)}
        except Exception as e:
            imports[mod] = {"status": "error", "error": str(e)}
    results["optional_imports"] = imports
    
    # 5. Save a readable summary
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()