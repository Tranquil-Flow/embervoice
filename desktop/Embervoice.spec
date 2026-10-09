# PyInstaller 6.22.3; helper is bundled beneath the system-framework native shell.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
DATA = [(str(ROOT / 'model-files.json'), '.'), (str(ROOT / 'LICENSE'), '.'), (str(ROOT / 'NOTICE'), '.')]
for folder in ('web', 'anchors', 'legal'):
    DATA.append((str(ROOT / folder), folder))
DATA.append((str(ROOT / '.local/ffmpeg-build/ffmpeg-9.0.2.tar.xz'), 'legal'))
for package in ('mlx', 'mlx_audio', 'tokenizers', 'certifi'):
    DATA += collect_data_files(package)
for package in ('mlx', 'mlx-metal', 'mlx-audio', 'huggingface-hub', 'numpy', 'tokenizers', 'transformers', 'miniaudio', 'sounddevice'):
    DATA += copy_metadata(package)
HIDDEN = collect_submodules('mlx') + collect_submodules('mlx_audio.tts.models.breeze_tts')
HIDDEN += ['_cffi_backend', '_miniaudio', 'transformers.tokenization_utils_tokenizers', 'mlx._reprlib_fix', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.h11_impl',
           'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on', 'audiobook', 'model_setup']
BINARIES = [(str(ROOT / '.local/tools' / name), 'tools') for name in ('ffmpeg', 'ffprobe')]
a = Analysis([str(ROOT / 'embervoice_entry.py')], pathex=[str(ROOT)], binaries=BINARIES,
             datas=DATA, hiddenimports=HIDDEN, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['pytest', 'playwright', 'ruff', 'matplotlib', 'torch', 'tensorflow', 'IPython',
                       'jupyter', 'pandas', 'mlx_audio.server', 'mlx_audio.stt', 'mlx_audio.sts'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='embervoice-server', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True, target_arch='arm64',
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='embervoice-server')
