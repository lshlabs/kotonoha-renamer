"""UI-independent work state, translation plans and approved-result storage."""

import copy
import json

import kotonoha_engine as engine
import kotonoha_storage as storage


class WorkCancelled(Exception):
    cancelled = True


class WorkSession:
    def __init__(self, root, model, rename_root=True, prepare=True):
        self.root, self.model = root, model
        self.rename_root = rename_root
        self.correction_draft = []
        self.meta, self.sources = [], []
        self.translations, self.manual, self.corrections = {}, set(), []
        self.history, self.records = [], {}
        self.strength = 1
        self.names = {}
        self.scan()
        engine.MODEL = model
        engine.TRANSLATION_OPTIONS["temperature"] = 0.0
        if prepare:
            self.prepare()

    def input_key(self):
        cache = engine.load_proper_name_cache()
        if cache is None:
            return None
        confirmed = cache.get("confirmed", {})
        return engine.sha256_json(
            {
                "identity": storage.identity(self.root),
                "sources": self.sources,
                "model": self.model,
                "glossary": engine.GLOSSARY_HASH,
                "preferences": engine.PREFERENCES_HASH,
                "prompt": engine.PROMPT_VERSION,
                "options": engine.TRANSLATION_OPTIONS,
                "confirmed": {
                    k: v for k, v in confirmed.items() if any(k in title for title in self.sources)
                },
            }
        )

    def prepare(self):
        self.approved = storage.read_json(
            self.approved_path, {"version": 2, "works": {}, "namespaces": {}}
        )
        input_key = self.input_key()
        saved = next(
            (
                copy.deepcopy(value)
                for value in reversed(list(self.approved["works"].values()))
                if input_key is not None and value.get("input_key") == input_key
            ),
            None,
        )
        if saved is None and input_key is not None:
            confirmed = engine.load_proper_name_cache()["confirmed"]
            for key, value in reversed(list(self.approved["works"].items())):
                if "input_key" in value:
                    continue
                names = next(
                    (
                        record["names"]
                        for record in value.get("records", {}).values()
                        if isinstance(record.get("names"), dict)
                    ),
                    {},
                )
                self.names = names
                if key != self.work_key() or any(
                    names.get(name) != text
                    for name, text in confirmed.items()
                    if any(name in source for source in self.sources)
                ):
                    continue
                records = value.get("records", {})
                expected = {
                    k: v for k, v in engine.TRANSLATION_OPTIONS.items() if k != "temperature"
                }
                if any(
                    source not in value.get("manual", [])
                    and {
                        k: v
                        for k, v in records.get(source, {}).get("options", {}).items()
                        if k != "temperature"
                    }
                    != expected
                    for source in self.sources
                ):
                    continue
                saved = copy.deepcopy(value)
                saved.update(names=names, input_key=input_key)
                self.approved["works"][key] = saved
                break
            if saved is None:
                self.names = {}
        if saved:
            self.names = saved.get("names", {})
            self.translations = saved["translations"].copy()
            self.manual = set(saved.get("manual", []))
            self.corrections = saved.get("corrections", [])
            self.records = saved.get("records", {})
            self.strength = saved.get("strength", 1)
            self.report("이 작품에서 채택한 결과를 재사용합니다.")
        pending = [source for source in self.sources if source not in self.translations]
        if pending:
            self.generate_initial(pending)

    @engine.releases_model
    def generate_initial(self, pending):
        self.names, _ = engine.resolve_work_proper_names(self.sources)
        translated, records = self.translate(pending, (self.strength - 1) / 10, reason="initial")
        self.translations.update(translated)
        self.records.update(records)

    def incomplete(self):
        return [
            source
            for source in self.sources
            if source not in self.manual
            and (
                not engine.valid_title_output(self.translations.get(source))
                or engine.has_untranslated_japanese(self.translations.get(source, source))
            )
        ]

    def work_key(self):
        return engine.sha256_json(
            {
                "identity": storage.identity(self.root),
                "sources": self.sources,
                "model": self.model,
                "glossary": engine.GLOSSARY_HASH,
                "preferences": engine.PREFERENCES_HASH,
                "names": self.names,
                "prompt": engine.PROMPT_VERSION,
            }
        )

    def translate_with_progress(self, selected, temperature, correction, reason, progress):
        result, records = {}, {}
        pending_corrections = (
            correction if isinstance(correction, list) else [correction] if correction else []
        )
        pending = list(selected)
        ids = {i: source for i, source in enumerate(self.sources, 1)}
        for attempt in range(2):
            if not pending:
                break
            actual_temperature = temperature if not attempt else max(temperature, 0.2)
            context = [
                {
                    "id": i,
                    "source": source,
                    **(
                        {"translation": self.translations[source]}
                        if source in self.translations and source not in pending
                        else {}
                    ),
                }
                for i, source in ids.items()
            ]
            requested = {i for i, source in ids.items() if source in pending}
            prompt = (
                "작품 전체 문맥과 승인된 표기를 참고하여 번역 대상 ID만 translations로 출력하세요.\n"
                + engine.make_glossary_prompt(self.sources)
                + engine.make_proper_name_prompt(self.names)
                + "\n작품 전체 제목: "
                + json.dumps(context, ensure_ascii=False)
                + "\n번역 대상 ID: "
                + json.dumps(sorted(requested))
                + "\n이번 작품 교정 지시: "
                + json.dumps(self.corrections + pending_corrections, ensure_ascii=False)
            )
            if attempt:
                prompt += "\n누락·출력 형식 오류·일본어 잔존 항목만 다시 번역하세요."
            progress.request(
                requested,
                len(selected) - len(pending),
                lambda value: (
                    engine.valid_title_output(value) and not engine.has_untranslated_japanese(value)
                ),
                attempt + 1,
                round(actual_temperature * 10) + 1,
            )
            try:
                response = engine.ollama_chat_with_retry(
                    model=self.model,
                    think=False,
                    messages=[
                        {"role": "system", "content": engine.BASE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    format=engine.BATCH_OUTPUT_SCHEMA,
                    options={**engine.TRANSLATION_OPTIONS, "temperature": actual_temperature},
                    on_text=progress.text,
                )
                values = engine.parse_batch_results(response.message.content, requested)
            except WorkCancelled:
                raise
            except Exception as exc:
                self.report(f"번역 응답 오류: {exc}")
                values = {}
            remaining = []
            for i in sorted(requested):
                source, value = ids[i], values.get(i)
                if value is not None:
                    result[source] = value
                    records[source] = {
                        "model": self.model,
                        "temperature": actual_temperature,
                        "keep_alive": "1m",
                        "options": {
                            **engine.TRANSLATION_OPTIONS,
                            "temperature": actual_temperature,
                        },
                        "reason": "automatic_validation_retry" if attempt else reason,
                        "correction": correction,
                        "prompt": engine.PROMPT_VERSION,
                        "glossary": engine.GLOSSARY_HASH,
                        "preferences": engine.PREFERENCES_HASH,
                        "context": engine.sha256_json(context),
                        "names": copy.deepcopy(self.names),
                        "corrections": copy.deepcopy(self.corrections),
                        "translation": value,
                    }
                if value is None or engine.has_untranslated_japanese(value):
                    remaining.append(source)
            pending = remaining
        for source in pending:
            self.report(f"번역 미완료. 원문 또는 직전 결과를 유지합니다: {source}")
        progress.finish(
            len(selected) - len(pending),
            "번역 완료" if not pending else f"처리 종료 · 미완료 {len(pending)}개",
        )
        return result, records

    def plan(self):
        reserved, plan = set(), []
        incomplete = set(self.incomplete())
        title_ids = {title: index for index, title in enumerate(self.sources, 1)}
        for meta in self.meta:
            if meta["title"] in incomplete:
                continue
            path = meta["path"]
            translated = self.translations.get(meta["title"], meta["title"])
            new_stem = engine.sanitize_name(
                meta["prefix"] + translated,
                is_file_stem=meta["type"] == "file" and bool(path.suffix),
            )
            target = engine.unique_target(path, new_stem, reserved)
            if target.name != path.name:
                plan.append(
                    {
                        "type": meta["type"],
                        "source": path,
                        "target": target,
                        "identity": meta["identity"],
                        "title_id": title_ids[meta["title"]],
                    }
                )
        return plan

    def snapshot(self):
        return copy.deepcopy(
            (self.translations, self.manual, self.corrections, self.records, self.strength)
        )

    def save_accepted(self):
        # Generation namespaces retain actual options; approved work lookup is independent of retry strength.
        key = self.work_key()
        self.approved["works"][key] = copy.deepcopy(
            {
                "translations": self.translations,
                "manual": sorted(self.manual),
                "corrections": self.corrections,
                "records": self.records,
                "strength": self.strength,
                "names": self.names,
                "input_key": self.input_key(),
            }
        )
        for source, record in self.records.items():
            namespace = engine.sha256_json(
                {
                    "model": record.get("model", self.model),
                    "glossary": record.get("glossary", engine.GLOSSARY_HASH),
                    "preferences": record.get("preferences", engine.PREFERENCES_HASH),
                    "prompt": record.get("prompt", engine.PROMPT_VERSION),
                    **{
                        k: record.get(k)
                        for k in (
                            "options",
                            "temperature",
                            "keep_alive",
                            "correction",
                            "corrections",
                            "reason",
                        )
                    },
                }
            )
            payload = {
                "source": source,
                "work": key,
                "names": self.names,
                "context": record.get("context"),
                "result": self.translations.get(source),
            }
            self.approved["namespaces"].setdefault(namespace, {})[engine.sha256_json(payload)] = {
                "request": payload,
                "generation": record,
            }
        storage.atomic_json(self.approved_path, self.approved)

    @property
    def approved_path(self):
        return storage.DATA_DIR / "approved-works.json"

    def report(self, message):
        print(message)

    def scan(self):
        from kotonoha_paths import iter_work_paths, translation_title

        seen = set()
        for path, kind in iter_work_paths(self.root, self.rename_root):
            parts = translation_title(path, kind)
            if parts is None:
                continue
            prefix, title = parts
            self.meta.append(
                {
                    "path": path,
                    "type": kind,
                    "prefix": prefix,
                    "title": title,
                    "identity": storage.identity(path),
                }
            )
            if title not in seen:
                seen.add(title)
                self.sources.append(title)
        self.meta.sort(
            key=lambda m: (
                0 if m["type"] == "file" else 2 if m["type"] == "root" else 1,
                -len(m["path"].parts),
                str(m["path"]).lower(),
            )
        )

    @engine.releases_model
    def translate(self, selected, temperature, correction=None, reason="user_retry"):
        from kotonoha_progress import LiveProgress

        with LiveProgress(
            "제목 번역", self.model, total=len(selected), strength=round(temperature * 10) + 1
        ) as progress:
            return self.translate_with_progress(selected, temperature, correction, reason, progress)
