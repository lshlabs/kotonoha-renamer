"""Journalled output inside the original directory; never rename the work root."""

import os
import uuid
from pathlib import Path

import kotonoha_storage as storage

MODE = "nested_output"


class Cancelled(RuntimeError):
    pass


def checked_root(data):
    root, output = Path(data["root"]), Path(data["planned_root"])
    if not root.is_absolute() or ".." in root.parts or output.parent != root or output == root:
        raise RuntimeError("결과 폴더의 경로 범위가 잘못됐습니다.")
    if (
        storage.identity(root) != data["root_identity"]
        or root.is_symlink()
        or getattr(root.lstat(), "st_file_attributes", 0) & 1024
    ):
        raise RuntimeError("원본 폴더가 교체됐습니다. 복구 기록을 유지하세요.")
    initial_output = Path(data.get("initial_output", data["planned_root"]))
    for operation in data["planned_operations"]:
        before, after = Path(operation["source"]), Path(operation["target"])
        kind = operation["type"]
        if kind == "output":
            valid = (
                before == root / (".kotonoha-output-" + data["run_id"]) and after == initial_output
            )
        elif kind == "move":
            valid = (
                before.parent == root
                and after.parent == initial_output
                and before.name == after.name
            )
        elif kind == "result":
            valid = (
                before.parent == root
                and after.parent == root
                and operation["identity"] == data["output_identity"]
            )
        else:
            valid = (
                kind in {"file", "folder"}
                and root in before.parents
                and before.parent == after.parent
            )
        if not valid or before == after or ".." in before.parts or ".." in after.parts:
            raise RuntimeError("복구 작업이 허용된 폴더 범위를 벗어납니다.")
    allowed = {
        (op["type"], op["source"], op["target"]): op["identity"]
        for op in data["planned_operations"]
    }
    records = data.get("completed_operations", []) + data.get("undo_operations", [])
    if data.get("pending_operation"):
        records.append(data["pending_operation"])
    for operation in records:
        key = (operation["type"], operation["source"], operation["target"])
        if key not in allowed or operation["identity"] != allowed[key]:
            raise RuntimeError("복구 기록에 계획하지 않은 작업이 있습니다.")
    if data["status"] == "completed" and (
        not output.is_dir() or storage.identity(output) != data["output_identity"]
    ):
        raise RuntimeError("결과 폴더가 이동 또는 교체됐습니다. 기록을 유지하세요.")
    return root


def persist(data):
    root = checked_root(data)
    data["revision"] = data.get("revision", 0) + 1
    storage.atomic_json(storage.journal_path(data), data)
    if os.environ.get("KOTONOHA_TEST_PERSIST_CRASH") == f"journal:{data['revision']}":
        os._exit(99)
    log = root / storage.LOG_NAME
    storage.atomic_json(log, data, hidden=True)
    if os.environ.get("KOTONOHA_TEST_PERSIST_CRASH") == f"main:{data['revision']}":
        os._exit(99)
    return log


def recover(log):
    log = Path(log).absolute()
    main = storage.read_json(log)
    if main and main.get("mode") == MODE:
        journal = storage.read_recovery_journal(main)
        candidates = [journal] if journal else []
    else:
        candidates = [
            storage.read_json(path) for path in (storage.DATA_DIR / "recovery").glob("*.json")
        ]
        candidates = [
            data
            for data in candidates
            if data
            and data.get("mode") == MODE
            and log.parent in {Path(data["root"]), Path(data["planned_root"])}
        ]
        active = [data for data in candidates if data.get("status") != "undone"]
        candidates = active or candidates
    if len(candidates) != 1:
        raise RuntimeError("복구 저널이 없거나 여러 개입니다. 기록을 유지하세요.")
    data = candidates[0]
    root = checked_root(data)
    if main and (
        main.get("run_id") != data["run_id"] or main.get("revision", 0) > data["revision"]
    ):
        raise RuntimeError("주 로그와 복구 저널이 일치하지 않습니다.")
    current = root / storage.LOG_NAME
    existing = storage.read_json(current, {})
    if existing.get("run_id") not in {None, data["run_id"]}:
        if data["status"] == "undone":
            return storage.journal_path(data), data
        raise RuntimeError("원본 폴더에 다른 작업 기록이 있습니다. 기록을 유지하세요.")
    storage.atomic_json(current, data, hidden=True)
    return current, data


def reconcile(data):
    pending = data.get("pending_operation")
    if not pending:
        return
    direction = pending["direction"]
    if direction not in {"apply", "undo"}:
        raise RuntimeError("알 수 없는 복구 방향입니다.")
    before = Path(pending["source"] if direction == "apply" else pending["target"])
    after = Path(pending["target"] if direction == "apply" else pending["source"])
    if before.exists() == after.exists():
        raise RuntimeError(f"중단된 작업 상태가 모호합니다: {before} / {after}")
    current = after if after.exists() else before
    if storage.identity(current) != pending["identity"]:
        raise RuntimeError(f"중단된 항목이 교체됐습니다: {current}")
    if after.exists():
        field = "completed_operations" if direction == "apply" else "undo_operations"
        data[field].append({key: pending[key] for key in ("type", "source", "target", "identity")})
    data.pop("pending_operation")
    persist(data)


def execute(data, op, direction, index):
    checked_root(data)
    before = Path(op["source"] if direction == "apply" else op["target"])
    after = Path(op["target"] if direction == "apply" else op["source"])
    if not before.exists() or after.exists() or storage.identity(before) != op["identity"]:
        raise RuntimeError(f"원본 교체 또는 대상 충돌: {before} → {after}")
    if op["type"] == "output" and direction == "undo" and any(before.iterdir()):
        data["undo_operations"].append(dict(op))
        persist(data)
        return  # Preserve new user files and the output folder in place.
    data["pending_operation"] = {**op, "direction": direction}
    persist(data)
    storage.checkpoint("before_rename", direction, index)
    storage.release_cwd(before)
    before.rename(after)
    storage.checkpoint("after_rename", direction, index)
    field = "completed_operations" if direction == "apply" else "undo_operations"
    data[field].append({key: op[key] for key in ("type", "source", "target", "identity")})
    data.pop("pending_operation")
    persist(data)
    storage.checkpoint("after_commit", direction, index)


def content_entries(root):
    from kotonoha_engine import should_skip_file
    from kotonoha_paths import is_link

    entries = []
    for child in sorted(Path(root).iterdir()):
        if child.name.startswith((".kotonoha-", "rename-log.")) or should_skip_file(child):
            continue
        if is_link(child):
            raise RuntimeError(f"링크·junction 항목은 이동하지 않습니다: {child}")
        entries.append({"path": str(child), "identity": storage.identity(child)})
    return entries


def preview(root, inner_plan, output_name, entries):
    root = Path(root)
    output = root / output_name
    if (
        not output_name
        or output_name in {".", ".."}
        or output_name.startswith((".kotonoha-", "rename-log."))
        or Path(output_name).name != output_name
        or output.parent != root
    ):
        raise ValueError("결과 폴더 이름을 확인하세요.")
    if output.exists():
        raise RuntimeError(f"같은 이름의 하위 폴더가 있습니다: {output}")
    top_names = {str(op["source"]): Path(op["target"]).name for op in inner_plan}
    moves = []
    for entry in entries:
        source = Path(entry["path"])
        name = top_names.get(str(source), source.name)
        if name == output_name:
            raise RuntimeError("결과 폴더 이름이 내부 항목과 같습니다. 다른 이름을 지정하세요.")
        moves.append(
            {
                "type": "move",
                "source": str(root / name),
                "target": str(output / name),
                "identity": entry["identity"],
            }
        )
    return output, moves


def final_changes(root, inner_plan, output, entries):
    """Show original paths and final locations after all ancestor renames."""
    root = Path(root)
    names = {Path(op["source"]): Path(op["target"]).name for op in inner_plan}
    sources = {Path(op["source"]): op["type"] for op in inner_plan}
    sources.update({Path(entry["path"]): "move" for entry in entries})
    changes = []
    for source, kind in sources.items():
        original, target = root, Path(output)
        for part in source.relative_to(root).parts:
            original /= part
            target /= names.get(original, part)
        changes.append({"source": str(source), "target": str(target), "type": kind})
    return changes


def apply(root, inner_plan, output_name, entries, metadata=None, progress=None, cancel=None):
    root = Path(root)
    storage.check_existing(root)
    output, moves = preview(root, inner_plan, output_name, entries)
    for entry in entries:
        source = Path(entry["path"])
        if not source.exists() or storage.identity(source) != entry["identity"]:
            raise RuntimeError(f"스캔 이후 항목이 교체됐습니다: {source}")
    if content_entries(root) != entries:
        raise RuntimeError("스캔 이후 폴더 내용이 변경됐습니다. 다시 선택하세요.")
    for op in inner_plan:
        if op["type"] == "root" or not Path(op["source"]).exists() or Path(op["target"]).exists():
            raise RuntimeError("이름 변경 계획이 오래됐거나 충돌했습니다.")
        if storage.identity(op["source"]) != op["identity"]:
            raise RuntimeError("스캔 이후 원본이 교체됐습니다.")
    previous = root / storage.LOG_NAME
    if previous.exists():
        previous.rename(root / f"rename-log.undone-{uuid.uuid4().hex}.json")
    run_id = uuid.uuid4().hex
    staging = root / (".kotonoha-output-" + run_id)
    staging.mkdir()
    data = {
        "version": 4,
        "mode": MODE,
        "run_id": run_id,
        "status": "in_progress",
        "root": str(root),
        "planned_root": str(output),
        "root_identity": storage.identity(root),
        "output_identity": storage.identity(staging),
        "revision": 0,
        "planned_operations": [
            {**op, "source": str(op["source"]), "target": str(op["target"])} for op in inner_plan
        ]
        + [
            {
                "type": "output",
                "source": str(staging),
                "target": str(output),
                "identity": storage.identity(staging),
            }
        ]
        + moves,
        "completed_operations": [],
        "undo_operations": [],
        "translation": metadata or {},
    }
    persist(data)
    return _continue(data, "apply", progress, cancel)


def _continue(data, direction, progress=None, cancel=None):
    reconcile(data)
    if direction == "apply" and (data["status"].startswith("undo") or data["undo_operations"]):
        raise RuntimeError("복구가 시작됐습니다. 원래 위치 복구를 계속하세요.")
    field = "completed_operations" if direction == "apply" else "undo_operations"
    planned = (
        data["planned_operations"]
        if direction == "apply"
        else list(reversed(data["completed_operations"]))
    )
    pending = list(enumerate(planned, 1))[len(data[field]) :]
    try:
        data["status"] = "in_progress" if direction == "apply" else "undo_in_progress"
        data.pop("error", None)
        persist(data)
        for index, op in pending:
            if cancel and cancel():
                raise Cancelled("작업을 중단했습니다. 기록에서 계속하거나 복구할 수 있습니다.")
            execute(data, op, direction, index)
            if progress:
                progress(
                    len(data[field]),
                    len(planned),
                    "내용 이동" if direction == "apply" else "원래 위치 복구",
                )
        data["status"] = "completed" if direction == "apply" else "undone"
        log = persist(data)
    except BaseException as exc:
        data["status"] = "partial_failure" if direction == "apply" else "undo_partial_failure"
        data["error"] = str(exc)
        persist(data)
        raise
    if direction == "undo":
        staging = Path(data["root"]) / (".kotonoha-output-" + data["run_id"])
        if staging.exists() and storage.identity(staging) == data["output_identity"]:
            try:
                staging.rmdir()  # Only our empty directory; never remove user contents.
            except OSError:
                pass
    final = Path(data["planned_root"] if direction == "apply" else data["root"])
    return final, log, data


def resume(log, confirm, progress=None, cancel=None):
    log, data = recover(log)
    if data["status"] == "completed":
        return Path(data["planned_root"]), log, data
    if not confirm("남은 내용을 이동하시겠습니까? [y/N]: "):
        return Path(data["root"]), log, data
    return _continue(data, "apply", progress, cancel)


def undo(log, confirm, progress=None, cancel=None):
    log, data = recover(log)
    if data["status"] == "undone":
        return Path(data["root"]), log
    if not confirm("원래 위치와 이름으로 복구하시겠습니까? [y/N]: "):
        return Path(data["root"]), log
    root, log, _ = _continue(data, "undo", progress, cancel)
    return root, log


def preview_update(log, inner_plan, output_name):
    _, data = recover(log)
    if data["status"] != "completed":
        raise RuntimeError("중단된 작업을 먼저 계속하거나 복구하세요.")
    root, current = Path(data["root"]), Path(data["planned_root"])
    target = root / output_name
    if (
        not output_name
        or output_name in {".", ".."}
        or Path(output_name).name != output_name
        or target.parent != root
        or output_name.startswith((".kotonoha-", "rename-log."))
    ):
        raise ValueError("결과 폴더 이름을 확인하세요.")
    if target != current and target.exists():
        raise RuntimeError("다른 항목과 결과 폴더 이름이 겹칩니다.")
    for op in inner_plan:
        source, destination = Path(op["source"]), Path(op["target"])
        if (
            current not in source.parents
            or source.parent != destination.parent
            or not source.exists()
            or destination.exists()
            or storage.identity(source) != op["identity"]
        ):
            raise RuntimeError("이름 변경 계획이 오래됐거나 충돌했습니다. 폴더를 다시 확인하세요.")
    operations = [
        {**op, "source": str(op["source"]), "target": str(op["target"])} for op in inner_plan
    ]
    if target != current:
        operations.append(
            {
                "type": "result",
                "source": str(current),
                "target": str(target),
                "identity": data["output_identity"],
            }
        )
    return target, operations, data


def update(log, inner_plan, output_name, revision, progress=None, cancel=None):
    target, operations, data = preview_update(log, inner_plan, output_name)
    if data["revision"] != revision:
        raise RuntimeError("복구 기록이 바뀌었습니다. 적용 내용을 다시 확인하세요.")
    data.setdefault("initial_output", data["planned_root"])
    data["planned_root"] = str(target)
    data["planned_operations"].extend(operations)
    data["status"] = "in_progress"
    persist(data)
    return _continue(data, "apply", progress, cancel)
