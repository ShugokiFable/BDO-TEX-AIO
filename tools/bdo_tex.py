#!/usr/bin/env python3
"""BDO Texture AIO - extract, AI-upscale and re-inject Black Desert Online world textures.

Pipeline (each stage is resumable and writes its own manifest):

  scan       pad00000.meta -> eligible textures + real pixel dimensions
  extract    PAZ -> 02_filtered_png/            (mip 0, lossless PNG)
  upscale    02 -> 03_upscaled_png/             (Upscayl, then exact-size resample)
  materials  03 -> 03b_materials_png/           (resize EXISTING _n/_sp/_m/... only)
  pack       03 (+ 03b) -> 06_packed_dds/       (DXT + full mip chain)
  stage      06 -> <PAZ>/files_to_patch/_bdo_tex_upscale/   then Meta Injector

The game directory is only ever opened for reading; the single write target is
files_to_patch, which is Meta Injector's own staging folder.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dds  # noqa: E402
import materials  # noqa: E402
import paz  # noqa: E402
import texfilter  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent
STAGE_LAYER = "_bdo_tex_upscale"

# Never stage over paths claimed by these (BDO-AIO choices + other inject layers).
# existing_layers() already skips our own STAGE_LAYER; this is for messaging.
AIO_LAYER_HINTS = (
    "_midnight",
    "_pubic_hair",
    "_genital",
    "_censorship",
    "_slot_hide",
    "_body_size",
    "_partcutgen",
    "_player",
    "_bdo_aio_bodymats",  # body enhance is also out of scope for world TEX
)

# All extract / upscale / pack artifacts live under the app tree so the folder
# is self-contained (shareable, stays on the install SSD).
DEFAULT_WORK_DIRNAME = "work"

DEFAULTS = {
    "gameDir": paz.DEFAULT_GAME_DIR,
    # Relative paths resolve against APP_DIR. Prefer "work" for isolation.
    "workDir": DEFAULT_WORK_DIRNAME,
    "upscaylBin": r"C:\Program Files\Upscayl\resources\bin\upscayl-bin.exe",
    "upscaylModels": r"C:\Program Files\Upscayl\resources\models",
    "model": "high-fidelity-4x",
    "target": 1024,
    "minSize": 128,
    "maxOutput": 2048,
    # World only by default. character/texture is opt-in (usually already sharp enough).
    "roots": list(texfilter.DEFAULT_ROOTS),
    "workers": 0,          # 0 = os.cpu_count()
    # Batch size is a RESUME granularity, not a VRAM knob — VRAM is set by tile
    # and proc threads, so tiny batches only pay ~3s of process start-up each
    # (measured) for nothing. 256 keeps restarts cheap without the overhead.
    "upscaleBatch": 256,
    # Upscayl CLI performance (ncnn/Vulkan, not CUDA). Empty gpu = auto.
    # tile 0 = auto: measured 1.5x FASTER than tile=400 on a 4080 SUPER and
    # still correct. Do NOT raise this to 1024/2048 — measured, those make
    # upscayl-bin write fully blank images while still reporting success.
    # threads = load:proc:save (Upscayl -j). Drop to 1:1:1 if blanks appear.
    "upscaylGpu": "0",
    "upscaylTile": 0,
    "upscaylThreads": "2:4:2",
    # LOD / SpeedTree billboards: OFF by default (distance art, poor ROI).
    # High option: includeLodBillboards true re-scan required.
    "includeLodBillboards": False,
    # Player/GPT mode (opt-in): allow playable-class p* textures into the
    # pipeline for a GPT / external upscaler pass. OFF by default — those
    # paths are BDO-AIO territory; stage still lets BDO-AIO layers win.
    "includePlayerTextures": False,
    # Companion maps: ONLY resize maps that already exist in the archive for
    # that albedo. Inventing new _n/_sp/_p does nothing in BDO (shader slots
    # are fixed; no ParallaxGen equivalent). See materials.py docstring.
    "materialsEnabled": True,
    "materialsCompanionKinds": ["n", "sp", "m", "p", "w", "ao"],
    "bdoAioFilesToPatch": "",   # auto-detected from gameDir when empty
    "activePreset": "playtest",
}


def roots_normalized(cfg: dict) -> tuple[str, ...]:
    roots = cfg.get("roots") or texfilter.DEFAULT_ROOTS
    return tuple(str(r).lower().replace("\\", "/").rstrip("/") + "/" for r in roots)


def path_in_roots(path: str, roots: tuple[str, ...]) -> bool:
    p = path.lower().replace("\\", "/")
    return any(p.startswith(r) for r in roots)


def filter_by_roots(entries: list[dict], cfg: dict) -> list[dict]:
    """Drop paths outside current roots / LOD policy (leftover extracts, old manifests)."""
    roots = roots_normalized(cfg)
    lod_on = bool(cfg.get("includeLodBillboards", False))
    out = []
    for e in entries:
        p = e.get("path") or ""
        if not path_in_roots(p, roots):
            continue
        if not lod_on:
            is_dist, _ = texfilter.is_lod_or_billboard(p)
            if is_dist:
                continue
        out.append(e)
    return out

PRESET_KEYS = ("target", "minSize", "maxOutput", "model")


def presets_path() -> Path:
    return APP_DIR / "presets.json"


def load_presets() -> dict:
    path = presets_path()
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))

STAGES = {
    "extracted": "01_extracted_dds",
    "filtered": "02_filtered_png",
    # Alpha is split off before the upscaler and lives in its own directory, so
    # `-i 02_filtered_png` never sees it as an image to upscale.
    "alpha": "02b_alpha_png",
    "upscaled": "03_upscaled_png",
    # Materials sit beside the upscale outputs; do not renumber legacy swarm/pack dirs.
    "materials": "03b_materials_png",
    "swarm_in": "04_swarm_in",
    "swarm_out": "05_swarm_out",
    "packed": "06_packed_dds",
    "stage": "07_inject_stage",
    "logs": "logs",
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(2)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def resolve_work_dir(value: str | Path | None) -> Path:
    """Resolve workDir to an absolute path under this app when relative.

    Isolation rule: pipeline outputs always default to APP_DIR/work.
    Relative values (e.g. "work") join APP_DIR. Absolute values are kept
    as-is only if set intentionally, but the stock config uses "work".
    """
    if value is None or str(value).strip() == "":
        return (APP_DIR / DEFAULT_WORK_DIRNAME).resolve()
    p = Path(str(value).strip())
    if not p.is_absolute():
        p = APP_DIR / p
    return p.resolve()


def load_config(path: Path | None = None) -> dict:
    cfg = dict(DEFAULTS)
    cfg_path = path or (APP_DIR / "config.json")
    if cfg_path.is_file():
        # utf-8-sig strips a Windows PowerShell/Notepad BOM if present
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        cfg.update(raw)
    # Always normalize workDir to absolute, app-local when relative
    cfg["workDir"] = str(resolve_work_dir(cfg.get("workDir")))
    return cfg


def save_config(cfg: dict, path: Path | None = None) -> Path:
    dest = path or (APP_DIR / "config.json")
    out = dict(cfg)
    # Store portable relative workDir when it lives inside the app
    try:
        rel = Path(out["workDir"]).resolve().relative_to(APP_DIR.resolve())
        out["workDir"] = str(rel).replace("\\", "/") or DEFAULT_WORK_DIRNAME
    except (ValueError, KeyError):
        pass
    text = json.dumps(out, indent=2) + "\n"
    dest.write_text(text, encoding="utf-8")  # no BOM
    return dest


def apply_preset(cfg: dict, name: str) -> dict:
    presets = load_presets()
    if name not in presets:
        known = ", ".join(sorted(presets)) or "(none)"
        die(f"unknown preset '{name}'. Known: {known}")
    pr = presets[name]
    for key in PRESET_KEYS:
        if key in pr:
            cfg[key] = pr[key]
    cfg["activePreset"] = name
    return cfg


def work(cfg: dict, key: str) -> Path:
    root = resolve_work_dir(cfg.get("workDir"))
    p = root / STAGES[key]
    p.mkdir(parents=True, exist_ok=True)
    return p


def paz_dir(cfg: dict) -> Path:
    return Path(cfg["gameDir"]) / "Paz"


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    path: str
    paz_number: int
    offset: int
    comp_size: int
    orig_size: int
    width: int = 0
    height: int = 0
    fourcc: str = ""
    mips: int = 0
    out_width: int = 0
    out_height: int = 0
    verdict: str = ""


_WORKER_ARCHIVE: paz.Archive | None = None
_WORKER_CFG: dict = {}


def _worker_init(cfg: dict) -> None:
    global _WORKER_ARCHIVE, _WORKER_CFG
    _WORKER_CFG = cfg
    _WORKER_ARCHIVE = paz.Archive(cfg["gameDir"])


def _probe(rec: dict) -> dict:
    """Decode one file far enough to read its DDS header, then size-gate it."""
    f = paz.PazFile(0, 0, 0, rec["paz_number"], rec["offset"], rec["comp_size"], rec["orig_size"])
    try:
        blob = _WORKER_ARCHIVE.content(f)
        head = dds.read_header(blob)
    except Exception as exc:
        rec["verdict"] = f"unreadable:{type(exc).__name__}"
        return rec
    rec["width"] = head["width"]
    rec["height"] = head["height"]
    rec["fourcc"] = head["fourcc"]
    rec["mips"] = head["mip_count"]
    if not head["compressed"]:
        rec["verdict"] = f"uncompressed-format({head['fourcc']!r})"
        return rec
    accept, _scale, (ow, oh), reason = texfilter.size_verdict(
        head["width"], head["height"],
        _WORKER_CFG["minSize"], _WORKER_CFG["target"], _WORKER_CFG["maxOutput"],
    )
    rec["out_width"], rec["out_height"] = (ow, oh) if accept else (0, 0)
    rec["verdict"] = "accept" if accept else reason
    return rec


def cmd_scan(cfg: dict, args) -> int:
    log("Loading pad00000.meta ...")
    cache = Path(cfg["workDir"]) / "logs" / "index-cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    ix = paz.load_cached_index(cache, cfg["gameDir"])
    log(f"  {len(ix.files):,} files indexed, meta version {ix.version}")

    roots = roots_normalized(cfg)
    char_on = any(r.startswith(texfilter.CHARACTER_ROOT) for r in roots)
    lod_on = bool(cfg.get("includeLodBillboards", False))
    player_on = bool(cfg.get("includePlayerTextures", False))
    log(f"  roots ({len(roots)}): {', '.join(r.rstrip('/') for r in roots)}")
    if player_on:
        log("  player/GPT textures: ON (opt-in - BDO-AIO territory, vanilla p* only)")
    if not char_on:
        log("  character/texture: OFF (default - enable in config or menu [C])")
    log(f"  LOD/billboards: {'ON (high option)' if lod_on else 'OFF (default)'}")
    pre: list[dict] = []
    rejected: dict[str, int] = {}
    for f in ix.files:
        name = ix.file_names[f.file_id] if f.file_id < len(ix.file_names) else ""
        if not name.lower().endswith(".dds"):
            continue
        path = ix.path_of(f)
        eligible, reason = texfilter.classify(
            path, include_lod_billboards=lod_on,
            include_player_textures=player_on,
        )
        if eligible and not path_in_roots(path, roots):
            # Distinguish optional character from other disabled roots.
            pl = path.lower().replace("\\", "/")
            if pl.startswith(texfilter.CHARACTER_ROOT):
                eligible, reason = False, "character-opt-in-off"
            else:
                eligible, reason = False, "root-disabled-in-config"
        if not eligible:
            key = reason.split("(")[0]
            rejected[key] = rejected.get(key, 0) + 1
            continue
        pre.append({
            "path": path, "paz_number": f.paz_number, "offset": f.offset,
            "comp_size": f.comp_size, "orig_size": f.orig_size,
            "width": 0, "height": 0, "fourcc": "", "mips": 0,
            "out_width": 0, "out_height": 0, "verdict": "",
        })

    log(f"  path/name filter: {len(pre):,} candidates, {sum(rejected.values()):,} excluded")
    for k, v in sorted(rejected.items(), key=lambda kv: -kv[1]):
        log(f"    {v:>8,}  {k}")

    if args.limit:
        pre = pre[: args.limit]
        log(f"  --limit {args.limit}: probing a subset")

    workers = cfg["workers"] or os.cpu_count() or 4
    log(f"\nReading DDS headers with {workers} workers "
        f"(decompression is the slow part; ~{len(pre)//max(1, workers*13)+1}s expected) ...")
    t0 = time.time()
    out: list[dict] = []
    with ProcessPoolExecutor(workers, initializer=_worker_init, initargs=(cfg,)) as pool:
        for i, rec in enumerate(pool.map(_probe, pre, chunksize=16), 1):
            out.append(rec)
            if i % 500 == 0:
                log(f"    {i:,}/{len(pre):,}  ({time.time()-t0:.0f}s)")
    log(f"  done in {time.time()-t0:.0f}s")

    verdicts: dict[str, int] = {}
    for rec in out:
        key = rec["verdict"].split("(")[0]
        verdicts[key] = verdicts.get(key, 0) + 1
    accepted = [r for r in out if r["verdict"] == "accept"]

    log("\nSize gate (min > %d, target %d):" % (cfg["minSize"], cfg["target"]))
    for k, v in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        log(f"    {v:>8,}  {k}")

    src_bytes = sum(r["orig_size"] for r in accepted)
    out_bytes = sum(
        (r["out_width"] * r["out_height"] // (2 if r["fourcc"] == "DXT1" else 1)) * 4 // 3
        for r in accepted
    )
    log(f"\nACCEPTED: {len(accepted):,} textures")
    log(f"  source  ~{src_bytes/2**30:.2f} GB")
    log(f"  output  ~{out_bytes/2**30:.2f} GB  (DXT + mips, added VRAM/disk cost)")

    report = {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "game_dir": cfg["gameDir"], "target": cfg["target"], "min_size": cfg["minSize"],
        "meta_version": ix.version,
        "path_rejected": rejected, "size_verdicts": verdicts,
        "accepted": len(accepted), "candidates": out,
    }
    dest = Path(cfg["workDir"]) / "logs" / "scan.json"
    dest.write_text(json.dumps(report), encoding="utf-8")
    log(f"\nWrote {dest}")
    return 0


def load_scan(cfg: dict) -> dict:
    p = Path(cfg["workDir"]) / "logs" / "scan.json"
    if not p.is_file():
        die("no scan.json — run `scan` first")
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------

def _split_rgb_alpha(im: Image.Image, dest: Path, alpha_dest: Path) -> bool:
    """Write RGB to dest; write alpha sidecar only when mask is non-opaque.

    Returns True when an alpha channel was saved. RGB-only extract is required:
    feeding RGBA to upscayl-bin can produce blank success-exit images.
    """
    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A")
    has_alpha = alpha.getextrema()[0] < 255
    dest.parent.mkdir(parents=True, exist_ok=True)
    rgba.convert("RGB").save(dest, "PNG", optimize=False, compress_level=1)
    if has_alpha:
        alpha_dest.parent.mkdir(parents=True, exist_ok=True)
        alpha.save(alpha_dest, "PNG", optimize=False, compress_level=1)
    elif alpha_dest.is_file():
        alpha_dest.unlink(missing_ok=True)
    return has_alpha


def _extract_one(rec: dict) -> dict:
    f = paz.PazFile(0, 0, 0, rec["paz_number"], rec["offset"], rec["comp_size"], rec["orig_size"])
    dest = Path(rec["_dest"])
    alpha_dest = Path(rec["_alpha_dest"])
    if dest.is_file():
        # Resume / upgrade path: older extracts saved full RGBA. Re-split those
        # so Upscayl never sees an alpha channel (blank-output trap).
        try:
            with Image.open(dest) as existing:
                existing.load()
                if existing.mode in ("RGBA", "LA", "PA") or "A" in existing.getbands():
                    has_alpha = _split_rgb_alpha(existing, dest, alpha_dest)
                    return {"path": rec["path"], "status": "re-split", "alpha": has_alpha}
            return {"path": rec["path"], "status": "exists", "alpha": alpha_dest.is_file()}
        except Exception:
            pass  # fall through and re-read from PAZ
    try:
        blob = _WORKER_ARCHIVE.content(f)
        im = dds.to_image(blob)
        if rec.get("_keep_dds"):
            Path(rec["_keep_dds"]).write_bytes(blob)
        # RGB only for the upscaler. Two measured reasons, not style:
        #   1. An AI model run on an alpha channel invents soft gradients on
        #      what are cutout masks (foliage, decals) — wrong by construction.
        #   2. With RGBA input, upscayl-bin silently emits fully blank images
        #      under load (146/150 with upscayl-lite) while still exiting 0.
        #      The same run with RGB input is clean. Alpha is carried around
        #      the upscaler and re-attached with Lanczos in _resize_one.
        has_alpha = _split_rgb_alpha(im, dest, alpha_dest)
        return {"path": rec["path"], "status": "ok", "alpha": has_alpha}
    except Exception as exc:
        return {"path": rec["path"], "status": f"error:{type(exc).__name__}: {exc}"}


def cmd_extract(cfg: dict, args) -> int:
    scan = load_scan(cfg)
    accepted = [r for r in scan["candidates"] if r["verdict"] == "accept"]
    # Honour current roots even if scan.json was made with character on.
    before = len(accepted)
    accepted = filter_by_roots(accepted, cfg)
    skipped = before - len(accepted)
    if skipped:
        log(f"  skipped {skipped:,} outside current roots (e.g. character with opt-in off)")
    if args.limit:
        accepted = accepted[: args.limit]
    if not accepted:
        die("scan accepted nothing - loosen --target or check the filters / roots "
            "(re-run [1] Scan after changing character toggle or preset)")

    out_preview = work(cfg, "filtered")
    existing = sum(1 for _ in out_preview.glob("*.png")) if out_preview.is_dir() else 0
    log(f"  extract targets: {len(accepted):,}  already in 02_filtered_png: {existing:,}")

    out_dir = work(cfg, "filtered")
    dds_dir = work(cfg, "extracted") if args.keep_dds else None
    paz.assert_safe_out(out_dir, cfg["gameDir"])

    alpha_dir = alpha_sidecar_dir(cfg)
    jobs = []
    for r in accepted:
        flat = texfilter.flat_name(r["path"])
        j = dict(r)
        j["_dest"] = str(out_dir / (flat[:-4] + ".png"))
        j["_alpha_dest"] = str(alpha_dir / (flat[:-4] + ".png"))
        j["_keep_dds"] = str(dds_dir / flat) if dds_dir else ""
        jobs.append(j)

    workers = cfg["workers"] or os.cpu_count() or 4
    log(f"Extracting {len(jobs):,} textures -> {out_dir} ({workers} workers)")
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(workers, initializer=_worker_init, initargs=(cfg,)) as pool:
        for i, res in enumerate(pool.map(_extract_one, jobs, chunksize=8), 1):
            results.append(res)
            if i % 500 == 0:
                log(f"    {i:,}/{len(jobs):,}  ({time.time()-t0:.0f}s)")

    errors = [r for r in results if r["status"].startswith("error")]
    log(f"  ok={sum(1 for r in results if r['status']=='ok'):,} "
        f"already-present={sum(1 for r in results if r['status']=='exists'):,} "
        f"errors={len(errors):,}  in {time.time()-t0:.0f}s")
    for e in errors[:10]:
        log(f"    ! {e['path']}: {e['status']}")

    alpha_by_path = {r["path"]: bool(r.get("alpha")) for r in results if "alpha" in r}
    n_alpha = sum(1 for v in alpha_by_path.values() if v)
    log(f"  alpha channel: {n_alpha:,} of {len(alpha_by_path):,} textures "
        f"(carried around the upscaler, re-attached with Lanczos)")

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target": cfg["target"],
        "roots": list(roots_normalized(cfg)),
        "entries": [
            {"path": r["path"], "flat": texfilter.flat_name(r["path"])[:-4] + ".png",
             "fourcc": r["fourcc"], "src": [r["width"], r["height"]],
             "out": [r["out_width"], r["out_height"]],
             "alpha": alpha_by_path.get(r["path"], False)}
            for r in accepted
        ],
    }
    mpath = Path(cfg["workDir"]) / "logs" / "manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    log(f"Wrote {mpath} ({len(manifest['entries']):,} entries)")
    return 0


def load_manifest(cfg: dict) -> dict:
    p = Path(cfg["workDir"]) / "logs" / "manifest.json"
    if not p.is_file():
        die("no manifest.json — run `extract` first")
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# upscale
# --------------------------------------------------------------------------

def is_blank_rgb(im: Image.Image) -> bool:
    """True when every RGB channel is flat zero — upscayl's silent failure mode.

    upscayl-bin can exit 0, print success, and still write a fully empty image
    (measured: RGBA input, or a large -t). A genuinely all-black world albedo
    would also match, but that only costs one retry and a warning, whereas
    missing this ships invisible textures into the game.
    """
    ex = im.convert("RGB").getextrema()
    return all(lo == 0 and hi == 0 for lo, hi in ex)


def _resize_one(job: dict) -> str:
    """Raw 4x -> exact target size, re-attaching alpha the upscaler never saw."""
    try:
        w, h = job["w"], job["h"]
        with Image.open(job["src"]) as raw:
            rgb = raw.convert("RGB")
            if is_blank_rgb(rgb):
                return f"blank:{job['flat']}"
            if rgb.size != (w, h):
                rgb = rgb.resize((w, h), Image.LANCZOS)

        out = rgb
        alpha_src = job.get("alpha_src")
        if alpha_src and Path(alpha_src).is_file():
            with Image.open(alpha_src) as a:
                alpha = a.convert("L")
            if alpha.size != (w, h):
                alpha = alpha.resize((w, h), Image.LANCZOS)
            out = rgb.convert("RGBA")
            out.putalpha(alpha)

        out.save(job["dest"], "PNG", optimize=False, compress_level=1)
        return "ok"
    except Exception as exc:
        return f"error:{job['src']}: {exc}"


def alpha_sidecar_dir(cfg: dict) -> Path:
    return work(cfg, "alpha")


def _retry_blanks(cfg, blanks, bin_path, models, src_dir, raw_dir, batch_dir,
                  out_dir, alpha_dir, entries, up_kw, workers) -> list[str]:
    """Re-run the blanked files one at a time with the most conservative
    settings, then re-resample. Returns the ones still blank.

    batch_dir may have been removed after the main upscayl loop — recreate it.
    """
    by_flat = {e["flat"]: e for e in entries}
    safe_kw = dict(up_kw, threads="1:1:1", tile=0)
    batch_dir.mkdir(parents=True, exist_ok=True)
    redo = []
    for flat in blanks:
        src = src_dir / flat
        if not src.is_file():
            continue
        (raw_dir / flat).unlink(missing_ok=True)
        for old in list(batch_dir.iterdir()):
            if old.is_file():
                old.unlink(missing_ok=True)
        _link_or_copy(src, batch_dir / flat)
        _run_upscayl(bin_path, models, cfg["model"], batch_dir, raw_dir, **safe_kw)
        if (raw_dir / flat).is_file():
            redo.append(flat)
    shutil.rmtree(batch_dir, ignore_errors=True)
    if not redo:
        return blanks

    jobs = []
    for flat in redo:
        e = by_flat.get(flat)
        if not e:
            continue
        jobs.append({
            "src": str(raw_dir / flat), "dest": str(out_dir / flat), "flat": flat,
            "w": e["out"][0], "h": e["out"][1],
            "alpha_src": str(alpha_dir / flat) if e.get("alpha") else "",
        })
    with ProcessPoolExecutor(workers) as pool:
        results = list(pool.map(_resize_one, jobs, chunksize=4))
    still = [r.split(":", 1)[1] for r in results if r.startswith("blank:")]
    log(f"     recovered {sum(1 for r in results if r == 'ok'):,}, "
        f"still blank {len(still):,}")
    return still


def _link_or_copy(src: Path, dest: Path) -> None:
    """Hardlink when possible (same volume, free); fall back to copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        return
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def _run_upscayl(
    bin_path: Path,
    models: Path,
    model: str,
    in_path: Path,
    out_dir: Path,
    *,
    gpu: str = "",
    tile: int = 0,
    threads: str = "",
) -> int:
    """Run upscayl-bin; return exit code. Suppresses broken emoji on Windows consoles.

    Upscayl uses ncnn + Vulkan (not CUDA). Safe speed knobs:
      gpu     -g   device id ("0" for first GPU; empty = auto)
      tile    -t   tile size (0 = auto; 300-400 ok on high VRAM)
      threads -j   load:proc:save (default in Upscayl is 1:2:2)
    """
    cmd = [str(bin_path), "-i", str(in_path), "-o", str(out_dir),
           "-n", model, "-m", str(models), "-s", "4", "-f", "png"]
    if gpu is not None and str(gpu).strip() != "":
        cmd += ["-g", str(gpu).strip()]
    if tile and int(tile) > 0:
        cmd += ["-t", str(int(tile))]
    if threads and str(threads).strip():
        cmd += ["-j", str(threads).strip()]
    # Upscayl prints emoji progress that mojibakes on CP1252; capture and re-print ASCII.
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        cmd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    text = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    for line in text.splitlines():
        # Drop non-ASCII glyphs (emoji) so the console stays readable.
        clean = "".join(ch if ord(ch) < 128 else "?" for ch in line).strip()
        if clean:
            log(f"  {clean}")
    return proc.returncode


def cmd_upscale(cfg: dict, args) -> int:
    man = load_manifest(cfg)
    src_dir = work(cfg, "filtered")
    out_dir = work(cfg, "upscaled")
    raw_dir = Path(cfg["workDir"]) / "03_upscaled_png" / "_raw4x"
    raw_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = Path(cfg["workDir"]) / "03_upscaled_png" / "_batch_in"
    batch_dir.mkdir(parents=True, exist_ok=True)

    bin_path = Path(cfg["upscaylBin"])
    models = Path(cfg["upscaylModels"])
    if not bin_path.is_file():
        die(f"Upscayl CLI not found: {bin_path}\n"
            f"       Install Upscayl, or set upscaylBin in config.json")
    if not (models / f"{cfg['model']}.param").is_file():
        available = sorted(p.stem for p in models.glob("*.param"))
        die(f"model {cfg['model']!r} not in {models}. Available: {', '.join(available)}")

    entries = filter_by_roots(man["entries"], cfg)
    if len(entries) < len(man["entries"]):
        log(f"  ignoring {len(man['entries']) - len(entries):,} manifest paths "
            f"outside current roots (e.g. character off)")

    # Resume: final out present OR raw 4x present (resample later).
    todo = []
    missing_src = 0
    for e in entries:
        if (out_dir / e["flat"]).is_file():
            continue
        if (raw_dir / e["flat"]).is_file():
            continue
        src = src_dir / e["flat"]
        if not src.is_file():
            missing_src += 1
            continue
        todo.append(e)

    already_raw = sum(1 for e in entries if (raw_dir / e["flat"]).is_file()
                      and not (out_dir / e["flat"]).is_file())
    already_out = sum(1 for e in entries if (out_dir / e["flat"]).is_file())
    src_count = sum(1 for _ in src_dir.glob("*.png")) if src_dir.is_dir() else 0

    if not entries:
        die("manifest has no entries under current roots - run [1] Scan then [2] Extract")
    if not todo and already_raw == 0 and already_out == 0:
        # Classic failure mode: work/ was cleaned but logs/manifest.json stayed.
        die(
            f"nothing to upscale - no PNGs ready.\n"
            f"  manifest wants {len(entries):,} world textures\n"
            f"  missing source PNG: {missing_src:,}  (02_filtered_png has {src_count:,} files)\n"
            f"  This usually means work/ was cleared or extract never ran for current roots.\n"
            f"  Fix: run [1] Scan  then  [2] Extract  then  [3] Upscale\n"
            f"  (character is OFF - scan will drop old character@ jobs)"
        )
    if not todo and already_raw == 0 and already_out > 0:
        if missing_src:
            log(f"  note: {missing_src:,} listed in manifest but no source PNG "
                f"(already have {already_out:,} final outs)")
        log(f"Nothing to upscale - {already_out:,} final outputs already present.")
        return 0
    if not todo and already_raw > 0:
        log(f"  Upscayl done earlier; resampling {already_raw:,} raw 4x -> final size ...")

    batch_size = int(cfg.get("upscaleBatch") or 32)
    if getattr(args, "batch", None):
        batch_size = max(1, int(args.batch))
    batch_size = max(1, batch_size)

    # CLI --gpu overrides config; empty string means auto.
    gpu = getattr(args, "gpu", None)
    if gpu is None or str(gpu).strip() == "":
        gpu = str(cfg.get("upscaylGpu", "0")).strip()
    else:
        gpu = str(gpu).strip()
    try:
        tile = int(cfg.get("upscaylTile", 400) or 0)
    except (TypeError, ValueError):
        tile = 0
    threads = str(cfg.get("upscaylThreads") or "2:4:2").strip()

    log(f"Upscaling with {cfg['model']} (4x Vulkan/ncnn, then exact resample)")
    log(f"  need Upscayl: {len(todo):,}   already 4x raw: {already_raw:,}   "
        f"already final: {already_out:,}")
    log(f"  batch={batch_size}  gpu={gpu or 'auto'}  tile={tile or 'auto'}  "
        f"threads={threads or 'default'}")
    log(f"  (safe 4080-class defaults; if crash: tile=0 or 200, batch=16, threads=1:2:2)")
    log(f"  {src_dir}  ->  {raw_dir}  ->  {out_dir}")

    up_kw = dict(gpu=gpu, tile=tile, threads=threads)

    t0 = time.time()
    failed_batches = 0
    done_up = 0
    for bi in range(0, len(todo), batch_size):
        chunk = todo[bi: bi + batch_size]
        # Fresh batch input — only the files Upscayl must touch this pass.
        if batch_dir.is_dir():
            for old in batch_dir.iterdir():
                if old.is_file():
                    old.unlink(missing_ok=True)
        linked = 0
        for e in chunk:
            src = src_dir / e["flat"]
            if not src.is_file():
                continue
            _link_or_copy(src, batch_dir / e["flat"])
            linked += 1
        if linked == 0:
            continue

        n_batch = bi // batch_size + 1
        n_total = (len(todo) + batch_size - 1) // batch_size
        log(f"  batch {n_batch}/{n_total}: {linked} images ...")
        rc = _run_upscayl(bin_path, models, cfg["model"], batch_dir, raw_dir, **up_kw)
        if rc != 0:
            failed_batches += 1
            # 3221225477 = 0xC0000005 ACCESS_VIOLATION (VRAM / bad batch).
            if rc in (3221225477, -1073741819) or (rc & 0xFFFFFFFF) == 0xC0000005:
                log(f"  ! upscayl-bin ACCESS_VIOLATION (exit {rc}) on batch {n_batch}")
                log("    Retrying this batch one file at a time "
                    "(also try lower upscaylTile / upscaleBatch in config) ...")
                # Snapshot names before clearing the batch folder.
                names = [e["flat"] for e in chunk]
                for flat in names:
                    if (raw_dir / flat).is_file():
                        done_up += 1
                        continue
                    src = src_dir / flat
                    if not src.is_file():
                        continue
                    # One-file folder -> raw_dir (same as batch mode, size 1).
                    for old in batch_dir.iterdir():
                        if old.is_file():
                            old.unlink(missing_ok=True)
                    _link_or_copy(src, batch_dir / flat)
                    # Safer single-file: smaller tile if configured high
                    single_kw = dict(up_kw)
                    if tile and tile > 200:
                        single_kw["tile"] = 200
                    rc1 = _run_upscayl(
                        bin_path, models, cfg["model"], batch_dir, raw_dir, **single_kw
                    )
                    if rc1 != 0 or not (raw_dir / flat).is_file():
                        log(f"    FAIL {flat} exit {rc1}")
                    else:
                        done_up += 1
                continue
            die(f"upscayl-bin exited {rc} on batch {n_batch}/{n_total}. "
                f"Lower upscaleBatch/upscaylTile in config.json "
                f"(now batch={batch_size} tile={tile}) and re-run; "
                f"already-finished raw files are kept for resume.")
        done_up += linked
        if n_batch % 5 == 0 or n_batch == n_total:
            log(f"    progress ~{min(bi + linked, len(todo)):,}/{len(todo):,} "
                f"({time.time() - t0:.0f}s)")

    # Clean empty batch folder noise
    shutil.rmtree(batch_dir, ignore_errors=True)
    log(f"  upscayl phase done in {time.time() - t0:.0f}s "
        f"(failed batches recovered: {failed_batches})")

    # Resample every entry that has raw but not final (includes resume + new).
    alpha_dir = alpha_sidecar_dir(cfg)
    jobs = []
    missing = []
    for e in entries:
        if (out_dir / e["flat"]).is_file():
            continue
        raw = raw_dir / e["flat"]
        if not raw.is_file():
            missing.append(e["flat"])
            continue
        jobs.append({
            "src": str(raw), "dest": str(out_dir / e["flat"]), "flat": e["flat"],
            "w": e["out"][0], "h": e["out"][1],
            "alpha_src": str(alpha_dir / e["flat"]) if e.get("alpha") else "",
        })
    if missing:
        log(f"  ! {len(missing)} upscaler outputs still missing (first: {missing[:3]})")

    workers = cfg["workers"] or os.cpu_count() or 4
    log(f"Resampling {len(jobs):,} images to exact target size "
        f"(+ alpha re-attach, + blank check) ...")
    with ProcessPoolExecutor(workers) as pool:
        results = list(pool.map(_resize_one, jobs, chunksize=8))
    blanks = [r.split(":", 1)[1] for r in results if r.startswith("blank:")]
    errs = [r for r in results if r != "ok" and not r.startswith("blank:")]
    for e in errs[:10]:
        log(f"    ! {e}")

    recovered = 0
    if blanks:
        n_blank = len(blanks)
        log(f"\n  !! {n_blank:,} upscaler outputs were BLANK "
            f"(upscayl exited 0 but wrote an empty image)")
        log("     Retrying those one at a time with conservative settings ...")
        blanks = _retry_blanks(cfg, blanks, bin_path, models, src_dir, raw_dir,
                               batch_dir, out_dir, alpha_dir, entries, up_kw, workers)
        recovered = n_blank - len(blanks)
        if blanks:
            log(f"\n  !! {len(blanks):,} textures STILL blank after retry — "
                f"they are NOT written, so pack/stage cannot ship them:")
            for f in blanks[:10]:
                log(f"       {f}")
            log("     Try a different model, or upscaylThreads=1:1:1 in config.json.")
    written = sum(1 for r in results if r == "ok") + recovered
    log(f"  done, {written:,} written to {out_dir}"
        + (f" (incl. {recovered:,} blank-retries)" if recovered else ""))
    if not args.keep_raw:
        # Keep raw only if something is still missing (so resume can finish).
        if missing or blanks:
            log("  keeping intermediate 4x images (some still missing/blank — re-run upscale)")
        else:
            shutil.rmtree(raw_dir, ignore_errors=True)
            log("  removed intermediate 4x images (--keep-raw to keep)")
    # Exit non-zero only when Upscayl never produced a raw we could use.
    # Remaining blanks after retry are already excluded from pack (not written).
    return 0 if not missing else 1


def cmd_swarm_export(cfg: dict, args) -> int:
    """Hand the filtered PNGs to SwarmUI/ComfyUI for the advanced pass."""
    man = load_manifest(cfg)
    entries = filter_by_roots(man["entries"], cfg)
    src_dir = work(cfg, "filtered")
    dest_dir = work(cfg, "swarm_in")
    n = 0
    for e in entries[: args.limit or None]:
        src = src_dir / e["flat"]
        if src.is_file() and not (dest_dir / e["flat"]).is_file():
            shutil.copy2(src, dest_dir / e["flat"])
            n += 1
    log(f"Copied {n:,} PNGs to {dest_dir}")
    log("Run your GPT / SwarmUI / ComfyUI batch with that folder as input and")
    log(f"write the results (same filenames) to {work(cfg, 'swarm_out')}")
    log("Then:  bdo_tex.py pack --source gpt   (or --source swarm)")
    return 0


# --------------------------------------------------------------------------
# materials — resize EXISTING companion maps only (no invent)
# --------------------------------------------------------------------------

def _path_index(ix: paz.Index) -> dict[str, paz.PazFile]:
    """Lowercase archive path -> PazFile for sibling material lookups."""
    out: dict[str, paz.PazFile] = {}
    for f in ix.files:
        out[ix.path_of(f).lower()] = f
    return out


def _find_sibling(path_ix: dict[str, paz.PazFile], albedo_path: str, kind: str
                  ) -> tuple[str, paz.PazFile] | None:
    for cand in materials.material_candidates(albedo_path, kind):
        hit = path_ix.get(cand.lower())
        if hit is not None:
            return cand, hit
    return None


def _material_flat(albedo_flat: str, kind: str) -> str:
    """object@texture@foo.png + n -> object@texture@foo_n.png"""
    if not albedo_flat.lower().endswith(".png"):
        raise ValueError(albedo_flat)
    stem = albedo_flat[:-4]
    return f"{stem}{materials.KIND_PRIMARY_SUFFIX[kind]}.png"


def _mat_resize_from_blob(blob: bytes, dest: Path, ow: int, oh: int) -> None:
    im = dds.to_image(blob).convert("RGBA")
    if im.size != (ow, oh):
        im = im.resize((ow, oh), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "PNG", optimize=False, compress_level=1)


def cmd_materials(cfg: dict, args) -> int:
    """Resize companion maps that ALREADY exist in the game archive.

    When you upscale foo.dds 512->1024, foo_n.dds at 512 would mismatch.
    This step extracts each existing sibling (_n/_sp/_m/_p/_w/_ao) and
    LANCZOS-resizes it to the new albedo size, then pack injects them.

    Does NOT invent maps the archive never had — BDO shaders do not
    auto-bind orphan textures (no ParallaxGen-style material rewrite).
    """
    if not cfg.get("materialsEnabled", True) and not getattr(args, "force", False):
        log("materialsEnabled=false - skip (set true in config, or pass --force)")
        return 0

    man = load_manifest(cfg)
    entries = filter_by_roots(man["entries"], cfg)
    if args.limit:
        entries = entries[: args.limit]
    kinds = materials.companion_kinds_enabled(cfg)
    if not kinds:
        die("materialsCompanionKinds is empty")

    out_dir = work(cfg, "materials")
    paz.assert_safe_out(out_dir, cfg["gameDir"])

    log("Materials: EXISTING companions only (no invent / no fake parallax)")
    log(f"  kinds checked: [{', '.join('_' + k for k in kinds)}]")
    log(f"  output: {out_dir}")
    log("Loading pad00000.meta for sibling lookup ...")
    cache = Path(cfg["workDir"]) / "logs" / "index-cache.json"
    ix = paz.load_cached_index(cache, cfg["gameDir"])
    path_ix = _path_index(ix)
    archive = paz.Archive(cfg["gameDir"])
    log(f"  {len(path_ix):,} paths indexed")

    stats = {"no_siblings": 0, "resized": 0, "already": 0, "errors": 0, "touched": 0}
    t0 = time.time()

    for ei, e in enumerate(entries, 1):
        ow, oh = int(e["out"][0]), int(e["out"][1])
        found_any = False
        for kind in kinds:
            hit = _find_sibling(path_ix, e["path"], kind)
            if hit is None:
                continue
            found_any = True
            sib_path, pf = hit
            dest = out_dir / _material_flat(e["flat"], kind)
            # Flat uses primary suffix (_n) even if archive used _normal etc.
            # Pack re-derives path from primary suffix — good for inject path.
            # If archive path used a non-primary spelling, map inject path to
            # the REAL archive path so Meta Injector overwrites the right file.
            real_flat = texfilter.flat_name(sib_path)
            if real_flat.lower().endswith(".dds"):
                dest = out_dir / (real_flat[:-4] + ".png")
            if dest.is_file():
                stats["already"] += 1
                continue
            try:
                blob = archive.content(pf)
                _mat_resize_from_blob(blob, dest, ow, oh)
                del blob
                stats["resized"] += 1
                stats["touched"] += 1
            except Exception as exc:
                stats["errors"] += 1
                if stats["errors"] <= 10:
                    log(f"  ! {sib_path}: {exc}")
        if not found_any:
            stats["no_siblings"] += 1
        if ei % 500 == 0:
            log(f"    {ei:,}/{len(entries):,}  resized={stats['resized']:,}  "
                f"({time.time()-t0:.0f}s)")

    archive.close()
    log(f"  done in {time.time()-t0:.0f}s")
    log(f"  resized={stats['resized']:,} already={stats['already']:,} "
        f"errors={stats['errors']:,}  albedos_with_no_companions={stats['no_siblings']:,}")
    log("  Invent path removed: only maps the game already ships are matched.")
    return 0 if stats["errors"] == 0 else 1


# --------------------------------------------------------------------------
# pack
# --------------------------------------------------------------------------

def _pack_one(job: dict) -> str:
    try:
        with Image.open(job["src"]) as im:
            im = im.convert("RGBA")
            # Blank-output guard: an upscaler (GPT included) can return a fully
            # black image while "succeeding" — never ship that to the game.
            if is_blank_rgb(im):
                return f"blank:{job['src']}"
            if im.size != (job["w"], job["h"]):
                im = im.resize((job["w"], job["h"]), Image.LANCZOS)
            blob = dds.write(im, job["fourcc"], mipmaps=True)
        Path(job["dest"]).write_bytes(blob)
        return "ok"
    except Exception as exc:
        return f"error:{job['src']}: {type(exc).__name__}: {exc}"


def cmd_pack(cfg: dict, args) -> int:
    man = load_manifest(cfg)
    entries = filter_by_roots(man["entries"], cfg)
    if len(entries) < len(man["entries"]):
        log(f"  ignoring {len(man['entries']) - len(entries):,} outside current roots")
    man = dict(man)
    man["entries"] = entries
    src_dir = work(cfg, "swarm_out" if args.source in ("swarm", "gpt") else "upscaled")
    mat_dir = work(cfg, "materials")
    out_dir = work(cfg, "packed")
    paz.assert_safe_out(out_dir, cfg["gameDir"])

    jobs, missing = [], 0
    for e in man["entries"]:
        src = src_dir / e["flat"]
        if not src.is_file():
            missing += 1
            continue
        # flat_name(path) ends with .dds; pack writes that name into packed/
        dest_name = texfilter.flat_name(e["path"])
        if not dest_name.endswith(".dds"):
            dest_name = dest_name.rsplit(".", 1)[0] + ".dds"
        jobs.append({
            "src": str(src), "dest": str(out_dir / dest_name),
            "fourcc": e["fourcc"] if e["fourcc"] in ("DXT1", "DXT5", "DXT3") else "DXT1",
            "w": e["out"][0], "h": e["out"][1],
        })

    # Pack companion maps from materials stage (DXT5). PNG flat name already
    # encodes the real archive path (including non-primary suffixes).
    mat_n = 0
    if mat_dir.is_dir() and cfg.get("materialsEnabled", True):
        for png in sorted(mat_dir.glob("*.png")):
            # Skip if this is somehow an albedo flat also listed above
            dest_name = png.stem + ".dds"
            # Prefer DDS fourcc: normals often DXT5; default DXT5 for companions
            dest = out_dir / dest_name
            # Infer size from PNG
            try:
                with Image.open(png) as im:
                    w, h = im.size
            except Exception:
                continue
            jobs.append({
                "src": str(png),
                "dest": str(dest),
                "fourcc": "DXT5",
                "w": w, "h": h,
            })
            mat_n += 1

    if not jobs:
        die(f"no upscaled images found in {src_dir}")
    log(f"Packing {len(jobs):,} DDS "
        f"({missing:,} albedo missing, {mat_n:,} companion maps) -> {out_dir}")

    workers = cfg["workers"] or os.cpu_count() or 4
    t0 = time.time()
    with ProcessPoolExecutor(workers) as pool:
        errs = [r for r in pool.map(_pack_one, jobs, chunksize=8) if r != "ok"]
    blanks = [e for e in errs if e.startswith("blank:")]
    errs = [e for e in errs if not e.startswith("blank:")]
    if blanks:
        log(f"    SKIPPED {len(blanks):,} blank outputs "
            f"(blank-output guard - never packed)")
    for e in errs[:10]:
        log(f"    ! {e}")
    total = sum(Path(j["dest"]).stat().st_size for j in jobs if Path(j["dest"]).is_file())
    log(f"  {len(jobs)-len(errs)-len(blanks):,} packed, {len(errs)} failed, "
        f"{len(blanks)} blank-skipped, {total/2**30:.2f} GB in {time.time()-t0:.0f}s")
    return 0


# --------------------------------------------------------------------------
# stage  (the only step that writes near the game, into Meta Injector's folder)
# --------------------------------------------------------------------------

def internal_path(rel: Path) -> str | None:
    """Meta Injector 1.4.1 organizer-marker rules: '_'/'.' dirs are stripped."""
    if rel.name.startswith((".", "_")):
        return None
    kept = [p for p in rel.parts[:-1] if not p.startswith((".", "_"))]
    return "/".join((*kept, rel.name)).lower()


def existing_layers(ftp: Path) -> dict[str, str]:
    """Map internal game path -> owning top-level folder, for every staged mod.

    Any other layer wins the path: BDO-AIO body/pube/outfit choices and
    BodyMats enhancements are never overwritten by this world-texture tool.
    """
    owned: dict[str, str] = {}
    if not ftp.is_dir():
        return owned
    for top in ftp.iterdir():
        if top.name == STAGE_LAYER or not top.is_dir():
            continue
        for f in top.rglob("*"):
            if f.is_file():
                ip = internal_path(f.relative_to(ftp))
                if ip:
                    owned.setdefault(ip, top.name)
    return owned


def _owner_is_aio_choice(layer_name: str) -> bool:
    low = layer_name.lower()
    return any(h in low for h in AIO_LAYER_HINTS)


def cmd_stage(cfg: dict, args) -> int:
    packed = work(cfg, "packed")
    files = sorted(packed.glob("*.dds"))
    if not files:
        die(f"nothing packed in {packed} - run `pack` first")

    ftp = Path(cfg["bdoAioFilesToPatch"]) if cfg["bdoAioFilesToPatch"] else paz_dir(cfg) / "files_to_patch"
    if not (paz_dir(cfg) / "pad00000.meta").is_file():
        die(f"pad00000.meta not found in {paz_dir(cfg)} - is gameDir correct?")

    # Never write into the BDO-AIO install tree
    log("Checking for collisions - BDO-AIO / other layers always keep their paths ...")
    owned = existing_layers(ftp)
    log(f"  {len(owned):,} game paths already claimed by other layers")

    dest_root = ftp / STAGE_LAYER
    collisions = []
    aio_collisions = []
    plan = []
    player_blocked = 0
    for f in files:
        game_path = texfilter.unflat_name(f.name)
        # Never stage playable-class paths (BDO-AIO body/outfit territory)
        _elig, why = texfilter.classify(
            game_path,
            include_lod_billboards=bool(cfg.get("includeLodBillboards", False)),
            include_player_textures=bool(cfg.get("includePlayerTextures", False)),
        )
        if "playable-class" in why:
            player_blocked += 1
            continue
        # If LOD/billboards are off, never stage leftovers from an older scan
        if not cfg.get("includeLodBillboards", False):
            is_dist, _ = texfilter.is_lod_or_billboard(game_path)
            if is_dist:
                continue
        if game_path in owned:
            who = owned[game_path]
            collisions.append((game_path, who))
            if _owner_is_aio_choice(who):
                aio_collisions.append((game_path, who))
            continue
        plan.append((f, dest_root / game_path))

    if player_blocked:
        log(f"  blocked {player_blocked:,} playable-class paths (BDO-AIO territory)")
    if collisions:
        log(f"\n  SKIPPED {len(collisions):,} path(s) already claimed "
            f"({len(aio_collisions):,} by BDO-AIO/body layers):")
        for gp, who in collisions[:15]:
            tag = " [AIO/body choice - kept]" if _owner_is_aio_choice(who) else ""
            log(f"    - {gp}  (owned by {who}){tag}")
        if len(collisions) > 15:
            log(f"    ... +{len(collisions)-15} more")
        log("  BDO-AIO body/pube/outfit choices are never overwritten.")

    log(f"\nStaging {len(plan):,} world textures -> {dest_root}")
    if args.dry_run:
        for src, dst in plan[:10]:
            log(f"    {src.name} -> {dst}")
        log("  (dry run - nothing written)")
        return 0

    if dest_root.exists():
        # Refresh our layer only; never touch sibling AIO folders
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    total = 0
    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        total += dst.stat().st_size
    (dest_root / "README_BDO_TEX.txt").write_text(
        "BDO-TEX-AIO texture layer\n"
        "Paths already staged by BDO-AIO/BodyMats were skipped (they win).\n"
        "Playable-class textures appear here only in opt-in GPT/player mode.\n",
        encoding="utf-8",
    )
    log(f"  wrote {total/2**30:.2f} GB")
    log("\nNext: Meta Injector on this PAZ folder "
        "(or BDO-AIO injector). AIO layers were not modified.")
    return 0


def cmd_unstage(cfg: dict, args) -> int:
    ftp = Path(cfg["bdoAioFilesToPatch"]) if cfg["bdoAioFilesToPatch"] else paz_dir(cfg) / "files_to_patch"
    layer = ftp / STAGE_LAYER
    if not layer.is_dir():
        log(f"Nothing staged at {layer}")
        return 0
    n = sum(1 for _ in layer.rglob("*") if _.is_file())
    if not args.yes:
        log(f"Would remove {n:,} files from {layer}. Re-run with --yes to confirm.")
        return 0
    shutil.rmtree(layer)
    log(f"Removed {layer} ({n:,} files).")
    log("Restore the game itself with Meta Injector's backup restore "
        "(or BDO-AIO menu R) — removing the stage alone does not un-inject.")
    return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def cmd_preset(cfg: dict, args) -> int:
    """List or apply a named quality preset (writes config.json)."""
    presets = load_presets()
    if not presets:
        die(f"no presets at {presets_path()}")

    if not args.name:
        log("Quality presets (tools/../presets.json)")
        log("")
        for name, pr in presets.items():
            mark = " *" if cfg.get("activePreset") == name else ""
            same = all(cfg.get(k) == pr.get(k) for k in PRESET_KEYS)
            if same:
                mark = " *"
            log(f"  {name:<14}{mark}  {pr.get('label', name)}")
            log(f"    target {pr.get('target')}  min>{pr.get('minSize')}  "
                f"maxOut {pr.get('maxOutput')}  model {pr.get('model')}")
            if pr.get("description"):
                log(f"    {pr['description']}")
            log("")
        log("Apply:  python tools/bdo_tex.py preset playtest")
        log("        python tools/bdo_tex.py --preset quality scan")
        return 0

    name = args.name.strip().lower()
    # allow aliases
    aliases = {"1": "playtest", "2": "quality", "3": "balanced", "4": "legacy1440", "5": "sharp"}
    name = aliases.get(name, name)
    apply_preset(cfg, name)
    save_config(cfg)
    pr = load_presets()[name]
    log(f"Applied preset: {pr.get('label', name)} ({name})")
    log(f"  target {cfg['target']}px   min>{cfg['minSize']}px   "
        f"maxOut {cfg['maxOutput']}px   model {cfg['model']}")
    log("Re-run scan — the size gate depends on target.")
    log(f"Saved: {APP_DIR / 'config.json'}")
    return 0


def cmd_status(cfg: dict, args) -> int:
    log("=== BDO Texture AIO status ===")
    log(f"game : {cfg['gameDir']}")
    log(f"work : {cfg['workDir']}")
    preset = cfg.get("activePreset") or "custom"
    log(f"preset {preset}")
    log(f"target {cfg['target']}px   min>{cfg['minSize']}px   "
        f"maxOut {cfg.get('maxOutput', 4096)}px   model {cfg['model']}")
    roots = roots_normalized(cfg)
    char_on = any(r.startswith(texfilter.CHARACTER_ROOT) for r in roots)
    log(f"roots: {', '.join(r.rstrip('/') for r in roots)}")
    log(f"character/texture: {'ON' if char_on else 'OFF (default)'}")
    player_on = bool(cfg.get("includePlayerTextures", False))
    log(f"player/GPT textures: {'ON (opt-in)' if player_on else 'OFF (default)'}")
    log(f"upscaleBatch: {cfg.get('upscaleBatch', 32)}")
    log(f"upscayl: gpu={cfg.get('upscaylGpu', '0')!s}  "
        f"tile={cfg.get('upscaylTile', 400)}  "
        f"threads={cfg.get('upscaylThreads', '2:4:2')}  "
        f"(Vulkan/ncnn, not CUDA)")
    lod_on = bool(cfg.get("includeLodBillboards", False))
    log(f"LOD/billboards: {'ON (high)' if lod_on else 'OFF (default)'}")
    mats_on = bool(cfg.get("materialsEnabled", True))
    kinds = materials.companion_kinds_enabled(cfg) if mats_on else []
    log(f"companions: {'ON' if mats_on else 'OFF'}  "
        f"(resize existing only) kinds=[{', '.join('_' + k for k in kinds) or 'none'}]")
    log("")
    meta = paz_dir(cfg) / "pad00000.meta"
    log(f"pad00000.meta: {'found' if meta.is_file() else 'MISSING'}")
    up = Path(cfg["upscaylBin"])
    log(f"upscayl-bin  : {'found' if up.is_file() else 'MISSING - ' + str(up)}")
    log("")
    for key, name in STAGES.items():
        d = Path(cfg["workDir"]) / name
        n = sum(1 for _ in d.rglob("*") if _.is_file()) if d.is_dir() else 0
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0
        log(f"  {name:<20} {n:>8,} files  {size/2**30:>7.2f} GB")
    # Stale manifest vs empty work (common after cleaning work/ or disk wipe)
    man_path = Path(cfg["workDir"]) / "logs" / "manifest.json"
    filt = Path(cfg["workDir"]) / STAGES["filtered"]
    filt_n = sum(1 for _ in filt.glob("*.png")) if filt.is_dir() else 0
    if man_path.is_file() and filt_n == 0:
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
            n_ent = len(man.get("entries") or [])
        except Exception:
            n_ent = 0
        if n_ent:
            log(f"  WARNING: manifest lists {n_ent:,} textures but "
                f"02_filtered_png is empty.")
            log("           Run [1] Scan then [2] Extract before upscale.")
    if filt.is_dir() and not char_on:
        char_n = sum(1 for p in filt.glob("character@texture@*") if p.is_file())
        if char_n:
            log(f"  note: {char_n:,} character@ PNGs still in 02_filtered_png "
                f"(ignored; re-scan/extract or delete them)")
    log("")
    ftp = Path(cfg["bdoAioFilesToPatch"]) if cfg["bdoAioFilesToPatch"] else paz_dir(cfg) / "files_to_patch"
    layer = ftp / STAGE_LAYER
    n = sum(1 for _ in layer.rglob("*") if _.is_file()) if layer.is_dir() else 0
    log(f"staged for inject: {n:,} files at {layer}")
    if ftp.is_dir():
        others = [p.name for p in ftp.iterdir() if p.is_dir() and p.name != STAGE_LAYER]
        log(f"other layers present: {', '.join(others) if others else '(none)'}")
    return 0


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bdo_tex", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--target", type=int, help="override output long-edge target (e.g. 1024, 2048)")
    ap.add_argument("--preset", help="apply quality preset before the command (playtest|quality|balanced|legacy1440|sharp)")
    ap.add_argument("--work-dir", help="override work directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preset", help="list or apply a quality preset (writes config.json)")
    p.add_argument("name", nargs="?", help="preset id, or omit to list")
    p.set_defaults(fn=cmd_preset)

    p = sub.add_parser("scan", help="build the candidate list from pad00000.meta")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("extract", help="PAZ -> PNG")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--keep-dds", action="store_true", help="also save the original DDS")
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("upscale", help="Upscayl pass (basic option)")
    p.add_argument("--gpu", default="")
    p.add_argument("--batch", type=int, default=0,
                   help="images per Upscayl call (default: config upscaleBatch, 24)")
    p.add_argument("--keep-raw", action="store_true")
    p.set_defaults(fn=cmd_upscale)

    p = sub.add_parser(
        "materials",
        help="resize EXISTING companion maps (_n/_sp/...) to match upscaled albedo",
    )
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true",
                   help="run even if materialsEnabled is false")
    p.set_defaults(fn=cmd_materials)

    p = sub.add_parser("swarm-export", help="copy PNGs out for a GPT / SwarmUI / ComfyUI pass")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_swarm_export)

    p = sub.add_parser("gpt", help="alias for swarm-export: hand PNGs to GPT for upscaling")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_swarm_export)

    p = sub.add_parser("pack", help="PNG -> DDS with mip chain (albedo + materials)")
    p.add_argument("--source", choices=["upscayl", "swarm", "gpt"], default="upscayl",
                   help="upscayl, or an external pass (gpt/swarm output PNGs)")
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("stage", help="copy into files_to_patch for Meta Injector")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_stage)

    p = sub.add_parser("unstage", help="remove this app's layer from files_to_patch")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_unstage)

    sub.add_parser("status", help="show pipeline state").set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "preset", None):
        apply_preset(cfg, args.preset.strip().lower())
        # Persist when used as a global flag so menu/status stay in sync
        if args.cmd != "preset":
            save_config(cfg)
            log(f"Preset '{args.preset}' applied and saved to config.json")
    if args.target:
        cfg["target"] = args.target
    if args.work_dir:
        cfg["workDir"] = str(resolve_work_dir(args.work_dir))
    else:
        cfg["workDir"] = str(resolve_work_dir(cfg.get("workDir")))
    Path(cfg["workDir"]).mkdir(parents=True, exist_ok=True)
    # Hard isolation notice once if someone still points outside the app
    try:
        Path(cfg["workDir"]).resolve().relative_to(APP_DIR.resolve())
    except ValueError:
        log(f"NOTE: workDir is outside the app folder: {cfg['workDir']}")
        log(f"      Recommended isolated path: {APP_DIR / DEFAULT_WORK_DIRNAME}")
    return args.fn(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
