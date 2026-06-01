# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ["ticket_web.py"],
    pathex=[],
    binaries=[],
    datas=[("templates", "templates")],
    hiddenimports=[
        "korail2",
        "SRT",
        "SRT.errors",
        "waitress",
        "Crypto",
        "Crypto.Cipher",
        "Crypto.Cipher.AES",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "jedi",
        "matplotlib",
        "numpy",
        "pandas",
        "pytest",
        "scipy",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ticket-sniper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
