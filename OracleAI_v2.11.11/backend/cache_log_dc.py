import atrest
import json
from config import LOG_DIR

# This targets ONLY the prompt_cache.log file
log_path = LOG_DIR / "prompt_cache.log"

print(f"--- Decoding Prompt Cache Logs from {log_path} ---")

with open(log_path, "rb") as f:
    for line in f:
        # Each line is a bundle of encrypted bytes
        encrypted_line = line.strip()
        if encrypted_line:
            # This decrypts the specific line using your .fernet_key
            decrypted_data = atrest.decrypt_bytes(encrypted_line)
            # Convert the raw bytes back into a readable JSON object
            data = json.loads(decrypted_data)
            
            # Print it out so you can see it!
            print(data)
