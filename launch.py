#!/usr/bin/env python3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from shutil import which


APP_DIR = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765"
BROWSER_NAMES = ("brave", "brave-browser", "chromium", "google-chrome", "google-chrome-stable")


def server_is_running():
    try:
        with urllib.request.urlopen(f"{URL}/api/status", timeout=0.5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def main():
    own_server = None
    if not server_is_running():
        own_server = subprocess.Popen(
            [sys.executable, str(APP_DIR / "main.py"), "--serve"],
            cwd=APP_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(40):
            if server_is_running():
                break
            if own_server.poll() is not None:
                raise RuntimeError("O servidor local não iniciou. Verifique o OpenCV e tente novamente.")
            time.sleep(0.25)
        else:
            own_server.terminate()
            raise RuntimeError("O servidor local não respondeu a tempo.")

    browser = next((which(name) for name in BROWSER_NAMES if which(name)), None)
    if browser is None:
        raise RuntimeError("Instale um navegador compatível com Chromium (Brave, Chrome ou Chromium).")
    subprocess.Popen([browser, f"--app={URL}", "--no-first-run"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        print(f"Lista de Chamadas: {error}", file=sys.stderr)
        sys.exit(1)
