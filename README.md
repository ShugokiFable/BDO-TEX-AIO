# BDO Texture AIO

AI upscaling for Black Desert Online **world** textures — landscape, buildings,
props, NPCs, monsters, mounts (optional). Stages results for Meta Injector.
An opt-in mode (`[G]`) adds **playable-class (player)** textures for a GPT /
external upscaler pass. Double-click **`START.bat`**.

## Relationship to BDO-AIO (choices always win)

| App | Owns |
|-----|------|
| **BDO-AIO** | Body / pube / genital / outfit **choices** |
| **BDO-AIO-BodyMats** | Higher-res of those **same** chosen bodies |
| **BDO-TEX-AIO** (this) | World / environment; optional NPC skins |

This tool:

- **Never** includes playable-class `character/texture/p*` assets unless you
  opt in (`[G]` / `includePlayerTextures` — for a GPT/texconv pass)  
- **By default** skips LODs / SpeedTree **billboards** / impostors  
  (optional high toggle `[L]` / `includeLodBillboards`)  

- **Never** overwrites a path already present in `files_to_patch` (AIO, BodyMats, …)  
- **Never** writes into the BDO-AIO install folder  
- Writes only `files_to_patch\_bdo_tex_upscale\`

So after you pick bodies in BDO-AIO, TEX will not stomp those choices. Paths AIO
(or BodyMats) already staged are **skipped** at stage time — even in player mode.

## GPT / player-texture mode (opt-in)

World textures are the default. Playable-class `character/texture/p*` is
BDO-AIO territory, so this tool stays out unless you say otherwise. Press
`[G]` (or set `includePlayerTextures: true` in config) to opt them back in
for a GPT / external upscaler pass:

```text
[G] player mode on  ->  [1] scan  ->  [2] extract  ->  [A] export to work\04_swarm_in
     -> GPT upscales the PNGs (keep filenames) -> results into work\05_swarm_out
     -> [B] pack --source gpt  ->  [6] stage  ->  Meta Injector
```

- Stage still skips any path BDO-AIO / BodyMats already claimed — AIO wins.
- Every GPT result is blank-checked before packing; all-black outputs are
  skipped, never shipped (same guard that caught Upscayl's silent blanks).
- Output format follows each texture's own header (DXT1/DXT5, full mips) —
  no manual format matching; the GPT PNG just needs to be ≥ the source size.
- Same folder flow works with SwarmUI / ComfyUI (`swarm-export`, `--source swarm`).

## Recommended run order

```text
1. BDO-AIO          pick + DEPLOY bodies/pubes/outfits
2. BodyMats         optional - enhance those bodies only
3. BDO-TEX-AIO      this app - world textures
4. PartCutGen       if you use it
5. Meta Injector    once on the game Paz folder
```

TEX can be processed anytime (scan/upscale are self-contained under `work\`),
but **stage after AIO deploy** so collision checks see AIO paths and skip them.

## Pipeline

```text
pad00000.meta -> scan -> extract -> upscale -> companions(match) -> pack -> stage
```

| Step | What |
|------|------|
| Scan | Eligible colour textures under configured roots |
| Extract | PAZ → PNG (mip 0) |
| Upscale | Upscayl on **RGB only**, alpha re-attached after; every output blank-checked. Or **GPT mode**: export → your own GPT batch → `pack --source gpt` |
| Companions | Resize **existing** `_n`/`_sp`/… that already exist in the archive |
| Pack | DDS + full mips |
| Stage | Only unclaimed world paths → `_bdo_tex_upscale` |

**Companions:** only maps already in `pad00000.meta` for that albedo. Inventing
new `_n` fails Meta Injector (not in meta). No ParallaxGen equivalent for BDO.

## Upscayl speed and the blank-output trap

Measured on this machine (RTX 4080 SUPER, 9800X3D), 150 real BDO textures per
config, **every output checked for blankness**:

| Config | 5,187 textures | Output |
|--------|---------------:|--------|
| `tile=400` (old default) | ~81 min | valid |
| `tile=auto` | **~54 min** | valid |
| `tile=1024` / `tile=2048` | ~10 min | **blank — all of it** |
| `upscayl-lite`, RGBA input | ~7 min | **blank (146/150)** |
| `upscayl-lite`, RGB input | **~21 min** | valid |

> **upscayl-bin can write a completely empty image, print
> `🙌 Upscayled Successfully!`, and exit 0.** Nothing in the exit code or the
> log tells you. Two triggers were reproduced: a large `-t`, and an RGBA input
> under load. Configs that looked 7× faster were fast because they were
> producing nothing.

So the pipeline now:

1. **Feeds RGB only.** Alpha is split off before the upscaler and re-attached
   with Lanczos afterwards. This removes the RGBA trigger *and* is the correct
   treatment anyway — alpha in these textures is a cutout mask, and an AI model
   invents soft edges on it.
2. **Blank-checks every output** during the resample pass (free — it is already
   decoding), retries the failures one at a time at `1:1:1`, and **refuses to
   write** anything still blank so `pack`/`stage` cannot ship invisible textures.

| Key | Default | Meaning |
|-----|--------:|---------|
| `upscaleBatch` | 256 | PNGs per Upscayl process. **Resume granularity, not a VRAM knob** — VRAM is set by tile and threads, so small batches only pay ~3s of process start-up each |
| `upscaylGpu` | `"0"` | GPU id (`-g`); empty = auto |
| `upscaylTile` | `0` | Tile size (`-t`); `0` = auto. **Do not raise this** — 1024/2048 produce blank images |
| `upscaylThreads` | `"2:4:2"` | load:proc:save (`-j`). Drop to `1:1:1` if blanks appear |

Upscayl uses **Vulkan/ncnn**, not CUDA. If a batch crashes outright
(ACCESS_VIOLATION) it retries one file at a time; resume keeps finished raws.

**If it is still too slow:** preset `[P]` → *Fast (lite model)* uses
`upscayl-lite-4x` (~21 min instead of ~54) with visibly softer detail. Model
choice barely matters otherwise — when output is actually valid, every model
lands in the 1.4–2.2 img/s band on this GPU.

## Defaults

- **Roots:** world only (`object/texture`, trees, terrain detail)  
- **Character textures:** OFF (menu `[C]` to enable NPC/monster; player `p*` opt-in via `[G]`)  
- **Target:** use menu `[P]` presets (playtest 1024, quality 2048, …)

## Menu

| Key | Action |
|-----|--------|
| `1`–`6` | Scan → extract → upscale → companions → pack → stage |
| `7` | Full pipeline |
| `C` | Toggle character/texture (NPC/monster; not player classes) |
| `G` | Toggle player/GPT textures (playable `p*`; opt-in) |
| `L` | Toggle LOD/billboards (**OFF** default; high option, low ROI) |
| `N` | Toggle companion-map matching |
| `P` / `T` / `M` | Preset / target / model |
| `R` | Remove `_bdo_tex_upscale` only |

## Requirements

- Python 3 + Pillow + numpy  
- Upscayl (CLI inside the install)  
- Meta Injector to apply `files_to_patch`  
- Optional: GPT / SwarmUI / ComfyUI for the external-upscaler path  

```bat
pip install pillow numpy
python tools\bdo_tex.py status
python tools\bdo_tex.py scan
```

## Config

First run copies `config.example.json` → `config.json`. Set `gameDir` to your
Black Desert install. Keep `workDir` as `"work"` (stays next to the app).

## Safety summary

| Rule | Enforced how |
|------|----------------|
| No player-class textures unless `[G]` opt-in | `texfilter` playable-class block + `includePlayerTextures` |
| AIO paths not overwritten | Stage skips any path owned by another layer |
| No invent materials | Companions = archive siblings only |
| Isolated work | All caches under app `work\` |
