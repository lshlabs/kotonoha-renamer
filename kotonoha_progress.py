"""Live console status; counts only complete, valid streamed title objects."""

import json
import re
import sys
import threading
import time
import unicodedata


class ReceivedTitles:
    """Decode each completed title once from the accumulated response stream."""

    def __init__(self, requested, valid):
        self.requested, self.valid = requested, valid
        self.decoder = json.JSONDecoder()
        self.position, self.length = None, 0
        self.found, self.seen, self.duplicates = set(), set(), set()

    def update(self, text):
        if len(text) < self.length:
            self.position = None
            self.found.clear()
            self.seen.clear()
            self.duplicates.clear()
        self.length = len(text)
        if self.position is None:
            match = re.search(r'"translations"\s*:\s*\[', text)
            if not match:
                return set()
            self.position = match.end()
        while True:
            while self.position < len(text) and text[self.position] in " \t\r\n,":
                self.position += 1
            try:
                item, end = self.decoder.raw_decode(text, self.position)
            except ValueError:
                break
            self.position = end
            if not isinstance(item, dict):
                continue
            identifier = item.get("id")
            if type(identifier) is int and identifier in self.requested:
                if identifier in self.seen:
                    self.duplicates.add(identifier)
                self.seen.add(identifier)
                if self.valid(item.get("text")):
                    self.found.add(identifier)
        return self.found - self.duplicates


def received_ids(text, requested, valid):
    return ReceivedTitles(requested, valid).update(text)


class LiveProgress:
    def __init__(self, label, model, total=None, strength=None):
        self.label, self.model, self.total, self.strength = label, model, total, strength
        self.done, self.characters, self.state = 0, 0, "모델 응답 대기"
        self.requested, self.base, self.valid = set(), 0, lambda value: True
        self.received = ReceivedTitles(self.requested, self.valid)
        self.output = sys.stdout
        self.tty = self.output.isatty()
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.started = time.monotonic()
        self.last_log = 0
        self.line_width = 0

    def render(self, force=False):
        with self.lock:
            elapsed = int(time.monotonic() - self.started)
            if not self.tty and not force and elapsed - self.last_log < 5:
                return
            self.last_log = elapsed
            count = f"[ {self.done} / {self.total} ]" if self.total is not None else "[ 상태 ]"
            line = f"  {count} {self.state} · 경과 {elapsed}초"
            if self.characters:
                line += f" · 응답 {self.characters}자 수신"
            width = sum(
                0
                if unicodedata.combining(char)
                else 2
                if unicodedata.east_asian_width(char) in "WF"
                else 1
                for char in line
            )
            padding = " " * max(0, self.line_width - width) if self.tty else ""
            self.line_width = width
            print(
                ("\r" if self.tty else "") + line + padding,
                end="" if self.tty else "\n",
                file=self.output,
                flush=True,
            )

    def heartbeat(self):
        while not self.stop.wait(1):
            self.render()

    def __enter__(self):
        from kotonoha_models import label

        print(
            f"\n{'-' * 72}\n  {self.label}\n{'-' * 72}\n  모델 : {label(self.model)}",
            file=self.output,
            flush=True,
        )
        if self.strength is not None:
            print(f"  번역 강도 : [ {self.strength} / 10 ]", file=self.output, flush=True)
        if self.total is not None:
            print(
                f"  대상 : 제목 {self.total}개 (같은 제목의 파일·폴더는 함께 처리)\n",
                file=self.output,
                flush=True,
            )
        self.render(force=True)
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()
        return self

    def request(self, requested, base, valid, attempt, strength):
        with self.lock:
            self.requested, self.base, self.valid = requested, base, valid
            self.received = ReceivedTitles(requested, valid)
            self.done, self.characters = base, 0
            self.state = f"응답 대기 · 요청 {attempt}/2"
        if attempt > 1:
            print(
                f"\n  미완료 제목 자동 보완 : {len(requested)}개 / 사용 강도 [ {strength} / 10 ]",
                file=self.output,
                flush=True,
            )
        self.render(force=True)

    def text(self, text):
        with self.lock:
            before = self.done
            previous_characters = self.characters
            self.characters = len(text)
            if self.total is not None:
                self.done = self.base + len(self.received.update(text))
            self.state = "응답 수신" if text else "모델 응답 대기"
            changed = before != self.done or bool(text) != bool(previous_characters)
        if changed:
            self.render(force=True)

    def finish(self, done=None, state="응답 처리 완료"):
        with self.lock:
            if done is not None:
                self.done = done
            self.state = state

    def __exit__(self, kind, value, traceback):
        self.stop.set()
        self.thread.join()
        if kind:
            self.state = "작업 중단"
        self.render(force=True)
        if self.tty:
            print(file=self.output, flush=True)
