# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

directory = Path(SPECPATH)
sys.path.insert(0, str(directory))
from build_support import without_system_icu
from PySide6.QtCore import QLibraryInfo

translations=Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
translation_datas=[(str(translations/('qtbase_'+language+'.qm')), 'translations')
                   for language in ('zh_CN','zh_TW','ja')]

a = Analysis(
    [str(directory / 'download_pawchive_images.py')],
    pathex=[str(directory)],
    binaries=[],
    datas=[(str(directory / '1.ico'), '.'),(str(directory/'activation_public.json'),'.')]+translation_datas,
    # These modules are imported from menu/dialog callbacks, so keeping them
    # explicit makes the frozen build independent of PyInstaller's optional
    # function-scope import analysis.
    hiddenimports=['artist_cards', 'popular_features', 'subscriptions', 'subscription_ui',
                   'queue_operations', 'queue_failure_ui','download_plan',
                   'update_checker','artist_migration','download_settings'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(directory / 'runtime_windows_icu.py')],
    excludes=['PyQt5', 'PyQt6', 'PySide2'],
    noarchive=False,
)
a.binaries = without_system_icu(a.binaries)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='PawchiveDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(directory / '1.ico'),
    version=str(directory / 'version_info.txt'),
)
