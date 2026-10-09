# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs

datas = [('clip-vit-base-patch32', 'clip-vit-base-patch32'),
         ('images/icon.ico', 'images')]
hiddenimports = [
    'timm.models.layers',
]
# The compiled libraries that register the torchvision custom operators
# (e.g. torchvision::nms) are loaded dynamically, so PyInstaller's binary
# dependency analysis can miss them.
binaries = (collect_dynamic_libs('torch')
            + collect_dynamic_libs('torchvision'))

block_cipher = None


a = Analysis(
    ['taggui/run_gui.py'],
    pathex=['taggui'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='taggui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX compression corrupts the torch/torchvision extension libraries
    # (their operator registration then fails at runtime with e.g.
    # "operator torchvision::nms does not exist").
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['images/icon.ico'],
    contents_directory='_taggui',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='taggui',
)
