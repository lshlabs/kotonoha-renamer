"""Isolated AI bootstrap tests; never install/update the user's Ollama."""

import configparser
import contextlib
import ctypes
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import kotonoha_setup as setup
import kotonoha_storage as storage


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kotonoha-ai-test-")
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(storage, "DATA_DIR", self.root / "data"))
        self.stack.enter_context(
            patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "profile")})
        )
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(
            patch.object(ctypes.windll.shell32, "IsUserAnAdmin", return_value=False)
        )
        self.model = next(item["model"] for item in setup.catalog() if item["quant"] == "IQ4_XS")
        self.args = SimpleNamespace(
            model=self.model,
            install_ollama=True,
            update_ollama=False,
            set_default=False,
            status_file=str(self.root / "status.ini"),
        )

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_catalog_only_supergemma_quants_and_validated_model(self):
        choices = setup.catalog()
        self.assertEqual(len(choices), 11)
        self.assertTrue(
            all(
                item["model"].startswith("hf.co/mradermacher/SuperGemma-4-12b-abliterated-GGUF:")
                for item in choices
            )
        )
        self.assertTrue(all(item["vram_gb"] >= 6 and item["bytes"] > 4e9 for item in choices))
        with self.assertRaises(ValueError):
            setup.validate_model("llama3")

    def test_report_unicode_cancel_and_thread_cleanup(self):
        with setup.Reporter(self.args.status_file) as report:
            report.update("모델 다운로드 / 1 GB / 2 GB", 50)
            data = configparser.ConfigParser()
            data.read(self.args.status_file, encoding="utf-16")
            self.assertEqual(data["Progress"]["percent"], "50")
            self.assertIn("모델 다운로드", data["Progress"]["text"])
            Path(self.args.status_file + ".cancel").write_text("cancel")
            with self.assertRaises(setup.SetupCancelled):
                report.check()
        self.assertFalse(report.thread.is_alive())

    def test_existing_model_becomes_default_and_keeps_other_config(self):
        path = storage.DATA_DIR / "config.json"
        storage.atomic_json(path, {"model": "existing-user-model", "custom": 12})
        client = SimpleNamespace(list=lambda: [], show=lambda model: {})
        with (
            patch.object(setup, "find_ollama", return_value=None),
            patch.object(setup, "install_ollama") as install,
        ):
            setup.prepare(self.args, setup.Reporter(self.args.status_file), client)
        install.assert_not_called()
        self.assertEqual(storage.read_json(path), {"model": self.model, "custom": 12})

    def test_reuse_missing_model_never_downloads_or_changes_default(self):
        path = storage.DATA_DIR / "config.json"
        storage.atomic_json(path, {"model": "old-default"})
        self.args.reuse_model = True
        client = SimpleNamespace(
            list=lambda: [],
            show=lambda model: (_ for _ in ()).throw(RuntimeError("model disappeared")),
        )
        with (
            patch.object(setup, "find_ollama", return_value=None),
            patch.object(setup, "pull_model") as pull,
        ):
            with self.assertRaises(RuntimeError):
                setup.prepare(self.args, setup.Reporter(), client)
        pull.assert_not_called()
        self.assertEqual(storage.read_json(path)["model"], "old-default")

    def test_missing_install_and_explicit_update(self):
        client = SimpleNamespace(list=lambda: [], show=lambda model: {})
        for update in (False, True):
            self.args.update_ollama = update
            with (
                patch.object(setup, "find_ollama", return_value=None),
                patch.object(setup, "server_ready", return_value=False),
                patch.object(setup, "install_ollama") as install,
                patch.object(setup, "ensure_server"),
            ):
                setup.prepare(self.args, setup.Reporter(), client)
            install.assert_called_once()

    def test_signed_only_and_failed_install_not_executed(self):
        def download(path, report):
            path.write_bytes(b"not an installer")

        with (
            patch.object(setup, "download_installer", side_effect=download),
            patch.object(setup, "verify_signature", side_effect=RuntimeError("bad signature")),
            patch.object(setup.subprocess, "Popen") as run,
        ):
            with self.assertRaises(RuntimeError):
                setup.install_ollama(setup.Reporter())
        run.assert_not_called()

    def test_cancel_after_install_does_not_interrupt_installer(self):
        report = setup.Reporter(self.args.status_file)

        def installed(*_, **__):
            Path(self.args.status_file + ".cancel").write_text("cancel")
            return 0

        with (
            patch.object(setup, "download_installer"),
            patch.object(setup, "verify_signature"),
            patch.object(setup.subprocess, "Popen") as process,
        ):
            process.return_value.wait.side_effect = installed
            with self.assertRaises(setup.SetupCancelled):
                setup.install_ollama(report)
            process.return_value.kill.assert_not_called()

    def test_ctrl_c_waits_for_ollama_installer_instead_of_killing_it(self):
        with (
            patch.object(setup, "download_installer"),
            patch.object(setup, "verify_signature"),
            patch.object(setup.subprocess, "Popen") as process,
        ):
            process.return_value.wait.side_effect = [KeyboardInterrupt(), 0]
            with self.assertRaises(setup.SetupCancelled):
                setup.install_ollama(setup.Reporter())
            self.assertEqual(process.return_value.wait.call_count, 2)
            process.return_value.kill.assert_not_called()

    def test_failure_does_not_change_default_model(self):
        path = storage.DATA_DIR / "config.json"
        storage.atomic_json(path, {"model": "existing"})
        with (
            patch.object(setup, "find_ollama", return_value=Path("ollama.exe")),
            patch.object(setup, "ensure_server"),
            patch.object(setup, "server_ready", return_value=True),
            patch.object(setup, "pull_model", side_effect=RuntimeError("network error")),
        ):
            with self.assertRaises(RuntimeError):
                setup.prepare(self.args, setup.Reporter(), object())
        self.assertEqual(storage.read_json(path)["model"], "existing")

    def test_explicit_default_changes_only_model(self):
        path = storage.DATA_DIR / "config.json"
        storage.atomic_json(path, {"model": "existing", "other": "keep"})
        self.args.set_default = True
        client = SimpleNamespace(list=lambda: [], show=lambda model: {})
        with patch.object(setup, "find_ollama", return_value=Path("ollama.exe")):
            setup.prepare(self.args, setup.Reporter(), client)
        self.assertEqual(storage.read_json(path), {"model": self.model, "other": "keep"})

    def test_elevated_helper_refuses_wrong_profile_preparation(self):
        with patch.object(ctypes.windll.shell32, "IsUserAnAdmin", return_value=True):
            with self.assertRaises(RuntimeError):
                setup.prepare(self.args, setup.Reporter(), object())

    def test_empty_job_does_not_report_false_success(self):
        self.args.model, self.args.install_ollama = None, False
        with self.assertRaises(ValueError):
            setup.prepare(self.args, setup.Reporter(), object())

    def test_real_client_stream_pull_against_isolated_http_server(self):
        from ollama import Client

        class Handler(BaseHTTPRequestHandler):
            exists = False

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"models":[]}')

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/show":
                    self.send_response(200 if Handler.exists else 404)
                    self.end_headers()
                    self.wfile.write(
                        b'{"model_info":{}}' if Handler.exists else b'{"error":"model missing"}'
                    )
                else:
                    self.send_response(200)
                    self.end_headers()
                    events = [
                        {"status": "pulling manifest"},
                        {"status": "pulling layer", "total": 100, "completed": 50},
                        {"status": "pulling layer", "total": 100, "completed": 100},
                        {"status": "success"},
                    ]
                    Handler.exists = True
                    for event in events:
                        self.wfile.write(json.dumps(event).encode() + b"\n")
                        self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(host=f"http://127.0.0.1:{server.server_port}", timeout=5)
            with patch.object(setup, "find_ollama", return_value=None):
                setup.prepare(self.args, setup.Reporter(self.args.status_file), client)
            self.assertEqual(
                storage.read_json(storage.DATA_DIR / "config.json")["model"], self.model
            )
            progress = configparser.ConfigParser()
            progress.read(self.args.status_file, encoding="utf-16")
            self.assertEqual(progress["Progress"]["result"], "success")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_installer_download_checks_https_and_complete_length(self):
        for content, length, url, succeeds in (
            (b"abcd", "4", "https://official.example/test", True),
            (b"abc", "4", "https://official.example/test", False),
            (b"abcd", "4", "http://official.example/test", False),
        ):
            with self.subTest(content=content, url=url):
                response = io.BytesIO(content)
                response.headers, response.url = {"Content-Length": length}, url
                path = self.root / ("download" + str(len(list(self.root.iterdir()))) + ".exe")
                with patch.object(setup.urllib.request, "urlopen", return_value=response):
                    if succeeds:
                        setup.download_installer(path, setup.Reporter())
                        self.assertEqual(path.read_bytes(), content)
                    else:
                        with self.assertRaises(RuntimeError):
                            setup.download_installer(path, setup.Reporter())

    def test_real_powershell_rejects_unsigned_installer(self):
        path = self.root / "unsigned.exe"
        path.write_bytes(b"not signed")
        with self.assertRaises(RuntimeError):
            setup.verify_signature(path)


if __name__ == "__main__":
    unittest.main()
