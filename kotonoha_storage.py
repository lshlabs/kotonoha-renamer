"""Process locks, atomic hidden logs, and a root-independent recovery journal."""

import ctypes
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path


def default_data_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "data"
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Kotonoha"


DATA_DIR = Path(os.environ.get("KOTONOHA_DATA_DIR", str(default_data_dir())))
LOG_NAME = "rename-log.json"


class PendingRecovery(RuntimeError):
    def __init__(self, log):
        self.log = Path(log)
        super().__init__(
            f"기존 미해결 복구 기록이 있습니다: {self.log}\n"
            "작업 기록에서 복구하거나 기존 작업을 확인하세요."
        )


class RootRenameBlocked(RuntimeError):
    def __init__(self, data, direction):
        self.root = actual_root(data)
        self.log = self.root / LOG_NAME
        self.direction = direction
        completed = len(data.get("completed_operations", []))
        super().__init__(
            f"작품 폴더 자체의 이름 변경이 Windows 잠금으로 보류됐습니다.\n"
            f"완료된 적용: {completed}개 / 복구 로그: {self.log}"
        )


def report_root_lock(data, direction, error):
    pending = data.get("pending_operation", {})
    if (
        not data.get("root_transfer")
        and pending.get("type") == "root"
        and getattr(error, "winerror", None) in {32, 33}
    ):
        raise RootRenameBlocked(data, direction) from error


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def hide(path):
    if os.name != "nt":
        return
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    api.GetFileAttributesW.restype = ctypes.c_uint32
    api.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    attributes = api.GetFileAttributesW(str(path))
    if attributes == 0xFFFFFFFF or not api.SetFileAttributesW(str(path), attributes | 2):
        raise ctypes.WinError(ctypes.get_last_error())


def atomic_json(path, data, hidden=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if hidden:
            try:
                hide(temp)
            except OSError as exc:
                print(f"경고: 임시 복구 로그를 숨기지 못했습니다: {exc}")
        temp.replace(path)
        if hidden:
            try:
                hide(path)
            except OSError as exc:
                print(
                    f"경고: 복구 로그를 숨기지 못했습니다. 가시 로그를 보존합니다: {path} ({exc})"
                )
    finally:
        if temp.exists():
            temp.unlink()


class Lock:
    """OS-held lock; a killed process releases it without stale lock deletion."""

    def __init__(self, key):
        self.path = DATA_DIR / "locks" / (key + ".lock")
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            self.stream.seek(0)
            if not self.stream.read(1):
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError(
                "다른 Kotonoha 실행이 진행 중입니다. 해당 실행을 종료한 뒤 다시 실행하세요."
            )
        return self

    def __exit__(self, *args):
        self.stream.close()


def identity(path):
    stat = Path(path).stat()
    if not stat.st_ino:
        raise RuntimeError(f"파일 식별자를 읽을 수 없습니다: {path}")
    return {"device": stat.st_dev, "inode": stat.st_ino}


def actual_root(data):
    validate_data(data)
    original, planned = Path(data["root"]), Path(data["planned_root"])
    transfer = data.get("root_transfer")
    if transfer:
        if not original.is_dir() or identity(original) != data["root_identity"]:
            raise RuntimeError("원본 폴더가 교체되거나 사라졌습니다. 이동 기록을 보존하세요.")
        destination = planned if planned.exists() else Path(transfer["staging_path"])
        if not destination.is_dir() or identity(destination) != transfer["identity"]:
            raise RuntimeError("이동 대상 폴더의 식별자가 다릅니다. 이동 기록을 보존하세요.")
        return planned if transfer["active"] == "target" else original
    candidates = list(dict.fromkeys([original, planned]))
    existing = [p for p in candidates if p.exists()]
    if len(existing) != 1 or identity(existing[0]) != data["root_identity"]:
        raise RuntimeError(
            "루트 경로 또는 식별자가 모호합니다. 주 로그와 복구 저널을 보존하고 수동 확인하세요."
        )
    return existing[0]


def journal_path(data):
    if not re.fullmatch(r"[a-f0-9]{32}", data["run_id"]):
        raise RuntimeError("복구 기록의 실행 ID 형식이 잘못됐습니다.")
    return DATA_DIR / "recovery" / (data["run_id"] + ".json")


def read_recovery_journal(main):
    destination = journal_path(main)
    journal = read_json(destination)
    if journal is not None:
        return journal
    previous = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Kotonoha"
    if previous.resolve() == DATA_DIR.resolve():
        return None
    candidate = read_json(previous / "recovery" / destination.name)
    if candidate is None:
        return None
    if any(
        candidate.get(key) != main.get(key)
        for key in ("run_id", "root", "planned_root", "root_identity", "mode")
    ):
        raise RuntimeError("이전 복구 저널과 선택한 로그가 다릅니다. 기록을 유지하세요.")
    if main.get("revision", 0) > candidate.get("revision", 0):
        raise RuntimeError("이전 복구 저널보다 주 로그가 최신입니다. 기록을 유지하세요.")
    if candidate.get("mode") == "nested_output":
        from kotonoha_nested import checked_root

        checked_root(candidate)
    else:
        actual_root(candidate)
    atomic_json(destination, candidate)
    return candidate


def validate_data(data):
    original, planned = Path(data["root"]), Path(data["planned_root"])
    if (
        not original.is_absolute()
        or not planned.is_absolute()
        or ".." in original.parts
        or ".." in planned.parts
        or original.parent != planned.parent
    ):
        raise RuntimeError("복구 기록의 루트 경로 범위가 잘못됐습니다.")
    transfer = data.get("root_transfer")
    if transfer:
        staging = Path(transfer["staging_path"])
        if (
            staging.parent != original.parent
            or not staging.name.startswith(".kotonoha-transfer-")
            or transfer["active"] not in {"original", "target"}
            or original == planned
            or staging in {original, planned}
        ):
            raise RuntimeError("폴더 이동 기록의 경로가 잘못됐습니다.")
        pending_transfer = transfer.get("pending")
        if pending_transfer and (
            not isinstance(pending_transfer.get("index"), int)
            or not 0 <= pending_transfer["index"] < len(transfer["entries"])
        ):
            raise RuntimeError("중단된 이동 항목 번호가 잘못됐습니다.")
        names = [entry["name"] for entry in transfer["entries"]]
        if len(names) != len(set(names)) or any(
            name in {"", ".", "..", LOG_NAME}
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            for name in names
        ):
            raise RuntimeError("폴더 이동 기록의 항목이 잘못됐습니다.")
    operations = (
        data.get("planned_operations", [])
        + data.get("completed_operations", [])
        + data.get("undo_operations", [])
    )
    if data.get("pending_operation"):
        operations.append(data["pending_operation"])
    for op in operations:
        source, target = Path(op["source"]), Path(op["target"])
        if op["type"] == "root":
            valid = source == original and target == planned and source != target
        else:
            valid = (
                op["type"] in {"file", "folder"}
                and original in source.parents
                and source.parent == target.parent
                and source != target
                and ".." not in source.parts
                and ".." not in target.parts
            )
        if not valid:
            raise RuntimeError(
                "복구 작업이 작품 폴더 범위를 벗어납니다. 로그를 보존하고 수동 확인하세요."
            )


def persist(data):
    """Journal is authoritative: commit it first, then mirror inside actual root.

    Every rename has a durable pending snapshot. If the process exits after a
    root move, actual_root locates the same directory by identity. A newer journal
    repairs a stale/missing main log; no write ever recreates a vanished root.
    """
    root = actual_root(data)
    data["revision"] = data.get("revision", 0) + 1
    data["actual_root"] = str(root)
    atomic_json(journal_path(data), data)
    if os.environ.get("KOTONOHA_TEST_PERSIST_CRASH") == f"journal:{data['revision']}":
        os._exit(99)
    atomic_json(root / LOG_NAME, data, hidden=True)
    if os.environ.get("KOTONOHA_TEST_PERSIST_CRASH") == f"main:{data['revision']}":
        os._exit(99)
    return root / LOG_NAME


def recover(log_path):
    log_path = Path(log_path).absolute()
    main = read_json(log_path)
    if main and main.get("mode") == "nested_output":
        import kotonoha_nested

        return kotonoha_nested.recover(log_path)
    journals = []
    if main and main.get("run_id"):
        journal = read_recovery_journal(main)
        if journal:
            journals.append(journal)
    else:
        for path in (DATA_DIR / "recovery").glob("*.json"):
            data = read_json(path)
            if str(log_path.parent) in {data["root"], data["planned_root"]}:
                journals.append(data)
    active = [d for d in journals if d.get("status") != "undone"]
    if active:
        journals = active
    if len(journals) > 1:
        raise RuntimeError("여러 복구 저널이 발견됐습니다. 로그를 보존하고 수동 확인하세요.")
    if journals:
        journal = journals[0]
        if journal.get("mode") == "nested_output":
            import kotonoha_nested

            return kotonoha_nested.recover(log_path)
        if main and main.get("run_id") != journal["run_id"]:
            raise RuntimeError("주 로그와 복구 저널의 실행 ID가 다릅니다.")
        if main and main.get("revision", 0) > journal["revision"]:
            raise RuntimeError("주 로그가 복구 저널보다 최신입니다. 수동 확인이 필요합니다.")
        root = actual_root(journal)
        if main and main.get("root_identity") != journal["root_identity"]:
            raise RuntimeError("주 로그와 저널의 루트 식별자가 다릅니다.")
        atomic_json(root / LOG_NAME, journal, hidden=True)
        return root / LOG_NAME, journal
    if main:
        if main.get("run_id"):
            raise RuntimeError("복구 저널이 없습니다. 주 로그를 보존하고 수동 확인하세요.")
        return log_path, main  # Legacy script log (now released as v0.1.0).
    raise FileNotFoundError(f"복구 로그를 찾을 수 없습니다: {log_path}")


def reconcile(data):
    pending = data.get("pending_operation")
    if not pending:
        return
    direction = pending["direction"]
    if data.get("root_transfer") and pending["type"] == "root":
        reconcile_transfer(data)
        expected = "target" if direction == "apply" else "original"
        if data["root_transfer"]["active"] == expected:
            field = "completed_operations" if direction == "apply" else "undo_operations"
            data.setdefault(field, []).append(
                {k: pending[k] for k in ("type", "source", "target", "identity")}
            )
        data.pop("pending_operation")
        persist(data)
        return
    if direction not in {"apply", "undo"}:
        raise RuntimeError("알 수 없는 pending 작업 방향입니다.")
    before = Path(pending["source"] if direction == "apply" else pending["target"])
    after = Path(pending["target"] if direction == "apply" else pending["source"])
    if before.exists() == after.exists():
        raise RuntimeError(f"중단된 작업 상태가 모호합니다: {before} / {after}")
    actual = after if after.exists() else before
    if identity(actual) != pending["identity"]:
        raise RuntimeError(f"중단된 작업의 식별자가 다릅니다: {actual}")
    if after.exists():
        field = "completed_operations" if direction == "apply" else "undo_operations"
        data.setdefault(field, []).append(
            {k: pending[k] for k in ("type", "source", "target", "identity")}
        )
    data.pop("pending_operation")
    persist(data)


def release_cwd(source):
    current = Path.cwd()
    if current == source or source in current.parents:
        os.chdir(source.parent)


def checkpoint(stage, direction, index):
    """Fault injection only enabled by tests, never exposed as a user option."""
    if os.environ.get("KOTONOHA_TEST_CRASH") == f"{direction}:{stage}:{index}":
        os._exit(99)


def rename_operation(data, op, direction, index):
    before = Path(op["source"] if direction == "apply" else op["target"])
    after = Path(op["target"] if direction == "apply" else op["source"])
    if op["type"] == "root" and data.get("root_transfer"):
        pending = {**op, "direction": direction, "identity": data["root_identity"]}
        data["pending_operation"] = pending
        persist(data)
        transfer_contents(data, direction)
        field = "completed_operations" if direction == "apply" else "undo_operations"
        data.setdefault(field, []).append(
            {k: pending[k] for k in ("type", "source", "target", "identity")}
        )
        data.pop("pending_operation")
        persist(data)
        return
    if not before.exists() or after.exists():
        raise RuntimeError(f"원본 누락 또는 대상 충돌: {before} → {after}")
    current_identity = identity(before)
    expected = op.get("identity")
    if expected and current_identity != expected:
        raise RuntimeError(f"파일 식별자가 다릅니다: {before}")
    pending = {**op, "direction": direction, "identity": current_identity}
    data["pending_operation"] = pending
    persist(data)
    checkpoint("before_rename", direction, index)
    if op["type"] == "root":
        release_cwd(before)
    before.rename(after)
    checkpoint("after_rename", direction, index)
    field = "completed_operations" if direction == "apply" else "undo_operations"
    data.setdefault(field, []).append(
        {k: pending[k] for k in ("type", "source", "target", "identity")}
    )
    data.pop("pending_operation")
    persist(data)
    checkpoint("after_commit", direction, index)


def reconcile_transfer(data):
    transfer = data["root_transfer"]
    pending = transfer.get("pending")
    if not pending:
        return
    entry = transfer["entries"][pending["index"]]
    original = Path(data["root"]) / entry["name"]
    target = Path(data["planned_root"]) / entry["name"]
    if original.exists() == target.exists():
        raise RuntimeError(f"중단된 파일 이동 상태가 모호합니다: {entry['name']}")
    current = target if target.exists() else original
    if identity(current) != entry["identity"]:
        raise RuntimeError(f"이동 항목이 교체됐습니다: {current}")
    entry["moved"] = target.exists()
    transfer.pop("pending")
    persist(data)


def transfer_contents(data, direction):
    transfer = data["root_transfer"]
    original, target = Path(data["root"]), Path(data["planned_root"])
    actual_root(data)
    if not target.exists():
        Path(transfer["staging_path"]).rename(target)
    reconcile_transfer(data)
    expected_names = {entry["name"] for entry in transfer["entries"]}
    current_names = {
        path.name for root in (original, target) for path in root.iterdir() if path.name != LOG_NAME
    }
    if current_names != expected_names:
        raise RuntimeError("이동 시작 후 폴더 내용이 변경됐습니다. 복구 기록을 보존하세요.")
    for entry in transfer["entries"]:
        current = (target if entry["moved"] else original) / entry["name"]
        other = (original if entry["moved"] else target) / entry["name"]
        if not current.exists() or other.exists() or identity(current) != entry["identity"]:
            raise RuntimeError(f"이동 항목 교체 또는 대상 충돌: {current}")
    moving = direction == "apply"
    entries = list(enumerate(transfer["entries"]))
    if not moving:
        entries.reverse()
    for index, entry in entries:
        if entry["moved"] == moving:
            continue
        before = (original if moving else target) / entry["name"]
        after = (target if moving else original) / entry["name"]
        if not before.exists() or after.exists() or identity(before) != entry["identity"]:
            raise RuntimeError(f"원본 교체 또는 이동 대상 충돌: {before} → {after}")
        transfer["pending"] = {"index": index}
        persist(data)
        checkpoint("before_transfer", direction, index + 1)
        release_cwd(before)
        try:
            before.rename(after)
        except OSError as exc:
            raise RuntimeError(
                f"내용 이동을 중단했습니다: {before}\n"
                f"이 항목을 사용 중인 프로그램을 닫은 뒤 작업 기록에서 계속하거나 복구하세요.\n"
                f"복구 로그: {actual_root(data) / LOG_NAME}"
            ) from exc
        checkpoint("after_transfer", direction, index + 1)
        entry["moved"] = moving
        transfer.pop("pending")
        persist(data)
        show_progress(
            sum(item["moved"] == moving for item in transfer["entries"]),
            len(entries),
            "내용 이동" if moving else "내용 복구",
        )
    transfer["active"] = "target" if moving else "original"
    persist(data)


def move_root_contents(log, confirm):
    """Opt-in fallback for an existing blocked root operation, with durable recovery."""
    log, data = recover(log)
    reconcile(data)
    remaining = [
        op
        for op in data["planned_operations"]
        if (op["source"], op["target"])
        not in {(done["source"], done["target"]) for done in data["completed_operations"]}
    ]
    if (
        data.get("undo_operations")
        or data["status"].startswith("undo")
        or (len(remaining) != 1 or remaining[0]["type"] != "root")
    ):
        raise RuntimeError("내부 이름 변경이 끝나고 작품 폴더 이름만 남았을 때 사용할 수 있습니다.")
    original, target = Path(data["root"]), Path(data["planned_root"])
    print(f"새 폴더: {target}\n내용을 이동하며 원본 폴더는 남습니다. 재번역하지 않습니다.")
    if not confirm("새 폴더로 내용물을 이동하시겠습니까? [y/N]: "):
        return actual_root(data), log, data
    if not data.get("root_transfer"):
        if target.exists():
            raise RuntimeError(f"같은 이름의 폴더가 이미 있습니다: {target}")
        entries = []
        for child in sorted(original.iterdir()):
            if child.name == LOG_NAME:
                continue
            if child.is_symlink() or getattr(child.lstat(), "st_file_attributes", 0) & 1024:
                raise RuntimeError(f"링크 항목은 자동 이동하지 않습니다: {child}")
            entries.append({"name": child.name, "identity": identity(child), "moved": False})
        staging = Path(tempfile.mkdtemp(prefix=".kotonoha-transfer-", dir=original.parent))
        data["root_transfer"] = {
            "staging_path": str(staging),
            "identity": identity(staging),
            "active": "original",
            "entries": entries,
        }
        persist(data)
    # resume uses the same original plan; root operation now transfers children.
    return resume(log, lambda _: True)


def check_existing(root):
    log = root / LOG_NAME
    journal_exists = any(
        str(root) in {d["root"], d["planned_root"]} and d.get("status") != "undone"
        for d in (read_json(p) for p in (DATA_DIR / "recovery").glob("*.json"))
    )
    if log.exists() or journal_exists:
        log, data = recover(log)
        if data.get("status") != "undone":
            raise PendingRecovery(log)


def show_progress(done, total, label):
    text = f"  [ {done} / {total} ] {label}"
    print(
        ("\r" if sys.stdout.isatty() else "") + text,
        end="" if sys.stdout.isatty() and done < total else "\n",
        flush=True,
    )


def apply(plan, root, metadata=None):
    check_existing(root)
    # Revalidate every source/target before writing the first pending operation.
    for op in plan:
        if not op["source"].exists() or op["target"].exists():
            raise RuntimeError(f"미리보기 이후 경로가 변경되거나 충돌했습니다: {op['source']}")
        if identity(op["source"]) != op["identity"]:
            raise RuntimeError(f"미리보기 이후 원본이 교체됐습니다: {op['source']}")
    log = root / LOG_NAME
    if log.exists():
        archive = log.with_name("rename-log.undone-" + uuid.uuid4().hex + ".json")
        log.replace(archive)
        try:
            hide(archive)
        except OSError as exc:
            print(f"경고: archive 숨김 실패: {archive} ({exc})")
    operations = [{**op, "source": str(op["source"]), "target": str(op["target"])} for op in plan]
    planned_root = next((op["target"] for op in operations if op["type"] == "root"), str(root))
    data = {
        "version": 3,
        "run_id": uuid.uuid4().hex,
        "status": "in_progress",
        "root": str(root),
        "planned_root": planned_root,
        "root_identity": identity(root),
        "planned_operations": operations,
        "completed_operations": [],
        "undo_operations": [],
        "translation": metadata or {},
    }
    persist(data)
    try:
        for index, op in enumerate(operations, 1):
            rename_operation(data, op, "apply", index)
            show_progress(index, len(operations), "이름 변경")
        data["status"] = "completed"
        log = persist(data)
    except BaseException as exc:
        data["status"] = "partial_failure"
        data["error"] = str(exc)
        persist(data)
        report_root_lock(data, "apply", exc)
        raise
    return actual_root(data), log, data


def resume(log, confirm):
    """Continue the exact durable plan, skipping operations already committed."""
    log, data = recover(log)
    if data.get("mode") == "nested_output":
        import kotonoha_nested

        return kotonoha_nested.resume(log, confirm)
    if not data.get("run_id"):
        raise RuntimeError(
            "이전 스크립트 로그의 적용 재개는 지원하지 않습니다. 작업 기록에서 복구하세요."
        )
    if data.get("status", "").startswith("undo") or data.get("undo_operations"):
        raise RuntimeError(
            "복구가 시작된 작업은 적용 재개할 수 없습니다. 작업 기록에서 복구를 계속하세요."
        )
    reconcile(data)
    if data["status"] == "completed":
        print("이미 모든 이름 변경이 완료됐습니다.")
        return actual_root(data), log, data
    done = {(op["source"], op["target"]) for op in data["completed_operations"]}
    operations = [
        (index, op)
        for index, op in enumerate(data["planned_operations"], 1)
        if (op["source"], op["target"]) not in done
    ]
    print(f"이미 완료: {len(done)}개 / 남은 변경: {len(operations)}개 (재번역 없음)")
    for _, op in operations:
        print(f"  {op['source']}\n  → {op['target']}")
    if operations and not confirm("남은 이름 변경을 적용하시겠습니까? [y/N]: "):
        return actual_root(data), log, data
    try:
        data["status"] = "in_progress"
        data.pop("error", None)
        persist(data)
        for index, op in operations:
            rename_operation(data, op, "apply", index)
            show_progress(
                len(data["completed_operations"]), len(data["planned_operations"]), "남은 이름 변경"
            )
        data["status"] = "completed"
        log = persist(data)
    except BaseException as exc:
        data["status"] = "partial_failure"
        data["error"] = str(exc)
        persist(data)
        report_root_lock(data, "apply", exc)
        raise
    print("남은 이름 변경이 완료됐습니다. 최종 폴더에서 터미널을 다시 여세요.")
    return actual_root(data), log, data


def undo(log, confirm):
    log, data = recover(log)
    if data.get("mode") == "nested_output":
        import kotonoha_nested

        return kotonoha_nested.undo(log, confirm)
    if not data.get("run_id"):
        raise RuntimeError(
            "이전 스크립트의 복구 로그는 Kotonoha Renamer CLI에서 복구하세요: https://github.com/lshlabs/kotonoha-renamer-cli"
        )
    reconcile(data)
    done = {(op["source"], op["target"]) for op in data.get("undo_operations", [])}
    operations = [
        op
        for op in reversed(data["completed_operations"])
        if (op["source"], op["target"]) not in done
    ]
    for op in operations:
        print(f"  {op['target']}\n  → {op['source']}")
    partial_transfer = data.get("root_transfer") and not any(
        op["type"] == "root" for op in data["completed_operations"]
    )
    if partial_transfer:
        print("새 폴더로 옮긴 내용도 원본 폴더로 되돌립니다.")
    if (operations or partial_transfer) and not confirm("위 이름으로 복구하시겠습니까? [y/N]: "):
        return actual_root(data), log
    try:
        data["status"] = "undo_in_progress"
        persist(data)
        if partial_transfer:
            transfer_contents(data, "undo")
        for index, op in enumerate(operations, 1):
            rename_operation(data, op, "undo", index)
            show_progress(index, len(operations), "원래 이름 복구")
        data["status"] = "undone"
        log = persist(data)
    except BaseException as exc:
        data["status"] = "undo_partial_failure"
        data["error"] = str(exc)
        persist(data)
        report_root_lock(data, "undo", exc)
        raise
    print("원래 이름으로 복구 완료.")
    return actual_root(data), log


def delete_records(log, data):
    if data["status"] != "completed":
        raise RuntimeError("실패하거나 중단된 실행의 복구 기록은 삭제할 수 없습니다.")
    # Main first: a failed journal deletion retains enough data for recovery.
    paths = [log]
    if data.get("root_transfer"):
        other = (
            Path(data["root"] if log.parent == Path(data["planned_root"]) else data["planned_root"])
            / LOG_NAME
        )
        mirror = read_json(other)
        if mirror and mirror.get("run_id") == data["run_id"]:
            paths.append(other)
    paths.append(journal_path(data))
    for path in paths:
        try:
            path.unlink()
        except OSError as exc:
            print(f"복구 기록 삭제 실패. 남은 파일: {path} ({exc})")
            break
