"""GUI boundaries and real filesystem jobs, isolated from user state and Ollama."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import kotonoha_engine as engine
import kotonoha_gui_service as gui
import kotonoha_nested as nested
import kotonoha_storage as storage
from tests import fixtures


class FakeClient:
    def __init__(self):
        self._client = self
        self.closed = False

    def close(self):
        self.closed = True

    def list(self):
        return SimpleNamespace(models=[])

    def ps(self):
        return SimpleNamespace(models=[])


class GuiTests(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown

    def service(self):
        self.root = fixtures.tree(self.parent)
        service = gui.GuiService(client_factory=FakeClient)
        with patch.object(
            engine, "ollama_chat_with_retry", side_effect=AssertionError("scan called AI")
        ):
            service.dispatch("scan", {"path": str(self.root)})
            self.finish(service)
        return service

    def finish(self, service, status="completed"):
        service.thread.join(10)
        self.assertFalse(service.thread.is_alive())
        self.assertEqual(service.job["status"], status, service.job.get("error"))

    def edit_all(self, service):
        names = {"作品": "작품", "外側": "바깥", "内側": "안쪽", "タイトル": "제목"}
        for row in service.snapshot()["rows"]:
            service.dispatch("edit", {"id": row["id"], "text": names[row["source"]]})

    def test_scan_is_offline_and_does_not_change_files(self):
        service = self.service()
        self.assertEqual(len(service.snapshot()["rows"]), 4)
        self.assertFalse((self.root / storage.LOG_NAME).exists())
        self.assertEqual(
            (self.root / "外側/内側/01-1　タイトル.flac").read_bytes(), b"unaltered payload"
        )
        with self.assertRaises(ValueError):
            service.dispatch("edit", {"id": 0, "text": "이름"})
        with self.assertRaises(ValueError):
            service.dispatch("unknown")

    def test_stale_plan_is_rejected_and_apply_undo_preserves_bytes(self):
        service = self.service()
        self.edit_all(service)
        preview = service.dispatch("preview")
        service.dispatch("strength", {"value": 4})
        with self.assertRaises(ValueError):
            service.dispatch("apply", {"plan_id": preview["plan_id"]})
        preview = service.dispatch("preview", {"output_name": "결과"})
        service.dispatch("apply", {"plan_id": preview["plan_id"]})
        self.finish(service)
        self.assertEqual(
            (self.root / "결과/바깥/안쪽/01-1　제목.flac").read_bytes(), b"unaltered payload"
        )
        record = service.snapshot()["records"][0]
        service.dispatch("undo", {"id": record["id"]})
        self.finish(service)
        self.assertEqual(
            (self.root / "外側/内側/01-1　タイトル.flac").read_bytes(), b"unaltered payload"
        )

    def test_candidate_requires_adoption_and_restore(self):
        service = self.service()
        self.edit_all(service)
        service.work.model = "test-model"
        source = service.work.sources[0]
        original = service.work.translations[source]
        with patch.object(gui.GuiWork, "translate", return_value=({source: "다른 작품"}, {})):
            service.dispatch("retry", {"ids": [1]})
            self.finish(service)
        self.assertEqual(service.work.translations[source], original)
        with self.assertRaises(ValueError):
            service.dispatch("preview")
        service.dispatch("adopt", {"accept": False})
        self.assertEqual(service.work.translations[source], original)
        with patch.object(gui.GuiWork, "translate", return_value=({source: "다른 작품"}, {})):
            service.dispatch("retry", {"ids": [1]})
            self.finish(service)
        service.dispatch("adopt", {"accept": True})
        self.assertEqual(service.work.translations[source], "다른 작품")
        service.dispatch("restore")
        self.assertEqual(service.work.translations[source], original)

    def test_historical_record_does_not_overwrite_current_log(self):
        service = self.service()
        _, log, first = nested.apply(self.root, [], "첫째", nested.content_entries(self.root))
        nested.undo(log, lambda _: True)

        _, log, second = nested.apply(self.root, [], "둘째", nested.content_entries(self.root))
        service.dispatch("undo", {"id": first["run_id"]})
        self.finish(service)
        self.assertEqual(storage.read_json(log)["run_id"], second["run_id"])
        self.assertTrue((self.root / "둘째/外側").exists())
        nested.undo(log, lambda _: True)

    def test_replace_is_offline_scoped_reviewable_and_reversible(self):
        service = self.service()
        self.edit_all(service)
        first, second = service.work.sources[:2]
        service.dispatch("edit", {"id": 1, "text": "바니걸 메이드 바니걸 메이드"})
        service.dispatch("edit", {"id": 2, "text": "바니걸 메이드"})
        pairs = [{"current_expression": "바니걸 메이드", "desired_expression": "바니 메이드"}]
        with patch.object(service, "client_factory", side_effect=AssertionError("AI called")):
            service.dispatch("replace", {"ids": [1], "corrections": pairs})
            self.assertEqual(set(service.candidate["translations"]), {first})
            self.assertEqual(service.work.translations[first], "바니걸 메이드 바니걸 메이드")
            service.dispatch("adopt", {"accept": False})
            service.dispatch("replace", {"corrections": pairs})
            self.assertEqual(set(service.candidate["translations"]), {first, second})
            service.dispatch("adopt", {"accept": True})
        self.assertEqual(service.work.translations[first], "바니 메이드 바니 메이드")
        self.assertTrue({first, second}.issubset(service.work.manual))
        service.dispatch("restore")
        self.assertEqual(service.work.translations[second], "바니걸 메이드")
        with self.assertRaises(ValueError):
            service.dispatch("replace", {"corrections": []})
        with self.assertRaises(ValueError):
            service.dispatch("replace", {"ids": [0], "corrections": pairs})
        with self.assertRaises(ValueError):
            service.dispatch(
                "replace",
                {"corrections": [{"current_expression": "없음", "desired_expression": "변경"}]},
            )

    def test_reapply_updates_existing_output_and_full_undo(self):
        service = self.service()
        self.edit_all(service)
        plan = service.dispatch("preview", {"output_name": "결과"})
        service.dispatch("apply", {"plan_id": plan["plan_id"]})
        self.finish(service)
        _, prior = nested.recover(self.root / storage.LOG_NAME)
        prior.pop("gui_state")
        nested.persist(prior)  # Previous GUI versions did not save session metadata.
        reopened = gui.GuiService(client_factory=FakeClient)
        reopened.dispatch("scan", {"path": str(self.root)})
        self.finish(reopened)
        service = reopened
        self.assertEqual(service.applied_output, self.root / "결과")
        source = service.work.sources.index("タイトル") + 1
        for name, output in (("수정", "결과"), ("제목", "새 결과"), ("수정", "새 결과")):
            service.dispatch("edit", {"id": source, "text": name})
            plan = service.dispatch("preview", {"output_name": output})
            self.assertTrue(plan["reapply"])
            service.dispatch("apply", {"plan_id": plan["plan_id"]})
            self.finish(service)
            self.assertEqual(
                (self.root / output / f"바깥/안쪽/01-1　{name}.flac").read_bytes(),
                b"unaltered payload",
            )
        nested.undo(self.root / storage.LOG_NAME, lambda _: True)
        self.assertEqual(
            (self.root / "外側/内側/01-1　タイトル.flac").read_bytes(), b"unaltered payload"
        )

    def test_cancel_and_busy_guard(self):
        import threading

        service = self.service()
        entered, proceed = threading.Event(), threading.Event()

        def work():
            entered.set()
            proceed.wait(2)
            service.check_cancel()

        service._start("test", work)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(RuntimeError):
            service.dispatch("strength", {"value": 10})
        service.dispatch("cancel")
        proceed.set()
        self.finish(service, "cancelled")
        self.assertEqual(service.job["status"], "cancelled")

    def test_models_and_preferences_are_structured(self):
        service = self.service()
        service.dispatch("models")
        self.finish(service)
        self.assertTrue(service.model_state["available"])
        self.assertTrue(service.model_state["items"])
        self.assertTrue(all(not item["installed"] for item in service.model_state["items"]))
        service.dispatch("preference", {"source": "作品", "desired": "작품"})
        self.assertEqual(service.snapshot()["preferences"]["作品"], "작품")
        service.dispatch("delete_preference", {"source": "作品"})
        self.assertNotIn("作品", service.snapshot()["preferences"])


if __name__ == "__main__":
    unittest.main()
