"""Validated work paths shared by GUI and CLI."""

import os
import re
import sys
from pathlib import Path

import kotonoha_engine as engine
import kotonoha_storage as storage


def split_title(name):
    # Product IDs are protected just like track prefixes; Python does not infer readings.
    match = re.match(r"^(\s*[\[【(（]?RJ\d+[\]】)）]?[-_ 　]*)(.*)$", name, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    return engine.split_leading_prefix(name)


def is_link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def validate_root(root, check_pending=True):
    install = (
        Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    )
    if not root.is_dir() or is_link(root):
        raise ValueError(f"대상은 접근 가능한 실제 작품 폴더여야 합니다: {root}")
    protected = [install.resolve(), storage.DATA_DIR.resolve()]
    for variable in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(variable):
            protected.append(Path(os.environ[variable]).resolve())
    if (
        root == Path(root.anchor)
        or root == Path.home().resolve()
        or any(root == p or root in p.parents or p in root.parents for p in protected)
    ):
        raise ValueError(
            "드라이브 루트·설치/시스템/사용자 데이터 폴더를 작업 대상으로 사용할 수 없습니다. 대상 폴더를 다시 선택하세요."
        )
    if not os.access(root, os.R_OK | os.W_OK) or not os.access(root.parent, os.W_OK):
        raise PermissionError("작품 폴더와 그 부모 폴더의 읽기/쓰기 권한을 확인하세요.")
    if check_pending:
        storage.check_existing(root)


def iter_work_paths(root, rename_root=True):
    """Yield eligible paths in stable order without following Windows links."""

    def scan_error(exc):
        raise exc

    if rename_root:
        yield root, "root"
    for directory, folders, files in os.walk(root, followlinks=False, onerror=scan_error):
        parent = Path(directory)
        folders[:] = sorted(name for name in folders if not is_link(parent / name))
        for name in folders:
            yield parent / name, "folder"
        for name in sorted(files):
            path = parent / name
            if (
                not is_link(path)
                and not engine.should_skip_file(path)
                and not name.startswith("rename-log.")
            ):
                yield path, "file"


def translation_title(path, kind):
    if kind != "file" and not engine.should_translate_folder(path):
        return None
    prefix, title = split_title(path.stem if kind == "file" else path.name)
    return (prefix, title) if title and engine.JAPANESE_RE.search(title) else None


def has_translation_targets(root, rename_root):
    return any(translation_title(path, kind) for path, kind in iter_work_paths(root, rename_root))
