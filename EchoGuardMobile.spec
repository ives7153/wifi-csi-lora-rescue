# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the EchoGuard Mobile release."""

from pathlib import Path
import runpy

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH)
version_info = runpy.run_path(str(project_root / "upper_computer" / "version.py"))
dist_name = version_info["MOBILE_DIST_NAME"]
assets_dir = project_root / "upper_computer" / "assets"
icon_path = assets_dir / "app_icon.ico"

datas = [
    (str(assets_dir / "app_icon.ico"), "upper_computer/assets"),
    (str(assets_dir / "app_icon.png"), "upper_computer/assets"),
]

excluded_modules = [
    "dearpygui",
    "OpenGL",
    "pyqtgraph.opengl",
    "psutil",
    "scipy",
    "upper_computer.utils.export",
    "upper_computer.viz.dashboard",
]

hiddenimports = [
    "PyQt6.QtSvg",
    *collect_submodules("pyqtgraph.multiprocess"),
]

a = Analysis(
    ["scripts/echoguard_mobile_pyinstaller_entry.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EchoGuard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=dist_name,
)
