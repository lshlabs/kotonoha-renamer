"""Package the browser GUI as a portable Windows ZIP, without an installer."""

import ast
import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

from _bootstrap import ROOT


def release_version():
    source = ast.parse((ROOT / "kotonoha_version.py").read_text(encoding="utf-8"))
    return next(
        node.value.value
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
        and isinstance(node.value, ast.Constant)
    )


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    version = release_version()
    dist = ROOT / "dist"
    bundle = dist / "kotonoha-renamer"
    if not (bundle / "Kotonoha.exe").is_file():
        raise FileNotFoundError(bundle / "Kotonoha.exe")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-Item -LiteralPath $env:KOTONOHA_VERIFY_EXE).VersionInfo.ProductVersion",
        ],
        env={**os.environ, "KOTONOHA_VERIFY_EXE": str(bundle / "Kotonoha.exe")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    if result.stdout.strip() != version:
        raise ValueError("Rebuild the executable before packaging")
    if {p.name for p in bundle.glob("*.exe")} != {"Kotonoha.exe"}:
        raise ValueError("The release must contain exactly one GUI launcher")
    for asset in (ROOT / "gui").iterdir():
        if (
            asset.is_file()
            and (bundle / "_internal/gui" / asset.name).read_bytes() != asset.read_bytes()
        ):
            raise ValueError(f"Bundled GUI asset is stale: {asset.name}")
    archive = dist / f"kotonoha-renamer-v{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(bundle.rglob("*")):
            relative = path.relative_to(bundle)
            if path.is_file() and relative.parts[0] != "data":
                output.write(path, Path("kotonoha-renamer") / relative)
        output.writestr("kotonoha-renamer/data/", "")
        for name in (
            "README.md",
            "docs/usage.md",
            "docs/validation.md",
            "docs/release.md",
            "docs/images/gui.png",
        ):
            output.write(ROOT / name, Path("kotonoha-renamer") / name)
    with zipfile.ZipFile(archive) as output:
        if output.testzip() is not None:
            raise ValueError("Portable archive integrity check failed")
    checksum = dist / f"SHA256SUMS-v{version}.txt"
    checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    print(f"Portable package: {archive}")


if __name__ == "__main__":
    main()
