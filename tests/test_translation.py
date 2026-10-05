"""Regression coverage for truncated batches, bounded requests and honest outcomes."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import kotonoha_engine as engine
from kotonoha_progress import ReceivedTitles
from kotonoha_work import WorkSession
from tests import test_kotonoha_gui as gui_tests


class Progress:
    def __init__(self):
        self.counts = []
        self.received = ReceivedTitles(set(), engine.valid_title_output)

    def request(self, requested, base, valid, attempt, strength):
        self.received = ReceivedTitles(requested, valid)
        self.counts.append(base)

    def text(self, text):
        self.received.update(text)

    def finish(self, done, state):
        self.counts.append(done)


class TranslationTests(unittest.TestCase):
    def test_transport_disconnect_does_not_reset_completed_response(self):
        calls = []

        def transport(**kwargs):
            calls.append(kwargs)
            yield SimpleNamespace(
                message=SimpleNamespace(content='{"translations":[{"id":1,"text":"제목"},')
            )
            raise OSError("connection closed")

        token = engine.CHAT_TRANSPORT.set(transport)
        try:
            with patch.object(engine, "_MODEL_TOUCHED", set()):
                response = engine.ollama_chat_with_retry(
                    model="test-model", format=engine.BATCH_OUTPUT_SCHEMA, on_text=lambda _: None
                )
        finally:
            engine.CHAT_TRANSPORT.reset(token)
        self.assertEqual(len(calls), 1)
        self.assertEqual(engine.parse_batch_results(response.message.content, {1, 2}), {1: "제목"})

    def test_truncated_string_keeps_complete_items_and_rejects_duplicates(self):
        content = '{"translations":[{"id":1,"text":"제목"},{"id":2,"text":"잘림'
        self.assertEqual(engine.parse_batch_results(content, {1, 2}), {1: "제목"})
        duplicate = (
            '{"translations":[{"id":1,"text":"제목"},{"id":1,"text":"중복"},{"id":2,"text":"잘림'
        )
        with self.assertRaises(json.JSONDecodeError):
            engine.parse_batch_results(duplicate, {1, 2})
        with self.assertRaises(json.JSONDecodeError):
            engine.parse_batch_results(content, {2})
        with self.assertRaises(json.JSONDecodeError):
            engine.parse_batch_results('{"text":"잘림', {1})

    def work(self, count):
        work = WorkSession.__new__(WorkSession)
        work.sources = [f"日本語{i}" for i in range(count)]
        work.translations, work.corrections, work.names = {}, [], {}
        work.model = "test-model"
        work.report = lambda _: None
        return work

    def test_51_titles_are_split_and_only_missing_id_is_retried(self):
        work, progress = self.work(51), Progress()
        requests = []

        def respond(**kwargs):
            prompt = kwargs["messages"][1]["content"]
            ids = json.loads(prompt.split("번역 대상 ID: ")[1].split("\n")[0])
            requests.append(ids)
            self.assertLessEqual(len(ids), 5)
            self.assertEqual(kwargs["options"]["temperature"], 0)
            items = [{"id": i, "text": f"제목 {i}"} for i in ids]
            content = json.dumps({"translations": items}, ensure_ascii=False)
            if len(requests) == 1:
                content = (
                    json.dumps({"translations": items[:-1]}, ensure_ascii=False)[:-2]
                    + ',{"id":5,"text":"잘림'
                )
            kwargs["on_text"](content)
            return SimpleNamespace(message=SimpleNamespace(content=content))

        with patch.object(engine, "ollama_chat_with_retry", side_effect=respond):
            result, _ = work.translate_with_progress(work.sources, 0, None, "initial", progress)
        self.assertEqual(len(result), 51)
        self.assertEqual(requests[-1], [5])
        self.assertEqual(progress.counts, sorted(progress.counts))

    def test_connection_failure_retains_completed_stream_items(self):
        work, progress = self.work(2), Progress()
        requests = []

        def respond(**kwargs):
            ids = json.loads(
                kwargs["messages"][1]["content"].split("번역 대상 ID: ")[1].split("\n")[0]
            )
            requests.append(ids)
            kwargs["on_text"]('{"translations":[{"id":1,"text":"제목"},')
            raise OSError("connection closed")

        with patch.object(engine, "ollama_chat_with_retry", side_effect=respond):
            result, _ = work.translate_with_progress(work.sources, 0, None, "initial", progress)
        self.assertEqual(result, {work.sources[0]: "제목"})
        self.assertEqual(requests, [[1, 2], [2]])

    def test_long_title_batches_and_context_are_bounded(self):
        work = self.work(100)
        work.sources = [s + "長" * 220 for s in work.sources]
        for batch in work.translation_batches(work.sources):
            self.assertEqual(len(batch), 1)
        ids = dict(enumerate(work.sources, 1))
        context = work.translation_context(ids, {99}, {})
        self.assertIn(99, [item["id"] for item in context])
        self.assertLessEqual(
            sum(len(json.dumps(item, ensure_ascii=False)) for item in context), 3000
        )


class OutcomeTests(unittest.TestCase):
    setUp = gui_tests.GuiTests.setUp
    tearDown = gui_tests.GuiTests.tearDown
    service = gui_tests.GuiTests.service
    finish = gui_tests.GuiTests.finish
    edit_all = gui_tests.GuiTests.edit_all

    def test_previous_version_manual_results_are_reused_without_ai(self):
        service = self.service()
        with patch.object(engine, "PROMPT_VERSION", "kotonoha-title-translation-1"):
            self.edit_all(service)
        original = service.work.translations.copy()
        with patch.object(
            engine, "ollama_chat_with_retry", side_effect=AssertionError("AI called")
        ):
            service.work.prepare()
        self.assertEqual(service.work.translations, original)
        self.assertEqual(service.work.incomplete(), [])

    def test_partial_and_failed_retry_keep_previous_translations(self):
        service = self.service()
        self.edit_all(service)
        service.work.model = "test-model"
        source = service.work.sources[0]
        original = service.work.translations.copy()
        with patch.object(type(service.work), "translate", return_value=({source: "새 제목"}, {})):
            service.dispatch("retry", {"ids": [1, 2]})
            self.finish(service, "partial")
        self.assertEqual(service.job["done"], 1)
        self.assertIn("미완료 1개", service.message)
        self.assertEqual(service.work.translations, original)
        service.dispatch("adopt", {"accept": False})
        with patch.object(type(service.work), "translate", return_value=({}, {})):
            service.dispatch("retry", {"ids": [1, 2]})
            self.finish(service, "failed")
        self.assertEqual(service.job["label"], "번역 실패")
        self.assertIsNone(service.candidate)
        self.assertEqual(service.work.translations, original)

    def test_initial_failure_is_not_reported_as_completed(self):
        service = self.service()
        service.work.model = "test-model"
        with patch.object(service.work, "prepare", return_value=None):
            service.dispatch("translate")
            self.finish(service, "failed")
        self.assertEqual(service.job["label"], "번역 실패")
        self.assertIn("미완료 4개", service.message)
