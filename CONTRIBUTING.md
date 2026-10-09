# Contributing

## CPU-only checks

```sh
uv sync --locked --extra test
.venv/bin/python scripts/build_ffmpeg.py
PATH="$PWD/.local/tools:$PATH" .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
node --check web/app.js && node --check web/setup.js
bash -n install.sh Install.command 'Start Embervoice.command'
```

Ordinary tests never download weights or use a real narrator. Audio fixtures are synthetic tones, not claimed narration. First-run tests use isolated tiny file manifests. Native launcher tests compile with system Swift on macOS 26 Apple Silicon; behavioral AppKit tests are deliberately skipped in CI, which is not a physical Finder/Gatekeeper session.

## Isolated browser checks

Install the test Chromium with `.venv/bin/playwright install chromium`. Use a throwaway loopback server and set `STUDIO_URL` and `STUDIO_TEST_STORE` explicitly. `tests/e2e_support.py` rejects ports 8765/8766 and the personal storage directory. Test routes may simulate readiness for non-model UI checks; they do not establish a real licence receipt or a real narration result. Microphone automation uses a fake browser device; do not substitute a real microphone without fresh consent.

Real-model scripts require `STUDIO_REAL_MODEL=1`, the exact licence independently accepted by the operator in the isolated store, a verified local snapshot and live resource admission. Never auto-accept the model licence for a recipient. Do not run concurrent MLX jobs, stop someone else's work, relax a refusal or target a personal EPUB as a convenience.

## Mac app build

```sh
uv sync --locked --extra voice --extra test --extra build
.venv/bin/python scripts/build_ffmpeg.py
.venv/bin/python scripts/build_macos.py
```

The native shell uses only installed macOS system frameworks. The onedir helper contains Python, its needed packages and the LGPL-only audio tools, with no weights. Bundled third-party notices include the Python runtime and native library licences. The exact FFmpeg source archive accompanies the binary, together with its checksum and configuration. `desktop/Embervoice.spec` is a maintained input, not an automatically generated spec.

Build outputs and runtime scratch directories remain ignored. No Developer ID certificate or notarization is implied by local ad-hoc signing. The build script does not remove quarantine, alter Gatekeeper, open permission dialogs, install global tools or use signing credentials.

## Public source artifacts

```sh
uv build
.venv/bin/python scripts/build_release.py
```

The source ZIP uses an explicit public allowlist; do not archive the entire working directory. Do not include personal books, model weights, environments, stores, credentials, scratch artifacts or internal handover documents. Preset narration samples carry the separate non-commercial terms in NOTICE.

## Acceptance and publication

Follow `RELEASE_STATUS.md` and record observed results. A source unit suite is not a fresh-user install, a successful import is not a synthesis, and non-silent audio is not subjective listening approval. Test the extracted ZIP, ready loopback launch, first-run refusal before consent, exact pinned-model verification, a real synthetic-book narration, chaptered M4B downloads, browser audio without autoplay and owned-process shutdown. Verify public uploaded assets and hashes after upload. Describe reused-cache checks separately from a genuinely empty-cache download and physical Gatekeeper first launch.

The owner has approved unsigned preview distribution; retain its first-launch warning and do not imply notarization. Do not accept the model agreement or bypass OS trust on their behalf. If a gate is unqualified, publish an honestly labelled source candidate or keep the binary in a draft rather than claiming a stable consumer release.
