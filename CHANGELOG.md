# Changelog

All notable changes to BDO-TEX-AIO. Format: Keep a Changelog, versions
semantic. Release zips on GitHub always match the tagged commit.

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
