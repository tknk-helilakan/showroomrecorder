# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


def try_collect(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except Exception:
        return []


hiddenimports = []
hiddenimports += collect_submodules("showroomrecorder")
hiddenimports += try_collect(collect_submodules, "faster_whisper")
hiddenimports += try_collect(collect_submodules, "ctranslate2")
hiddenimports += try_collect(collect_submodules, "streamlink")
hiddenimports += try_collect(collect_submodules, "streamlink_cli")
hiddenimports += try_collect(collect_submodules, "transformers.models.nllb")
hiddenimports += try_collect(collect_submodules, "transformers.models.m2m_100")
hiddenimports += try_collect(collect_submodules, "transformers.generation")
hiddenimports += [
    "av",
    "sentencepiece",
    "tokenizers",
    "torch",
    "transformers",
    "transformers.models.auto",
    "transformers.models.auto.modeling_auto",
    "transformers.models.auto.tokenization_auto",
]

datas = []
binaries = []
for package in (
    "faster_whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "sentencepiece",
    "transformers",
    "huggingface_hub",
    "torch",
    "streamlink",
    "streamlink_cli",
):
    datas += try_collect(collect_data_files, package, include_py_files=False)
    binaries += try_collect(collect_dynamic_libs, package)

for distribution in (
    "PyYAML",
    "requests",
    "yt-dlp",
    "streamlink",
    "faster-whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "sentencepiece",
    "transformers",
    "huggingface-hub",
    "torch",
    "protobuf",
    "filelock",
    "numpy",
    "tqdm",
    "regex",
    "safetensors",
):
    datas += try_collect(copy_metadata, distribution)

a = Analysis(
    ["showroomrecorder_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tensorflow",
        "keras",
        "jax",
        "flax",
        "matplotlib",
        "IPython",
        "pytest",
        "scipy",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="showroomrecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="showroomrecorder",
)
