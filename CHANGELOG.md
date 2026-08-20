# Changelog

All notable changes to BDO-TEX-AIO. Format: Keep a Changelog, versions
semantic. Release zips on GitHub always match the tagged commit.

## [1.5.1] - 2026-08-19

### Added
- **GPT / Codex zip round-trip**: `[A]` export also writes `work\gpt-export.zip`;
  `[B]` accepts the returned zip (`pack --source gpt --zip <file>`) with a
  zip-slip guard, or Enter to keep the folder flow. `swarm-export --zip` /
  `gpt --zip` flags added.

### Changed
- `tools\texconv.exe` now tracked in the repo (was untracked) — ships in the
  release zip; pack still uses bundled `dds.py` by default.

## [1.5.0] - 2026-08-19

### Added
- **Bodies mode (opt-in)** — merges the BDO-AIO-BodyMats addon into this
  tool; the separate app is retired:
  - `[X]` menu toggle (persisted as `bodiesEnabled`, default **false**)
  - `[Y]` runs the bodies pipeline: scan AIO winners in `files_to_patch`
    (layer priority `_pubic_hair_` > `_genital_` > `_midnight_`) →
    process (skin albedo → 4096, others → 2048, resize existing
    `_n`/`_sp`/`_m`/… companions only) → stage to `_bdo_aio_bodymats`
  - Same safety rails as the world path: RGB-only upscale with alpha
    re-attach, blank-output check + LANCZOS fallback, no invented maps,
    never writes into the BDO-AIO install folder
  - `tools\body_mats.py` (scan/process/stage/status) now reads the unified
    `config.json` — falls back to TEX keys (`gameDir`, `bdoAioFilesToPatch`,
    `model`) when its own keys are absent
  - New config keys: `bodiesEnabled`, `bdoAioRoot`, `gamePaz`, `bodyWorkDir`
    (default `work_body`), `exportDir`, `skinAlbedoTarget`, `otherAlbedoTarget`,
    `materialMaxEdge`, `minAlbedoEdge`, `allowPackFallback`, `sourceGlobs`,
    `texconv`, `stageLayerName`

### Changed
- README / WORKFLOW updated: one app, run order now
  BDO-AIO → TEX ([Y] bodies, world) → PartCutGen → Meta Injector.
- `dds.py` confirmed byte-identical between the two tools before merging.

## [1.4.0] - 2026-08-19

### Added
- **GPT / player-texture mode (opt-in).** Playable-class `character/texture/p*`
  textures can now enter the pipeline for a GPT / external upscaler pass:
  - `[G]` menu toggle (persisted as `includePlayerTextures` in config)
  - `bdo_tex.py gpt` alias for `swarm-export`; `pack --source gpt` reads GPT
    output PNGs (same folders as the existing SwarmUI/ComfyUI path)
  - scan, extract and stage honour the flag; BDO-AIO / BodyMats paths are
    still never overwritten (stage collision guard wins as before)
- **Blank-output guard extended to pack.** Any all-black image in a GPT/Swarm
  pack source is skipped and counted — the same guard that caught Upscayl's
  silent blank outputs now protects imported results too.

### Changed
- Menu labels updated (`[A]`/`[B]` mention GPT; `[C]` help text notes the
  new opt-in).
- Stage-layer README wording: paths skipped are the claimed ones, not "all
  playable-class".

### Notes
- Player textures are BDO-AIO territory; the opt-in exists for vanilla
  player textures only, and any path AIO/BodyMats already staged still wins.

## [1.3.0] - 2026-08-05

### Added
- Initial release. World-texture pipeline for BDO: scan `pad00000.meta` →
  extract PAZ → Upscayl upscale → companion-map match → pack DDS (full mip
  chains, DXT1/DXT5) → stage to `files_to_patch\_bdo_tex_upscale` for Meta
  Injector.
- Safe Upscayl defaults after measurement: `tile=0` (auto), RGB-only input
  with alpha split/re-attach, blank-output check + one-at-a-time retry.
- Quality presets, resume batches, LOD/billboard and companion toggles,
  optional NPC/monster character textures, SwarmUI/ComfyUI advanced path.
