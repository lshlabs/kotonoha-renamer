# GUI-only portable build. Models and personal data are excluded.
from pathlib import Path

root = Path(SPECPATH)
a = Analysis([str(root / 'kotonoha_gui.py')], pathex=[str(root)],
    binaries=[], datas=[(str(root / 'glossary-runtime.json'), '.'),
        (str(root / 'model-catalog.json'), '.'),
        (str(root / 'resources/verify-ollama-signature.ps1'), '.'),
        (str(root / 'gui'), 'gui')],
    hiddenimports=['tkinter'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['webview', 'clr', 'pythonnet'], noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Kotonoha',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False, version=str(root / "resources/version-info.txt"))
coll = COLLECT(gui, a.binaries, a.datas, strip=False, upx=False, name='kotonoha-renamer')
