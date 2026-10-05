"""Portable launcher: start a local server and open the default browser."""

import json
import os
import sys
import urllib.request
import webbrowser
from pathlib import Path


def reopen(record):
    """Reopen a verified instance from this portable data directory."""
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        port = data["port"]
        token = data["token"]
        if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str):
            return False
        url = f"http://localhost:{port}"
        request = urllib.request.Request(
            url + "/api/snapshot", headers={"Authorization": "Bearer " + token}
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if not json.load(response).get("ok"):
                return False
        return webbrowser.open(url + "/#token=" + token)
    except (OSError, ValueError, KeyError):
        return False


def main():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    os.environ.setdefault("KOTONOHA_DATA_DIR", str(base / "data"))
    import kotonoha_storage as storage
    from kotonoha_server import LocalServer

    storage.DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = storage.DATA_DIR / "server.json"
    lock = storage.Lock("gui-server")
    try:
        lock.__enter__()
    except RuntimeError:
        if not reopen(record):
            raise RuntimeError("실행 중인 Kotonoha에 연결하지 못했습니다. 잠시 후 다시 실행하세요.")
        return
    try:
        with (storage.DATA_DIR / "gui.log").open("a", encoding="utf-8", buffering=1) as log:
            if sys.stdout is None:
                sys.stdout = log
            if sys.stderr is None:
                sys.stderr = log
            with LocalServer() as server:
                storage.atomic_json(record, {"port": server.server_port, "token": server.token})
                server.service.dispatch("models")
                if not webbrowser.open(server.browser_url):
                    print("브라우저를 열지 못했습니다. 주소: " + server.browser_url, flush=True)
                try:
                    server.serve_forever(poll_interval=0.1)
                except KeyboardInterrupt:
                    if server.service._busy():
                        server.service.dispatch("cancel")
                    if server.service.thread:
                        server.service.thread.join()
                finally:
                    record.unlink(missing_ok=True)
    finally:
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    main()
