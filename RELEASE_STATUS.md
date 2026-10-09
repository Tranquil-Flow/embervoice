# Release status — 0.1.0 preview

This initial preview is intentionally ad-hoc signed, not Developer ID signed or Apple notarized. The owner explicitly approved public unsigned Mac distribution and independently accepted the current BreezeBlue licence for isolated non-commercial release testing. Recipients must independently accept the model agreement themselves.

## Observed qualification

| Boundary | Observed result |
|---|---|
| Full source suite | 161 tests passed on macOS 26 Apple Silicon, using the bundled audio tools; one upstream Starlette/httpx deprecation warning. |
| Static checks | Ruff lint and formatting, JavaScript syntax and shell syntax passed. |
| Native shell | Compiler and isolated lifecycle tests passed, including fragmented stdout, early helper failure, SIGTERM cleanup and ephemeral loopback binding. |
| Final Mac ZIP | Extracted into a separate directory. Bundle signature consistency verified. Corrected FFmpeg configuration includes `pcm_u8` and `alac`; complete checksum-pinned corresponding FFmpeg source is present. |
| Absent recipient tools | An empty temporary HOME and PATH restricted to `/usr/bin:/bin` successfully ran the packaged helper diagnostics: MLX, Breeze and real tokenizer fixture imported; bundled ffmpeg/ffprobe ran; no platform/tool blockers. |
| Real native launch | The extracted native app launched the real frozen server with `--port 0`, reached its live `/api/setup`, and served the actual browser UI. No mocked server was substituted. |
| Consent boundary | With fresh setup state, model setup without acceptance was refused, and narration before acceptance was refused. The browser displayed the full agreement and required an explicit acceptance/start action. |
| Model integrity | The actual packaged setup worker deeply verified the exact pinned, already-cached model snapshot. This was cache reuse, not an empty-cache network download. |
| Real narration | The packaged worker generated the synthetic EPUB “A Free World”: 2 sections, 2 passages and 2 chapters. Its M4B duration was 10.76 seconds; complete decoding yielded 10.83 seconds. Chapter titles and source checksum matched, every passage was non-silent, and all three download links returned audio. |
| Resume | An identical full-book rerun completed by reusing both saved passages with no synthesis events. |
| Browser | Headless Chromium saw no JavaScript errors or external page requests, no audio autoplay, and no horizontal overflow at 390 px. Desktop and mobile screenshots were inspected. Physical microphone capture was not used. |
| Shutdown | Native shell exited normally; no owned helper remained and the qualification watchdog did not fire. Separate shutdown regression covers TERM-resistant narration children. |
| Source privacy | Explicit public allowlist excludes private books, model weights, environments, stores, credentials and internal handovers. Secret scanning found no leaks; a seeded negative control was detected. |

The exact downloadable artifacts and their SHA-256 hashes are listed in the release's `SHA256SUMS` and `release-evidence.json`. Download the Mac app ZIP, not GitHub's automatic source archive. The ZIP includes `OPEN_FIRST.txt` with safe first-launch instructions.

## Not qualified; explicit preview limitations

- Fresh-account Finder/Gatekeeper launch of a quarantined internet download. An isolated home on the development Mac is not a fresh machine. No quarantine was removed and no OS trust protection was bypassed during testing.
- A genuinely empty model-cache network download. Controlled synthetic cancel/resume/repair tests are separate evidence and do not establish this physical download path.
- Subjective listening acceptance, pronunciation/omission guarantees, long-book stability, or narrator consistency across a long book. Non-silent complete audio is not proof of those qualities.
- Intel Macs, 8 GB Macs, other operating systems, DRM/image-only EPUBs and languages beyond the model's English/Chinese support.
- Independent security audit or legal/trademark clearance of the display name. No project domain was acquired.

## Distribution boundaries and history

Code: AGPL-3.0-only. Breeze weights and generated samples: separate non-commercial model terms. The prebuilt app contains no weights. FFmpeg is built from the checksum-pinned LGPL-only upstream source; the complete corresponding source archive, configuration and notices accompany it inside the app. Ad-hoc signing establishes bundle consistency, not a verified publisher identity.

The initial commits are a concern-grouped reconstruction of previously unpublished work, not the original development chronology. Only the assembled final tip is the qualified release candidate; the intermediate snapshots were not independent releases.
