"""Authenticated loopback HTTP flow over the real file engine."""

import http.client
import json
import threading
import unittest
from unittest.mock import patch

import kotonoha_gui
import kotonoha_gui_service as gui
import kotonoha_server as server
import kotonoha_storage as storage
from tests import fixtures
from tests.test_kotonoha_gui import FakeClient


class ServerTests(unittest.TestCase):
    def setUp(self):
        fixtures.Tests.setUp(self)
        self.service = gui.GuiService(client_factory=FakeClient)
        self.server = server.LocalServer(self.service, picker=lambda _: str(self.root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.root = fixtures.tree(self.parent)

    def tearDown(self):
        if not self.server.stopped.is_set():
            self.server.stop()
        self.assertTrue(self.server.stopped.wait(5))
        self.thread.join(5)
        self.server.server_close()
        fixtures.Tests.tearDown(self)

    def request(self, path="/api/snapshot", action=None, payload=None, headers=None, body=None):
        defaults = {"Authorization": "Bearer " + self.server.token}
        defaults.update(headers or {})
        method = "GET"
        if action is not None or body is not None:
            method = "POST"
            defaults.setdefault("Content-Type", "application/json")
            body = (
                body
                if body is not None
                else json.dumps({"action": action, "payload": payload or {}})
            )
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=defaults)
        response = connection.getresponse()
        status, response_headers, raw = (
            response.status,
            dict(response.getheaders()),
            response.read(),
        )
        connection.close()
        return status, response_headers, raw

    def action(self, name, payload=None):
        status, _, raw = self.request("/api/invoke", action=name, payload=payload)
        result = json.loads(raw)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        return result["data"]

    def finish(self):
        self.service.thread.join(5)
        self.assertFalse(self.service.thread.is_alive())
        self.assertEqual(self.service.job["status"], "completed", self.service.job)

    def test_authentication_host_origin_and_input_boundaries(self):
        self.assertEqual(self.request(headers={"Authorization": ""})[0], 401)
        self.assertEqual(self.request(headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request(headers={"Host": "outside.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "https://outside.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": self.server.url})[0], 200)
        self.assertEqual(self.request("/api/invoke", body="{bad json")[0], 400)
        self.assertEqual(
            self.request("/api/invoke", body="{}", headers={"Content-Type": "text/plain"})[0], 415
        )
        self.assertEqual(self.request("/api/invoke", body="x" * (server.MAX_REQUEST + 1))[0], 413)
        self.assertIsNone(self.service.work)

    def test_static_assets_do_not_expose_arbitrary_files(self):
        for path in ("/", "/style.css", "/app.js"):
            status, headers, raw = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(raw)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn(b"pywebview", self.request("/app.js")[2])
        self.assertEqual(self.request("/../data/server.json")[0], 404)
        self.assertEqual(self.request("/kotonoha_storage.py")[0], 404)

    def test_browser_transport_scan_edit_preview_apply_and_undo(self):
        self.action("scan", {"path": str(self.root)})
        self.finish()
        rows = json.loads(self.request()[2])["data"]["rows"]
        names = {"作品": "작품", "外側": "바깥", "内側": "안쪽", "タイトル": "제목"}
        for row in rows:
            self.action("edit", {"id": row["id"], "text": names[row["source"]]})
        preview = self.action("preview", {"output_name": "작품"})
        self.assertIn(
            str(self.root / "작품/바깥/안쪽/01-1　제목.flac"),
            [c["target"] for c in preview["changes"]],
        )
        self.action("apply", {"plan_id": preview["plan_id"]})
        self.finish()
        self.assertEqual(
            (self.root / "작품/바깥/안쪽/01-1　제목.flac").read_bytes(), b"unaltered payload"
        )
        record = self.service.saved_logs[0]
        self.action("undo", {"id": record["id"]})
        self.finish()
        self.assertEqual(
            (self.root / "外側/内側/01-1　タイトル.flac").read_bytes(), b"unaltered payload"
        )

    def test_native_picker_and_browser_shutdown(self):
        self.action("pick_folder")
        self.finish()
        self.assertEqual(self.service.work.root, self.root)
        entered = threading.Event()

        def running():
            entered.set()
            self.service.cancel_event.wait(3)
            self.service.check_cancel()

        self.service._start("test", running)
        self.assertTrue(entered.wait(2))
        self.action("shutdown")
        self.assertTrue(self.server.stopped.wait(5))
        self.assertEqual(self.service.job["status"], "cancelled")

    def test_reopen_verifies_the_existing_server(self):
        record = self.data / "server.json"
        storage.atomic_json(record, {"port": self.server.server_port, "token": self.server.token})
        with patch("webbrowser.open", return_value=True) as open_browser:
            self.assertTrue(kotonoha_gui.reopen(record))
            open_browser.assert_called_once_with(self.server.browser_url)
        storage.atomic_json(record, {"port": "not a port", "token": "bad"})
        self.assertFalse(kotonoha_gui.reopen(record))

    def test_frozen_data_stays_next_to_the_program(self):
        with (
            patch("sys.frozen", True, create=True),
            patch("sys.executable", str(self.parent / "Kotonoha.exe")),
        ):
            self.assertEqual(storage.default_data_dir(), self.parent / "data")

    def test_previous_install_journal_is_verified_before_import(self):
        import copy

        import kotonoha_nested as nested

        _, log, data = nested.apply(self.root, [], "작품", nested.content_entries(self.root))
        nested.undo(log, lambda _: True)
        journal = storage.journal_path(data)
        previous = self.parent / "previous-profile"
        old = previous / "Kotonoha/recovery" / journal.name
        old.parent.mkdir(parents=True)
        journal.rename(old)
        with patch.dict("os.environ", {"LOCALAPPDATA": str(previous)}):
            self.assertEqual(storage.recover(log)[1]["run_id"], data["run_id"])
            self.assertTrue(journal.exists())
            journal.unlink()
            tampered = copy.deepcopy(storage.read_json(old))
            tampered["root_identity"]["inode"] += 1
            storage.atomic_json(old, tampered)
            with self.assertRaises(RuntimeError):
                storage.recover(log)
            self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
