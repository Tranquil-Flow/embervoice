# Embervoice

Turn an EPUB into a chaptered audiobook on your own Mac. Choose a narrator style, optionally add a voice clip you have permission to use, try a preview, and create an M4B plus individual chapter M4As. Your source book is not modified.

**Apple Silicon Mac · macOS 26 or later · at least 16 GB RAM · non-commercial narration.** Intel Macs, 8 GB Macs, Windows and Linux are unsupported. The local voice model supports English and Chinese; DRM, image-only books and other languages are unsupported.

## Release status

The self-contained Mac app is an **initial preview release**, not a notarized production release. Download it from [GitHub Releases](https://github.com/Tranquil-Flow/embervoice/releases). Unsigned distribution is intentional and owner-approved. Do not mistake automated tests for a completed fresh-machine or listening-quality qualification. See [RELEASE_STATUS.md](RELEASE_STATUS.md) for the exact tested scope and limitations.

## Mac app installation (preview)

These instructions apply to **embervoice-0.1.0-macos-arm64.zip** on the release page—not GitHub's automatic “Source code” download.

1. Download the Mac app ZIP and unzip it. The package contains **Embervoice.app** and a short opening guide.
2. Move **Embervoice.app** to Applications, or another folder you want to keep.
3. Double-click the app. A small Embervoice window starts its private local server; your browser opens only when it is ready. Python and the audio tools are included; **no Homebrew, terminal commands or separate Python installation is needed**.
4. Complete the voice-model setup below, then add your EPUB.
5. Use **Quit** in the Embervoice window, close that window, or press Command-Q to stop the server and its owned workers. Closing just the browser does not stop generation. **Open Embervoice** reopens the browser interface.

The public preview is **ad-hoc signed only**, not Developer ID signed or Apple notarized. Ad-hoc signing provides internal bundle consistency, not a verified publisher identity. Download only from this repository's release page. If macOS blocks it, use **System Settings → Privacy & Security → Open Anyway** only after you have decided to trust this download. Do not disable Gatekeeper globally or remove quarantine with a terminal command. A fresh quarantined-download flow still needs human verification.

## First launch: the voice model

The setup panel displays the **full BreezeBlue Research and Non-Commercial License Agreement**. Read it and independently accept it before choosing **Download voice model**. No installer, app launcher or automated release test accepts it for you. Model weights are not included in any release ZIP.

- Pinned model: `mlx-community/Breeze-TTS-2-mlx-8bit`, revision `c6e4a2ff6ab9afba68b7853de802273ffe23fb49`.
- The pinned runtime files occupy approximately **4.60 GB**. Allow additional download headroom and room for outputs; passage WAVs can be substantially larger than AAC files.
- No Hugging Face login or token is needed.
- Setup shows **bytes stored locally**, not a guessed completion time, and verifies files against the pinned manifest.
- **Cancel download** keeps partial data. Resume from the setup panel; quitting Embervoice stops its owned download.
- Already-cached weights still require your own acceptance and verification.

After setup, narration stays offline. Missing or damaged model files produce a visible setup error instead of silently downloading during a book run.

## Make an audiobook

1. Drop a readable EPUB (up to 50 MiB).
2. Pick one of eight narrator styles, or reveal the four playful styles. Samples play only when pressed. These direct one model; they are not separate fixed voice models.
3. Optionally add a clean **2–30 second** clip (up to 10 MiB) and its **exact spoken transcript**, or explicitly press **Record your voice**. Use the speaker's consent and audio you have rights to. Recording never starts automatically.
4. Choose **Generate preview** for a passage from the first substantive section, or **Generate full book**.
5. Finished books appear on **Your shelf**. Playback never autostarts. Download the chapter-marked M4B or individual M4As.

Every readable section is preserved, including front matter. Chapter markers let you skip opening pages without deleting them. Synthetic narration is identified in M4B metadata. Listen across chapters for pronunciation, omissions and voice consistency; technical completion is not a listening-quality guarantee.

## Stop and resume

**Stop this run** preserves completed passages. Submit the same book with the **identical voice direction, clip and transcript** to resume. Changed settings create a separate voice version rather than overwrite the old one. Job IDs do not survive restart; saved books and passages do.

The previously measured workload peaked around 8.5 GiB of MLX memory. The worker recommends 12.5 GiB reclaimable RAM. Below that, each run asks whether to use **low-memory mode**, which may be slower. Hard limits refuse under 12 GiB total RAM, under 4 GiB reclaimable RAM or over 90% swap usage; critical pressure between passages stops safely. A 16 GB Mac may need low-memory mode. Do not disable safety checks.

## Privacy and storage

The browser talks only to a server bound to **127.0.0.1**, not your LAN. No cloud narration, analytics, remote fonts or CDNs are used.

The prebuilt Mac app needs network access only for user-approved model setup, which contacts Hugging Face and its file-serving infrastructure. Book text, voice prompts and reference clips are not sent with those requests. Generation forces Hugging Face and Transformers offline mode. Source installation additionally downloads Python/dependencies after consent.

Books, clips and outputs remain in `~/Audiobooks/Studio/books/`; setup state in `~/Audiobooks/Studio/setup/`; the model normally uses `~/.cache/huggingface/hub/`. These legacy storage names are retained for compatibility with Audiobook Studio. Browser local storage remembers your book, voice direction and clip transcript. Files are **not encrypted at rest**; someone with access to your user account may read them. Removing a selected clip does not delete clips referenced by older resumable versions.

Advanced configuration: `HF_HOME` or `HF_HUB_CACHE` moves the cache. `AUDIOBOOK_MODEL_DIR` may select the exact pinned snapshot layout. Use the same environment for setup and generation; mismatched checkpoints are refused.

## Troubleshooting

| Problem | Action |
|---|---|
| macOS blocks the app | Review the unsigned-app warning above. Never disable system protection globally. |
| Browser closed | Click **Open Embervoice** in the native launcher. |
| Audio tools missing | In the prebuilt app, download a fresh complete app ZIP. Source installations need `brew install ffmpeg`. |
| Port occupied | Embervoice starts on its own free loopback port; it never attaches to another service. |
| Download interrupted | Keep partial files, reopen setup and resume. |
| Disk or RAM refusal | Free storage or close unneeded demanding work; preserve saved passages to resume. |
| Microphone denied | Allow it in browser settings only if desired, or choose a clip file and supply its transcript. |
| EPUB rejected | Use a readable non-DRM English or Chinese EPUB you have the right to convert. No DRM bypass is provided. |

## Licensing

**Project code: AGPL-3.0-only**, without warranty; see [LICENSE](LICENSE).

The voice model and Breeze-generated audio—including shipped preset samples and built-in clips—carry separate **BreezeBlue Research and Non-Commercial License Agreement** terms. See [the full agreement](legal/BREEZE_LICENSE.txt) and [NOTICE](NOTICE). The code licence does not remove model restrictions or grant rights to books/voices. Commercial narration requires separate permissions.

The Mac app bundles Python and third-party runtime components with their notices. Its minimal FFmpeg audio tools use LGPL-2.1-or-later code. The exact source URL, checksum and rebuild configuration are in `scripts/build_ffmpeg.py`; the complete corresponding FFmpeg source accompanies a binary release. See `legal/FFMPEG_LICENSE.txt` and `legal/THIRD_PARTY_NOTICES.txt`.

## Source installation and development

The **source ZIP is for developers**, not the easiest sharing route. It requires ffmpeg/ffprobe (`brew install ffmpeg`) and includes the optional double-click `Install.command` / `Start Embervoice.command` route. That installer asks before downloading checksum-pinned uv and Python/dependencies. It does not install Homebrew or accept/download the model. Keep the source folder in place, or recreate its environment after moving it.

Python **3.13** and the lockfile are required. The voice runtime is pinned to an exact `mlx-audio` source revision.

```sh
uv sync --locked --extra voice --extra test --extra build
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -m pytest -q
node --check web/app.js
node --check web/setup.js
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for isolated browser tests, macOS packaging and consent-qualified real-model checks. Ordinary tests use synthetic inputs; they never download weights, record a physical microphone or use the personal library.
