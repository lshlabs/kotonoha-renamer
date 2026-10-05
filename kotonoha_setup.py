"""Optional Ollama/model bootstrap, always run as the original user by Setup."""

import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

import kotonoha_storage as storage

BASE = Path(__file__).resolve().parent
OLLAMA_DOWNLOAD = "https://ollama.com/download/OllamaSetup.exe"
LOCAL_SERVER = "http://127.0.0.1:11434"


@lru_cache(maxsize=1)
def _catalog():
    data = json.loads((BASE / "model-catalog.json").read_text(encoding="utf-8"))
    return tuple(
        {**item, "model": "hf.co/" + data["repository"] + ":" + item["quant"]}
        for item in data["models"]
    )


def catalog():
    """Read the bundled catalog once; callers receive independent entries."""
    return [item.copy() for item in _catalog()]


def validate_model(model):
    if model and model not in {item["model"] for item in catalog()}:
        raise ValueError("목록의 SuperGemma 모델을 선택하세요.")


class SetupCancelled(Exception):
    pass


class Reporter:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.text, self.percent, self.result = "준비 중", 0, "running"
        self.phase_started = time.monotonic()
        self.last_output = ""

    def cancelled(self):
        return self.path is not None and Path(str(self.path) + ".cancel").exists()

    def check(self):
        if self.cancelled():
            raise SetupCancelled("AI 준비 중단. 설치된 프로그램과 모델은 유지됩니다.")

    def publish(self):
        with self.lock:
            text = self.text
            if self.result == "running":
                text += f" · 경과 {int(time.monotonic() - self.phase_started)}초"
            if self.path:
                parser = configparser.ConfigParser(interpolation=None)
                parser["Progress"] = {
                    "text": text.replace("\n", " ").replace("\r", " "),
                    "percent": str(self.percent),
                    "result": self.result,
                }
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(self.path.name + ".tmp")
                with temporary.open("w", encoding="utf-16") as stream:
                    parser.write(stream)
                os.replace(temporary, self.path)
            if text != self.last_output:
                print(text, flush=True)
                self.last_output = text

    def update(self, text, percent=0, result="running"):
        with self.lock:
            if text.split(" / ")[0] != self.text.split(" / ")[0]:
                self.phase_started = time.monotonic()
            self.text, self.percent, self.result = text, max(0, min(100, int(percent))), result
            self.publish()

    def heartbeat(self):
        while not self.stop.wait(1):
            self.publish()

    def __enter__(self):
        self.publish()
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        if sys.stdout.isatty():
            print()


def find_ollama():
    candidates = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"]
    found = shutil.which("ollama")
    if found:
        candidates.append(Path(found))
    if os.name == "nt":
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    root,
                    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{44E83376-CE68-45EB-8FC1-393500EB558C}_is1",
                ) as key:
                    candidates.append(
                        Path(winreg.QueryValueEx(key, "InstallLocation")[0]) / "ollama.exe"
                    )
            except OSError:
                pass
    return next((path for path in candidates if path.is_file()), None)


def download_installer(path, report):
    report.update("Ollama 최신 설치 파일 다운로드")
    request = urllib.request.Request(OLLAMA_DOWNLOAD, headers={"User-Agent": "Kotonoha-Setup"})
    with urllib.request.urlopen(request, timeout=30) as response, path.open("xb") as output:
        if not response.url.startswith("https://"):
            raise RuntimeError("설치 파일 다운로드는 HTTPS만 허용합니다.")
        total = int(response.headers.get("Content-Length", 0))
        completed, last_update = 0, 0
        while True:
            report.check()
            block = response.read(1024 * 1024)
            if not block:
                break
            output.write(block)
            completed += len(block)
            if time.monotonic() - last_update >= 0.25:
                report.update(
                    f"Ollama 설치 파일 다운로드 / {completed / 1e6:.1f} MB"
                    + (f" / {total / 1e6:.1f} MB" if total else ""),
                    completed * 100 / total if total else 0,
                )
                last_update = time.monotonic()
        if not completed or (total and completed != total):
            raise RuntimeError("설치 파일 다운로드가 누락되었습니다.")


def verify_signature(path):
    powershell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = BASE / "verify-ollama-signature.ps1"
    if not script.is_file():
        script = BASE / "resources/verify-ollama-signature.ps1"
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InstallerPath",
            str(path),
        ],
        capture_output=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Ollama 서명 검증 실패. 설치를 중단합니다.")


def install_ollama(report):
    with tempfile.TemporaryDirectory(prefix="kotonoha-ollama-") as temporary:
        path = Path(temporary) / "OllamaSetup.exe"
        download_installer(path, report)
        report.update("Ollama 공식 디지털 서명 확인")
        verify_signature(path)
        report.check()
        report.update("Ollama 설치 중")
        marker = Path(os.environ["LOCALAPPDATA"]) / "Ollama/upgraded"
        marker.parent.mkdir(parents=True, exist_ok=True)
        # The official install script uses this marker so Ollama starts hidden.
        marker_created = not marker.exists()
        if marker_created:
            marker.touch(exist_ok=False)
        cancelled = False
        try:
            process = subprocess.Popen(
                [str(path), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            while True:
                try:
                    exit_code = process.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if report.cancelled() and not cancelled:
                        cancelled = True
                        report.update("중단 대기 · Ollama 설치 완료 후 중단")
                except KeyboardInterrupt:
                    cancelled = True
                    report.update("중단 대기 · Ollama 설치 완료 후 중단")
        except BaseException:
            if marker_created:
                marker.unlink(missing_ok=True)
            raise
        if exit_code not in (0, 3010):
            if marker_created:
                marker.unlink(missing_ok=True)
            raise RuntimeError(f"Ollama 설치 실패: 종료 코드 {exit_code}")
        if cancelled:
            raise SetupCancelled("AI 준비 중단. 설치된 Ollama와 모델은 유지됩니다.")
        report.check()


def server_ready(client):
    try:
        client.list()
        return True
    except Exception:
        return False


def ensure_server(client, executable, report):
    if server_ready(client):
        return
    if not executable:
        raise RuntimeError("Ollama가 없습니다. Ollama 설치를 선택하세요.")
    report.update("Ollama 서버 시작·응답 대기")
    log_path = storage.DATA_DIR / "ollama-setup-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(
            [str(executable), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        report.check()
        if server_ready(client):
            return
        time.sleep(1)
    raise RuntimeError("Ollama 서버가 응답하지 않습니다. 모델·설정에서 다시 시도하세요.")


def pull_model(client, model, report):
    try:
        client.show(model)
        report.update("설치된 모델 재사용", 100)
        return
    except Exception as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
    report.update("SuperGemma 모델 다운로드 시작 / " + model.rsplit(":", 1)[-1])
    success = False
    for event in client.pull(model, stream=True):
        report.check()
        if event.total:
            completed = event.completed or 0
            report.update(
                f"모델 파일 다운로드 / {completed / 1e9:.2f} GB / {event.total / 1e9:.2f} GB",
                completed * 100 / event.total,
            )
        else:
            report.update("모델 준비 / " + event.status)
        success = event.status == "success"
    if not success:
        raise RuntimeError("모델 다운로드 미완료. 재시도 시 이어받습니다.")
    client.show(model)


def prepare(args, report, client=None):
    validate_model(args.model)
    if not (args.install_ollama or args.update_ollama or args.model):
        raise ValueError("준비할 설치·모델 작업이 선택되지 않았습니다.")
    if os.name != "nt":
        raise RuntimeError("Ollama 자동 설치는 Windows 전용입니다.")
    import ctypes

    if ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("AI 준비는 Kotonoha를 일반 권한으로 실행해 주세요.")
    if client is None:
        from ollama import Client

        client = Client(host=LOCAL_SERVER, timeout=30)
    report.check()
    report.update("Ollama 확인 중")
    executable = find_ollama()
    online = server_ready(client)
    if args.update_ollama or (args.install_ollama and not executable and not online):
        if getattr(args, "no_ollama_install", False):
            raise RuntimeError("격리 테스트에서는 Ollama 설치·업데이트를 실행하지 않습니다.")
        install_ollama(report)
        executable = find_ollama()
    if args.model:
        ensure_server(client, executable, report)
        if getattr(args, "reuse_model", False):
            client.show(args.model)
            report.update("설치된 모델 재사용", 100)
        else:
            pull_model(client, args.model, report)
        report.check()
        with storage.Lock("user-data"):
            config_path = storage.DATA_DIR / "config.json"
            config = storage.read_json(config_path, {"version": 2, "model": None})
            config["model"] = args.model
            storage.atomic_json(config_path, config)
    report.update("AI 준비 완료", 100, "success")
