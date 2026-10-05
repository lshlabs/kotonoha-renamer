"""Restricted GUI actions and serial background jobs over the shared engines."""

import copy
import subprocess
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from ollama import Client

import kotonoha_engine as engine
import kotonoha_models as models
import kotonoha_nested as nested
import kotonoha_setup as setup
import kotonoha_storage as storage
from kotonoha_paths import iter_work_paths, split_title, translation_title, validate_root
from kotonoha_progress import ReceivedTitles
from kotonoha_version import VERSION
from kotonoha_work import WorkCancelled, WorkSession


class GuiProgress:
    def __init__(self, work, total):
        self.work, self.total = work, total
        self.base = 0
        self.received = ReceivedTitles(set(), lambda _: True)

    def request(self, requested, base, valid, attempt, strength):
        self.received = ReceivedTitles(requested, valid)
        self.base = base
        self.work.service.check_cancel()
        self.work.service.progress(
            base, self.total, "제목 번역", attempt=attempt, strength=strength
        )

    def text(self, text):
        self.work.service.check_cancel()
        self.work.service.progress(
            self.base + len(self.received.update(text)),
            self.total,
            "제목 번역",
            characters=len(text),
        )

    def finish(self, done, state):
        self.work.service.progress(done, self.total, state)


class GuiWork(WorkSession):
    def __init__(self, service, root, model):
        self.service = service
        super().__init__(root, model, prepare=False)
        self.approved = storage.read_json(
            self.approved_path, {"version": 2, "works": {}, "namespaces": {}}
        )

    def report(self, message):
        self.service.message = message

    @engine.releases_model
    def translate(self, selected, temperature, correction=None, reason="user_retry"):
        progress = GuiProgress(self, len(selected))
        return self.translate_with_progress(selected, temperature, correction, reason, progress)


class GuiSetupReport(setup.Reporter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def cancelled(self):
        return self.service.cancel_event.is_set()

    def publish(self):
        self.service.progress(self.percent, 100, self.text, unit="percent")


class GuiService:
    def __init__(self, client_factory=None):
        self.guard = threading.RLock()
        self.cancel_event = threading.Event()
        self.client_factory = client_factory or (
            lambda: Client(host=setup.LOCAL_SERVER, timeout=60)
        )
        self.client = None
        self.thread = None
        self.work = None
        self.entries = []
        self.applied_output = None
        self.revision = 0
        self.preview_plan = None
        self.candidate = None
        self.output_name = ""
        self.message = "대상 폴더를 선택하세요."
        self.job = {"status": "idle", "label": "대기", "done": 0, "total": 0, "unit": "titles"}
        self.model_state = {
            "available": False,
            "error": "",
            "default": None,
            "items": [],
            "running": [],
            "ollama_installed": bool(setup.find_ollama()),
        }
        self.unload_state = ""
        self.saved_logs = []
        storage.DATA_DIR.mkdir(parents=True, exist_ok=True)

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise WorkCancelled("작업을 중단했습니다. 완료된 파일 변경은 기록에서 확인하세요.")

    def progress(self, done, total, label, **extra):
        with self.guard:
            self.job.update(label=label, done=done, total=total, **extra)
            if extra.get("characters"):
                self.job["last_response"] = time.time()

    def _busy(self):
        return self.job["status"] in {"running", "cancelling"}

    def _invalidate(self):
        self.revision += 1
        self.preview_plan = None

    def _require_work(self):
        if not self.work:
            raise ValueError("대상 폴더를 먼저 선택하세요.")
        return self.work

    def _start(self, label, function, hold_lock=True):
        with self.guard:
            if self._busy():
                raise RuntimeError("진행 중인 작업이 끝난 뒤 실행하세요.")
            self.cancel_event.clear()
            job_id = uuid.uuid4().hex
            self.job = {
                "id": job_id,
                "status": "running",
                "label": label,
                "started": time.time(),
                "done": 0,
                "total": 0,
                "unit": "titles",
                "error": "",
            }
            self.message = ""

        def worker():
            terminal_status = "completed"
            try:
                if hold_lock:
                    with storage.Lock("user-data"):
                        function()
                else:
                    function()
            except (WorkCancelled, nested.Cancelled, setup.SetupCancelled) as exc:
                with self.guard:
                    terminal_status = "cancelled"
                    self.job.update(error=str(exc))
                    self.message = str(exc)
            except Exception as exc:
                with self.guard:
                    terminal_status = "cancelled" if self.cancel_event.is_set() else "failed"
                    self.job.update(error=str(exc))
                    self.message = str(exc)
            finally:
                if self.client is not None:
                    self.client._client.close()
                    self.client = None
                self.refresh_records()
                with self.guard:
                    if terminal_status == "completed":
                        terminal_status = self.job.pop("outcome", terminal_status)
                    self.job["status"] = terminal_status

        self.thread = threading.Thread(target=worker, name="kotonoha-work", daemon=False)
        self.thread.start()
        return {"job_id": job_id}

    def _chat(self, **kwargs):
        self.check_cancel()
        if not kwargs.get("stream"):
            result = self.client.chat(**kwargs)
            self.check_cancel()
            return result

        def stream():
            try:
                for chunk in self.client.chat(**kwargs):
                    self.check_cancel()
                    self.job["last_response"] = time.time()
                    yield chunk
            except Exception:
                self.check_cancel()
                raise

        return stream()

    def _translation(self, selected=None, corrections=None):
        work = self._require_work()
        if not work.model:
            raise ValueError("모델·설정에서 기본 모델을 준비하세요.")
        self.client = self.client_factory()
        token = engine.CHAT_TRANSPORT.set(self._chat)
        final_label = "번역 실패"
        try:
            engine.MODEL = work.model
            self.unload_state = ""
            if selected is None:
                self.progress(0, len(work.sources), "고유명사·번역 준비")
                work.prepare()
                work.save_accepted()
                total = len(work.sources)
                done = total - len(work.incomplete())
            else:
                translated, records = work.translate(
                    selected,
                    (work.strength - 1) / 10,
                    correction=corrections,
                    reason="user_correction" if corrections else "user_retry",
                )
                total, done = len(selected), len(translated)
                self.candidate = (
                    {
                        "translations": translated,
                        "records": records,
                        "corrections": corrections or [],
                        "revision": self.revision,
                    }
                    if translated
                    else None
                )
            self.check_cancel()
            self._invalidate()
            if self.candidate:
                self.candidate["revision"] = self.revision
            self.output_name = self._suggest_output()
            missing = total - done
            final_label = (
                "번역 완료" if not missing else "일부 번역 미완료" if done else "번역 실패"
            )
            if missing:
                self.job["outcome"] = "partial" if done else "failed"
                self.message = f"번역 {done}/{total}개 완료 · 미완료 {missing}개. 미완료 제목을 선택해 다시 번역하세요."
                if not done:
                    self.job["error"] = self.message
            else:
                self.message = (
                    "새 후보를 확인하세요."
                    if self.candidate
                    else "번역안을 확인한 뒤 이름 변경을 적용하세요."
                )
            self.progress(done, total, final_label)
        finally:
            engine.CHAT_TRANSPORT.reset(token)
            self.progress(self.job["done"], self.job["total"], "모델 메모리 확인")
            try:
                running = self.client_factory()
                try:
                    names = {item.model for item in running.ps().models}
                    self.unload_state = (
                        "선택 모델 해제 확인"
                        if work.model not in names
                        else "선택 모델이 아직 로딩되어 있습니다."
                    )
                finally:
                    running._client.close()
            except Exception:
                self.unload_state = "모델 해제 상태 확인 실패"
            self.progress(
                self.job["done"],
                self.job["total"],
                "번역 중단" if self.cancel_event.is_set() else final_label,
            )

    def _suggest_output(self):
        work = self._require_work()
        root_meta = next((meta for meta in work.meta if meta["type"] == "root"), None)
        if root_meta:
            title = work.translations.get(root_meta["title"], root_meta["title"])
            return engine.sanitize_name(root_meta["prefix"] + title)
        return engine.sanitize_name(work.root.name)

    def _scan(self, path):
        root = Path(path).expanduser().absolute()
        previous = storage.read_json(root / storage.LOG_NAME, {})
        reusable = previous.get("mode") == nested.MODE and previous.get("status") == "completed"
        if reusable:
            nested.recover(root / storage.LOG_NAME)
        validate_root(root, check_pending=not reusable)
        self.applied_output = None
        self.entries = nested.content_entries(root)
        config = models.config()
        self.work = GuiWork(self, root, config.get("model") or "")
        log = storage.read_json(root / storage.LOG_NAME, {})
        if log.get("mode") == nested.MODE and log.get("status") == "completed":
            _, log = nested.recover(root / storage.LOG_NAME)
            saved = log.get("gui_state")
            if not saved:
                output = Path(log["planned_root"])
                meta, sources, translations = [], [], {}
                for current, kind in [(root, "root"), *iter_work_paths(output, False)]:
                    original = current
                    if kind != "root":
                        for operation in reversed(log["completed_operations"]):
                            if operation["type"] == "output":
                                continue
                            target = Path(operation["target"])
                            if original == target or target in original.parents:
                                original = Path(operation["source"]) / original.relative_to(target)
                    parts = translation_title(original, kind)
                    if parts is None:
                        continue
                    prefix, title = parts
                    meta.append(
                        {
                            "path": str(current),
                            "type": kind,
                            "prefix": prefix,
                            "title": title,
                            "identity": storage.identity(current),
                        }
                    )
                    if title not in sources:
                        sources.append(title)
                    name = (
                        output.name
                        if kind == "root"
                        else (current.stem if kind == "file" else current.name)
                    )
                    translations[title] = split_title(name)[1]
                meta.sort(
                    key=lambda item: (
                        0 if item["type"] == "file" else 2 if item["type"] == "root" else 1,
                        -len(Path(item["path"]).parts),
                        item["path"].lower(),
                    )
                )
                saved = {
                    "meta": meta,
                    "sources": sources,
                    "translations": translations,
                    "manual": sources,
                    "records": log.get("translation", {}),
                }
            meta = copy.deepcopy(saved["meta"])
            for item in meta:
                item["path"] = Path(item["path"])
                path = item["path"]
                if (
                    (path != root and root not in path.parents)
                    or not path.exists()
                    or storage.identity(path) != item["identity"]
                ):
                    raise RuntimeError(
                        "적용한 항목이 이동 또는 교체됐습니다. 작업 기록을 확인하세요."
                    )
                validate_root(path if item["type"] == "root" else path.parent, check_pending=False)
            self.work.meta = meta
            self.work.sources = saved["sources"]
            self.work.translations = saved["translations"]
            self.work.manual = set(saved["manual"])
            self.work.records = saved["records"]
            self.applied_output = Path(log["planned_root"])
        self.output_name = self._suggest_output()
        self.candidate = None
        self._invalidate()
        self.message = f"제목 {len(self.work.sources)}개를 확인했습니다. 번역을 시작하세요."

    def refresh_models(self):
        client = self.client_factory()
        state = {
            "available": False,
            "error": "",
            "default": models.config().get("model"),
            "items": [],
            "running": [],
            "ollama_installed": bool(setup.find_ollama()),
        }
        try:
            installed = {item.model: item for item in client.list().models}
            state["available"] = True
            state["running"] = [item.model for item in client.ps().models]
            for item in setup.catalog():
                state["items"].append({**item, "installed": item["model"] in installed})
        except Exception as exc:
            state["error"] = "Ollama에 연결할 수 없습니다: " + str(exc)
            state["items"] = [{**item, "installed": False} for item in setup.catalog()]
        finally:
            client._client.close()
        self.model_state = state

    def refresh_records(self):
        records = []
        for path in (storage.DATA_DIR / "recovery").glob("*.json"):
            try:
                data = storage.read_json(path)
                records.append(
                    {
                        "id": data["run_id"],
                        "root": data["root"],
                        "output": data["planned_root"],
                        "status": data["status"],
                        "completed": len(data.get("completed_operations", [])),
                        "total": len(data.get("planned_operations", [])),
                    }
                )
            except (OSError, ValueError, KeyError):
                continue
        self.saved_logs = records

    def _get_log(self, run_id):
        self.refresh_records()
        record = next((item for item in self.saved_logs if item["id"] == run_id), None)
        if not record:
            raise ValueError("작업 기록을 찾을 수 없습니다.")
        journal = storage.DATA_DIR / "recovery" / (record["id"] + ".json")
        current = Path(record["root"]) / storage.LOG_NAME
        data = storage.read_json(current, {})
        if data.get("run_id") == run_id:
            return current
        if record["status"] == "undone":
            return journal
        raise ValueError("폴더의 현재 기록과 선택한 기록이 다릅니다. 기록을 유지하세요.")

    def snapshot(self):
        with self.guard:
            rows = []
            if self.work:
                incomplete = set(self.work.incomplete())
                counts = Counter(meta["title"] for meta in self.work.meta)
                for index, source in enumerate(self.work.sources, 1):
                    rows.append(
                        {
                            "id": index,
                            "source": source,
                            "translation": self.work.translations.get(source, ""),
                            "manual": source in self.work.manual,
                            "status": "확인 필요"
                            if source in incomplete
                            else "직접 수정"
                            if source in self.work.manual
                            else "완료",
                            "count": counts[source],
                        }
                    )
            candidate = []
            if self.candidate and self.work:
                candidate = [
                    {
                        "source": key,
                        "current": self.work.translations.get(key, ""),
                        "candidate": value,
                    }
                    for key, value in self.candidate["translations"].items()
                ]
            return copy.deepcopy(
                {
                    "version": VERSION,
                    "busy": self._busy(),
                    "job": self.job,
                    "root": str(self.work.root) if self.work else "",
                    "rows": rows,
                    "model": self.work.model if self.work else self.model_state["default"],
                    "strength": self.work.strength if self.work else 1,
                    "revision": self.revision,
                    "output_name": self.output_name,
                    "message": self.message,
                    "models": self.model_state,
                    "candidate": candidate,
                    "unload": self.unload_state,
                    "records": self.saved_logs,
                    "preferences": engine.load_preferences().get("terms", {}),
                }
            )

    def dispatch(self, action, payload=None):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("잘못된 요청입니다.")
        with self.guard:
            if action == "cancel":
                self.cancel_event.set()
                if self._busy():
                    self.job["status"] = "cancelling"
                    if self.client:
                        self.client._client.close()
                return {}
            if self._busy():
                raise RuntimeError("작업 중에는 설정·번역안을 변경할 수 없습니다.")
            if action == "scan":
                return self._start("폴더 확인", lambda: self._scan(payload["path"]))
            if action == "models":
                return self._start("모델 확인", self.refresh_models)
            if action == "records":
                self.refresh_records()
                return {}
            work = self.work
            if action == "translate":
                return self._start("번역 준비", self._translation)
            if action in {"edit", "replace", "strength", "retry", "restore", "adopt", "preview"}:
                work = self._require_work()
            if action == "replace":
                ids = payload.get("ids") or list(range(1, len(work.sources) + 1))
                if any(not isinstance(i, int) or not 1 <= i <= len(work.sources) for i in ids):
                    raise ValueError("선택한 제목 번호가 잘못됐습니다.")
                replacements = payload.get("corrections")
                if not isinstance(replacements, list) or not replacements:
                    raise ValueError("바꿀 표현과 원하는 표현을 입력하세요.")
                pairs = []
                for item in replacements:
                    if not isinstance(item, dict) or not all(
                        isinstance(item.get(key), str) and item[key].strip()
                        for key in ("current_expression", "desired_expression")
                    ):
                        raise ValueError("바꿀 표현과 원하는 표현을 입력하세요.")
                    pairs.append((item["current_expression"], item["desired_expression"]))
                changed = {}
                for index in sorted(set(ids)):
                    source = work.sources[index - 1]
                    original = work.translations.get(source, "")
                    value = original
                    for old, new in pairs:
                        value = value.replace(old, new)
                    if value != original:
                        if not engine.valid_title_output(value):
                            raise ValueError("바꾼 결과는 한 줄의 제목이어야 합니다.")
                        changed[source] = value
                if not changed:
                    raise ValueError("선택 범위의 번역에서 바꿀 표현을 찾지 못했습니다.")
                self.candidate = {
                    "translations": changed,
                    "records": {},
                    "corrections": [],
                    "manual": True,
                    "revision": self.revision,
                }
                self.message = f"표현을 바꿀 제목 {len(changed)}개를 확인하세요."
                return {}
            if action == "edit":
                index = int(payload["id"])
                if not 1 <= index <= len(work.sources):
                    raise ValueError("제목 번호가 잘못됐습니다.")
                source = work.sources[index - 1]
                value = payload["text"].strip()
                if not engine.valid_title_output(value):
                    raise ValueError("한 줄의 제목을 입력하세요.")
                work.history.append(work.snapshot())
                work.translations[source] = value
                work.manual.add(source)
                with storage.Lock("user-data"):
                    work.save_accepted()
                self._invalidate()
                self.output_name = self._suggest_output()
                return {}
            if action == "strength":
                value = int(payload["value"])
                if not 1 <= value <= 10:
                    raise ValueError("강도는 1~10입니다.")
                work.strength = value
                self._invalidate()
                return {}
            if action == "retry":
                ids = payload.get("ids") or [
                    i for i, source in enumerate(work.sources, 1) if source not in work.manual
                ]
                if any(not isinstance(i, int) or not 1 <= i <= len(work.sources) for i in ids):
                    raise ValueError("선택한 제목 번호가 잘못됐습니다.")
                selected = [work.sources[i - 1] for i in sorted(set(ids))]
                if not selected:
                    raise ValueError("다시 번역할 제목을 선택하세요.")
                corrections = payload.get("corrections") or []
                for item in corrections:
                    if not all(
                        isinstance(item.get(key), str) and item[key].strip()
                        for key in ("current_expression", "desired_expression")
                    ):
                        raise ValueError("바꿀 표현과 원하는 표현을 입력하세요.")
                self.candidate = None
                return self._start(
                    "선택 제목 재번역", lambda: self._translation(selected, corrections)
                )
            if action == "adopt":
                if not self.candidate or self.candidate["revision"] != self.revision:
                    raise ValueError("새 후보가 없거나 번역안이 바뀌었습니다.")
                if payload.get("accept"):
                    work.history.append(work.snapshot())
                    work.translations.update(self.candidate["translations"])
                    work.records.update(self.candidate["records"])
                    work.corrections.extend(self.candidate["corrections"])
                    if self.candidate.get("manual"):
                        work.manual.update(self.candidate["translations"])
                    else:
                        work.manual.difference_update(self.candidate["translations"])
                    with storage.Lock("user-data"):
                        work.save_accepted()
                self.candidate = None
                self._invalidate()
                self.output_name = self._suggest_output()
                return {}
            if action == "restore":
                if not work.history:
                    raise ValueError("직전 번역안이 없습니다.")
                work.translations, work.manual, work.corrections, work.records, work.strength = (
                    work.history.pop()
                )
                with storage.Lock("user-data"):
                    work.save_accepted()
                self.candidate = None
                self._invalidate()
                self.output_name = self._suggest_output()
                return {}
            if action == "preview":
                if self.candidate:
                    raise ValueError("새 후보를 사용하거나 현재안을 유지한 뒤 적용하세요.")
                name = engine.sanitize_name(payload.get("output_name", self.output_name))
                inner = [op for op in work.plan() if op["type"] != "root"]
                update_revision = None
                base = work.root
                entries = self.entries
                if self.applied_output:
                    output, _, data = nested.preview_update(
                        work.root / storage.LOG_NAME, inner, name
                    )
                    update_revision = data["revision"]
                    base = self.applied_output
                    entries = []
                else:
                    output, _ = nested.preview(work.root, inner, name, self.entries)
                plan_id = uuid.uuid4().hex
                self.preview_plan = {
                    "id": plan_id,
                    "revision": self.revision,
                    "inner": inner,
                    "output_name": name,
                    "entries": copy.deepcopy(entries),
                    "base": base,
                    "update_revision": update_revision,
                }
                return {
                    "plan_id": plan_id,
                    "revision": self.revision,
                    "output": str(output),
                    "incomplete": len(work.incomplete()),
                    "reapply": update_revision is not None,
                    "changes": nested.final_changes(base, inner, output, entries)
                    + (
                        [{"source": str(base), "target": str(output), "type": "folder"}]
                        if update_revision is not None and base != output
                        else []
                    ),
                }
            if action == "apply":
                plan = self.preview_plan
                if (
                    not plan
                    or plan["id"] != payload.get("plan_id")
                    or plan["revision"] != self.revision
                ):
                    raise ValueError("적용 계획이 오래됐습니다. 변경 경로를 다시 확인하세요.")
                self.preview_plan = None

                def apply():
                    if plan["update_revision"] is not None:
                        final, _, _ = nested.update(
                            work.root / storage.LOG_NAME,
                            plan["inner"],
                            plan["output_name"],
                            plan["update_revision"],
                            progress=self.progress,
                            cancel=self.cancel_event.is_set,
                        )
                    else:
                        final, _, _ = nested.apply(
                            work.root,
                            plan["inner"],
                            plan["output_name"],
                            plan["entries"],
                            metadata=work.records,
                            progress=self.progress,
                            cancel=self.cancel_event.is_set,
                        )
                    names = {Path(op["source"]): Path(op["target"]).name for op in plan["inner"]}
                    for meta in work.meta:
                        path = meta["path"]
                        if meta["type"] == "root":
                            continue
                        original, target = plan["base"], final
                        for part in path.relative_to(plan["base"]).parts:
                            original /= part
                            target /= names.get(original, part)
                        meta["path"] = target
                    self.applied_output = final
                    _, data = nested.recover(work.root / storage.LOG_NAME)
                    data["gui_state"] = {
                        "meta": [{**item, "path": str(item["path"])} for item in work.meta],
                        "sources": work.sources,
                        "translations": work.translations,
                        "manual": sorted(work.manual),
                        "records": work.records,
                    }
                    nested.persist(data)
                    self.output_name = plan["output_name"]
                    self.message = "적용 완료: " + str(final)

                return self._start("이름 변경 적용", apply)
            if action in {"resume", "undo", "delete_record"}:
                log = self._get_log(payload["id"])

                def recovery():
                    if action == "delete_record":
                        actual_log, data = storage.recover(log)
                        storage.delete_records(actual_log, data)
                    else:
                        _, data = storage.recover(log)
                        if data.get("mode") == nested.MODE:
                            function = nested.resume if action == "resume" else nested.undo
                            function(
                                log,
                                lambda _: True,
                                progress=self.progress,
                                cancel=self.cancel_event.is_set,
                            )
                        else:
                            function = storage.resume if action == "resume" else storage.undo
                            function(log, lambda _: True)
                    self.message = "작업 기록 처리 완료. 폴더를 다시 선택해 현재 내용을 확인하세요."
                    self.work = None
                    self.candidate = None
                    self.preview_plan = None

                return self._start(
                    "남은 내용 이동" if action == "resume" else "복구 기록 처리", recovery
                )
            if action == "import_log":
                path = Path(payload["path"]).absolute()
                actual_log, data = storage.recover(path)
                if actual_log.name != storage.LOG_NAME:
                    raise ValueError("복구 로그를 선택하세요.")
                self.refresh_records()
                return {"id": data.get("run_id")}
            if action in {
                "install_model",
                "default_model",
                "delete_model",
                "model_details",
                "setup_ollama",
            }:
                name = payload.get("model")
                if name:
                    setup.validate_model(name)

                def model_action():
                    self.client = self.client_factory()
                    if action == "install_model":
                        with GuiSetupReport(self) as report:
                            setup.ensure_server(self.client, setup.find_ollama(), report)
                            setup.pull_model(self.client, name, report)
                            report.check()
                            models.set_default(name)
                    elif action == "default_model":
                        self.client.show(name)
                        models.set_default(name)
                    elif action == "delete_model":
                        result = self.client.delete(name)
                        if result.status != "success":
                            raise RuntimeError("모델 삭제에 실패했습니다.")
                        if models.config().get("model") == name:
                            models.set_default(None)
                    elif action == "model_details":
                        details = self.client.show(name)
                        self.message = str(details.details)
                    else:
                        args = SimpleNamespace(
                            model=None,
                            update_ollama=bool(payload.get("update")),
                            install_ollama=True,
                        )
                        with GuiSetupReport(self) as report:
                            setup.prepare(args, report, client=self.client)
                    self.refresh_models()
                    if self.work:
                        self.work.model = models.config().get("model") or ""
                        self._invalidate()

                return self._start(
                    "모델·Ollama 준비", model_action, hold_lock=action != "setup_ollama"
                )
            if action == "preference":
                source, desired = (
                    payload.get("source", "").strip(),
                    payload.get("desired", "").strip(),
                )
                if not source or not engine.valid_title_output(desired):
                    raise ValueError("원문과 원하는 번역을 선택하세요.")
                preferences = engine.load_preferences()
                preferences.setdefault("terms", {})[source] = desired
                with storage.Lock("user-data"):
                    storage.atomic_json(engine.PREFERENCES_PATH, preferences)
                engine.PREFERENCES = preferences
                engine.PREFERENCES_HASH = engine.sha256_bytes(engine.PREFERENCES_PATH.read_bytes())
                self._invalidate()
                return {}
            if action == "delete_preference":
                preferences = engine.load_preferences()
                preferences.setdefault("terms", {}).pop(payload["source"], None)
                with storage.Lock("user-data"):
                    storage.atomic_json(engine.PREFERENCES_PATH, preferences)
                engine.PREFERENCES = preferences
                engine.PREFERENCES_HASH = engine.sha256_bytes(engine.PREFERENCES_PATH.read_bytes())
                self._invalidate()
                return {}
            if action == "open_output":
                if not work:
                    raise ValueError("선택한 폴더이 없습니다.")
                output = work.root / self.output_name
                if output.parent != work.root or not output.is_dir():
                    raise ValueError("결과 폴더가 없습니다.")
                subprocess.Popen(["explorer.exe", str(output)])
                return {}
            raise ValueError("지원하지 않는 작업입니다.")
