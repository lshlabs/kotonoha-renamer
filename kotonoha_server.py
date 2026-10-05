"""Loopback-only HTTP transport for the portable browser UI."""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from kotonoha_gui_service import GuiService

MAX_REQUEST = 64 * 1024
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def native_pick(kind):
    """A local Windows dialog; browsers cannot return unrestricted folder paths."""
    import tkinter as tk
    from tkinter import filedialog

    owner = tk.Tk()
    owner.withdraw()
    owner.attributes("-topmost", True)
    try:
        if kind == "folder":
            return filedialog.askdirectory(parent=owner, title="작품 폴더 선택", mustexist=True)
        return filedialog.askopenfilename(
            parent=owner, title="복구 로그 선택", filetypes=[("복구 로그", "*.json")]
        )
    finally:
        owner.destroy()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, service=None, port=0, picker=native_pick):
        self.service = service or GuiService()
        self.token = secrets.token_urlsafe(32)
        self.picker = picker
        self.picker_lock = threading.Lock()
        self.closing = threading.Event()
        self.stopped = threading.Event()
        self.assets = Path(__file__).resolve().parent / "gui"
        super().__init__(("127.0.0.1", port), Handler)
        self.url = f"http://localhost:{self.server_port}"
        self.hosts = {f"localhost:{self.server_port}", f"127.0.0.1:{self.server_port}"}
        self.origins = {"http://" + host for host in self.hosts}

    @property
    def browser_url(self):
        # Fragments never travel in HTTP requests or Referer headers.
        return self.url + "/#token=" + self.token

    def invoke(self, action, payload):
        if self.closing.is_set():
            raise RuntimeError("프로그램을 종료하고 있습니다.")
        if action == "shutdown":
            return {}
        if action in {"pick_folder", "pick_log"}:
            if self.service._busy():
                raise RuntimeError("작업이 끝난 뒤 선택하세요.")
            with self.picker_lock:
                selected = self.picker("folder" if action == "pick_folder" else "log")
                if not selected:
                    return None
                return self.service.dispatch(
                    "scan" if action == "pick_folder" else "import_log", {"path": selected}
                )
        return self.service.dispatch(action, payload)

    def stop(self):
        if self.closing.is_set():
            return
        self.closing.set()

        def finish():
            try:
                if self.service._busy():
                    self.service.dispatch("cancel")
                if self.service.thread:
                    self.service.thread.join()
            finally:
                self.shutdown()
                self.stopped.set()

        threading.Thread(target=finish, name="kotonoha-shutdown", daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "Kotonoha"
    sys_version = ""

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send(self, status, data, content_type="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, authenticated=False):
        if self.headers.get("Host") not in self.server.hosts:
            self.send(403, {"ok": False, "error": "허용되지 않은 접속 주소입니다."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.origins:
            self.send(403, {"ok": False, "error": "허용되지 않은 요청입니다."})
            return False
        if authenticated:
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied, "Bearer " + self.server.token):
                self.send(401, {"ok": False, "error": "Kotonoha.exe를 실행해 화면을 여세요."})
                return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path == "/api/snapshot":
            if self.allowed(authenticated=True):
                try:
                    self.send(200, {"ok": True, "data": self.server.service.snapshot()})
                except Exception as exc:
                    self.send(500, {"ok": False, "error": str(exc)})
            return
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if path not in assets:
            self.send(404, {"ok": False, "error": "페이지가 없습니다."})
            return
        name, content_type = assets[path]
        self.send(200, (self.server.assets / name).read_bytes(), content_type)

    def do_POST(self):
        if not self.allowed(authenticated=True):
            return
        if self.path != "/api/invoke":
            self.send(404, {"ok": False, "error": "지원하지 않는 요청입니다."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST or self.headers.get("Transfer-Encoding"):
                self.send(413, {"ok": False, "error": "요청 크기를 확인하세요."})
                return
            if self.headers.get_content_type() != "application/json":
                self.send(415, {"ok": False, "error": "JSON 요청이 필요합니다."})
                return
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or not isinstance(request.get("action"), str):
                raise ValueError("잘못된 요청입니다.")
            payload = request.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError("잘못된 요청입니다.")
            result = self.server.invoke(request["action"], payload)
            self.send(200, {"ok": True, "data": result})
            if request["action"] == "shutdown":
                self.server.stop()
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            self.send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self.send(409, {"ok": False, "error": str(exc)})

    def do_OPTIONS(self):
        self.send(403, {"ok": False, "error": "외부 페이지에서 접근할 수 없습니다."})
