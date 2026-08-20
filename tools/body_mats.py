#!/usr/bin/env python3
"""BDO-AIO BodyMats - enhance bodies AFTER BDO-AIO choices (never replaces them).

Authority
---------
BDO-AIO is where you pick bodies, pubes, genital packs, outfit edits.
This addon only UPGRADES the textures that BDO-AIO already staged into
files_to_patch. It never writes into BDO-AIO's install folder, never edits
AIO layers (_midnight_*, _pubic_hair_*, _genital_*, ...), and never pulls a
different body from the pack when AIO already made a choice.

Workflow
--------
1. BDO-AIO  -> deploy choices to Paz/files_to_patch
2. BodyMats -> scan winners, upscale undersized skins, resize existing
               companions, stage layer _bdo_aio_bodymats (same paths,
               higher-res pixels of YOUR choices)
3. PartCutGen (if used) then Meta Injector

Source
------
Primary and required: game files_to_patch (post-AIO), winner per basename
with priority pubic_hair > genital > midnight. Pack-folder fallback is OFF
by default so a stale pack cannot reintroduce bodies you did not pick.

Companions: resize only maps that already exist next to the winning albedo.
Does not invent _n/_sp (Meta Injector rejects paths not in pad00000.meta).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dds  # noqa: E402  bundled DDS writer (full mip chain, no external tool)
# materials.py kept only if texconv path needs it; invent generators unused.


APP_DIR = Path(__file__).resolve().parent.parent

MATERIAL_SUFFIXES = ("_n", "_nm", "_normal", "_sp", "_s", "_spec", "_m", "_mask", "_ao", "_dm", "_em", "_w", "_p", "_h", "_height")
NUDE_ALBEDO_RE = re.compile(r"(^|_)nude(_|$)|_00_nude|_01_nude", re.I)
CLASS_PREFIX_RE = re.compile(
    r"^(phw|pew|pbw|pvw|pww|pgw|pnw|plw|pdw|pcw|psw|ppw|pkww|pfw|pqw|pkow|pmyf|pnyw|pwge|pdkl|"
    r"phm|pgm|pkm|pwm|pwmm|pem|pnm|pcm|pam|ppm|prsa|pgms)_",
    re.I,
)


# Higher score wins when the same basename exists in several files_to_patch layers.
# Pubic-composited nudes beat genital packs beat midnight base nudes.
LAYER_PRIORITY = (
    ("_pubic_hair", 300),
    ("_genital", 200),
    ("_midnight", 100),
    ("_censorship", 50),
)

# Never treat our own previous output as an input source.
# Censorship / slot-hide / world-tex are not body-skin material targets.
SKIP_SOURCE_LAYER_PREFIXES = (
    "_bdo_aio_bodymats",
    "_bdo_tex_upscale",
    "_bdo_mat_softbind_test",
    "_partcutgen",
    "_body_size",
    "_slot_hide",
    "_censorship",
)

# Layers that belong to BDO-AIO (or its packs). We never write INTO these dirs.
AIO_LAYER_HINTS = (
    "_midnight",
    "_pubic_hair",
    "_genital",
    "_censorship",
    "_slot_hide",
    "_body_size",
    "_partcutgen",
    "_player",
)


@dataclass
class Job:
    source: str
    stem: str
    kind: str  # albedo | already_material
    w: int
    h: int
    albedo_target: int
    needs_albedo_upscale: bool
    # Existing companion paths that need a size match (kind -> absolute path).
    companions: dict = field(default_factory=dict)
    source_layer: str = ""
    archive_rel: str = ""  # character/texture/foo.dds
    # legacy invent list from older scans — ignored
    missing: list = field(default_factory=list)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def default_game_paz() -> Path:
    return Path(r"C:\Program Files (x86)\Steam\steamapps\common\Black Desert Online\Paz")


def resolve_files_to_patch(cfg: dict) -> Path:
    explicit = (cfg.get("gameFilesToPatch") or cfg.get("bdoAioFilesToPatch") or "").strip()
    if explicit:
        return Path(explicit)
    paz = Path(
        cfg.get("gamePaz")
        or (Path(cfg["gameDir"]) / "Paz" if cfg.get("gameDir") else "")
        or default_game_paz()
    )
    return paz / "files_to_patch"


def load_config(path: Path | None = None) -> dict:
    cfg_path = path or (APP_DIR / "config.json")
    example = APP_DIR / "config.example.json"
    if not cfg_path.is_file():
        if example.is_file():
            shutil.copy2(example, cfg_path)
            log(f"Created {cfg_path} from example")
        else:
            die(f"missing config: {cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    # Resolve relative dirs under APP_DIR
    for key in ("workDir", "bodyWorkDir", "exportDir"):
        p = Path(cfg.get(key, key.replace("Dir", "").lower() or "work"))
        if not p.is_absolute():
            p = APP_DIR / p
        cfg[key] = str(p.resolve())
    return cfg


def app_paths(cfg: dict) -> dict[str, Path]:
    work = Path(cfg.get("bodyWorkDir") or cfg["workDir"])
    paths = {
        "work": work,
        "src": work / "01_src_png",
        "albedo": work / "02_albedo_png",
        "mats": work / "03_materials_png",
        "packed": work / "04_packed_dds",
        "export": Path(cfg["exportDir"]),
        "logs": work / "logs",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def is_material_name(stem: str) -> bool:
    low = stem.lower()
    return any(low.endswith(s) for s in MATERIAL_SUFFIXES)


def material_kind(stem: str) -> str | None:
    low = stem.lower()
    for s, kind in (
        ("_normal", "n"),
        ("_nm", "n"),
        ("_n", "n"),
        ("_spec", "sp"),
        ("_sp", "sp"),
        ("_s", "sp"),
        ("_mask", "m"),
        ("_m", "m"),
    ):
        if low.endswith(s):
            return kind
    return None


def albedo_stem(stem: str) -> str:
    low = stem.lower()
    for s in sorted(MATERIAL_SUFFIXES, key=len, reverse=True):
        if low.endswith(s):
            return stem[: -len(s)]
    return stem


def is_player_nude_skin(stem: str) -> bool:
    """Playable-class nude body albedo (not a material suffix)."""
    if is_material_name(stem):
        return False
    if not NUDE_ALBEDO_RE.search(stem):
        return False
    return bool(CLASS_PREFIX_RE.match(stem))


def albedo_target_for(stem: str, cfg: dict) -> int:
    if is_player_nude_skin(stem):
        return int(cfg.get("skinAlbedoTarget", 4096))
    return int(cfg.get("otherAlbedoTarget", 2048))


def dds_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size


def open_image(path: Path) -> Image.Image:
    im = Image.open(path)
    return im.convert("RGBA")


def layer_score(layer_name: str) -> int:
    low = layer_name.lower()
    best = 0
    for prefix, score in LAYER_PRIORITY:
        if low.startswith(prefix) or prefix in low:
            best = max(best, score)
    return best


def skip_layer(layer_name: str) -> bool:
    low = layer_name.lower()
    return any(low.startswith(p) for p in SKIP_SOURCE_LAYER_PREFIXES)


def is_body_related_dds(path: Path) -> bool:
    """Nude body skin (+ material siblings) only - not armor / underwear / censorship."""
    if path.suffix.lower() != ".dds":
        return False
    name = path.name.lower()
    stem = path.stem
    # Albedo or material must be nude-family
    base = albedo_stem(stem).lower()
    if "nude" not in base and "nude" not in name:
        return False
    if not CLASS_PREFIX_RE.match(stem) and not CLASS_PREFIX_RE.match(albedo_stem(stem)):
        # still allow oddly named nude maps
        if "nude" not in name:
            return False
    parts = [p.lower() for p in path.parts]
    if "character" in parts and "texture" in parts:
        return True
    if path.parent.name.lower() == "texture":
        return True
    return "nude" in name


def archive_rel_for(path: Path, layer_root: Path | None = None) -> str:
    """Map a staged file to Meta Injector internal path character/texture/foo.dds."""
    name = path.name
    if layer_root:
        try:
            rel = path.relative_to(layer_root).as_posix().lower()
            # strip organizer prefixes until character/ or bare file
            parts = rel.split("/")
            if "character" in parts:
                i = parts.index("character")
                return "/".join(parts[i:])
            if parts[-1].endswith(".dds"):
                return f"character/texture/{parts[-1]}"
        except ValueError:
            pass
    return f"character/texture/{name}"


def collect_from_files_to_patch(ftp: Path) -> tuple[list[Path], dict[str, str], str]:
    """Return winning body DDS paths from post-AIO files_to_patch.

    Winner key = lowercase basename. Higher layer_score wins (pubic > genital > midnight).
    """
    if not ftp.is_dir():
        return [], {}, "missing"

    winners: dict[str, tuple[int, Path, str]] = {}  # basename -> (score, path, layer)
    layers_seen = 0
    for layer_dir in sorted(ftp.iterdir()):
        if not layer_dir.is_dir():
            continue
        layer = layer_dir.name
        if skip_layer(layer):
            continue
        layers_seen += 1
        score = layer_score(layer)
        for dds_file in layer_dir.rglob("*.dds"):
            if not is_body_related_dds(dds_file):
                continue
            key = dds_file.name.lower()
            prev = winners.get(key)
            if prev is None or score >= prev[0]:
                winners[key] = (score, dds_file, layer)

    if not winners:
        return [], {}, f"empty_body_dds (layers_scanned={layers_seen})"

    paths = [t[1] for t in winners.values()]
    layer_of = {str(t[1].resolve()): t[2] for t in winners.values()}
    return paths, layer_of, f"files_to_patch ({len(paths)} winning body dds, {layers_seen} layers)"


def collect_from_aio_fallback(cfg: dict) -> list[Path]:
    root = Path(cfg["bdoAioRoot"])
    if not root.is_dir():
        return []
    found: list[Path] = []
    for pattern in cfg.get("sourceGlobs") or []:
        found.extend(root.glob(pattern))
    uniq: dict[str, Path] = {}
    for p in found:
        if p.is_file() and p.suffix.lower() == ".dds" and is_body_related_dds(p):
            uniq[p.name.lower()] = p
    return list(uniq.values())


def collect_sources(cfg: dict) -> tuple[list[Path], dict[str, str], str]:
    """Post-AIO files_to_patch winners only (AIO choices).

    Pack-folder fallback is disabled unless allowPackFallback=true, so we never
    silently enhance a body the user did not deploy with BDO-AIO.
    """
    ftp = resolve_files_to_patch(cfg)
    paths, layer_of, how = collect_from_files_to_patch(ftp)
    if paths:
        return paths, layer_of, how

    if cfg.get("allowPackFallback", False):
        fb = collect_from_aio_fallback(cfg)
        layer_of = {str(p.resolve()): "bdo_aio_pack_fallback" for p in fb}
        if fb:
            return fb, layer_of, (
                f"FALLBACK pack globs ({len(fb)} files) - "
                "WARNING: not your AIO deploy; set allowPackFallback false "
                "and run BDO-AIO first for correct choices"
            )
    return [], {}, (
        f"no AIO body DDS in files_to_patch ({ftp}). "
        "Run BDO-AIO deploy first (bodies/pubes). "
        "Pack fallback is off so we do not override your AIO choices."
    )


def plan_jobs(cfg: dict, sources: list[Path], layer_of: dict[str, str] | None = None) -> list[Job]:
    layer_of = layer_of or {}
    # Group by albedo stem; materials share the stem
    by_stem: dict[str, dict[str, Path]] = {}
    for p in sources:
        stem = p.stem
        if is_material_name(stem):
            base = albedo_stem(stem)
            kind = material_kind(stem) or "other"
            by_stem.setdefault(base, {})[kind] = p
        else:
            by_stem.setdefault(stem, {})["albedo"] = p

    # Also discover material siblings next to each albedo (same folder)
    companion_suffixes = (
        ("n", "_n"), ("nm", "_nm"), ("sp", "_sp"), ("m", "_m"),
        ("w", "_w"), ("ao", "_ao"), ("p", "_p"),
    )
    for stem, maps in list(by_stem.items()):
        alb = maps.get("albedo")
        if not alb:
            continue
        parent = alb.parent
        for kind, suffix in companion_suffixes:
            if kind in maps:
                continue
            cand = parent / f"{stem}{suffix}.dds"
            if cand.is_file():
                maps[kind] = cand

    jobs: list[Job] = []
    min_edge = int(cfg.get("minAlbedoEdge", 256))
    for stem, maps in sorted(by_stem.items()):
        albedo_path = maps.get("albedo")
        if not albedo_path:
            continue
        # Nude skins only (bodies / pube bases)
        if "nude" not in stem.lower():
            continue
        try:
            w, h = dds_size(albedo_path)
        except Exception as e:
            log(f"  skip unreadable {albedo_path.name}: {e}")
            continue
        edge = max(w, h)
        if edge < min_edge:
            continue
        target = albedo_target_for(stem, cfg)
        needs_up = edge < target
        # Final albedo long edge after process (for companion size match).
        final_edge = target if needs_up else edge

        # Only EXISTING companions that are smaller than final albedo size.
        companions: dict[str, str] = {}
        for kind, path in maps.items():
            if kind == "albedo":
                continue
            try:
                mw, mh = dds_size(path)
            except Exception:
                continue
            if max(mw, mh) < final_edge:
                companions[kind] = str(path)

        # Skip jobs that need nothing
        if not needs_up and not companions:
            continue

        src_key = str(albedo_path.resolve())
        layer = layer_of.get(src_key, "")
        jobs.append(
            Job(
                source=str(albedo_path),
                stem=stem,
                kind="albedo",
                w=w,
                h=h,
                albedo_target=target,
                needs_albedo_upscale=needs_up,
                companions=companions,
                source_layer=layer,
                archive_rel=archive_rel_for(albedo_path),
                missing=[],
            )
        )
    return jobs


def snap4(n: int) -> int:
    return max(4, int(round(n / 4.0)) * 4)


def resize_long_edge(im: Image.Image, target: int, *, grow_only: bool = True) -> Image.Image:
    """Scale so long edge becomes target. grow_only=True never shrinks."""
    w, h = im.size
    edge = max(w, h)
    if edge <= 0:
        return im
    if grow_only and edge >= target:
        return im
    if not grow_only and edge == target:
        return im
    scale = target / edge
    nw, nh = snap4(int(w * scale)), snap4(int(h * scale))
    return im.resize((nw, nh), Image.Resampling.LANCZOS)


def is_blank_rgb(im: Image.Image) -> bool:
    """upscayl-bin can exit 0, print success, and still write an empty image."""
    ex = im.convert("RGB").getextrema()
    return all(lo == 0 and hi == 0 for lo, hi in ex)


def upscayl_or_resize(cfg: dict, src_png: Path, dest_png: Path, target: int) -> None:
    """Upscale one texture, keeping aspect ratio and alpha intact.

    Alpha never goes through the model: measured, RGBA input makes upscayl-bin
    emit fully blank images under load, and an AI model on a cutout mask
    invents soft edges anyway. RGB goes to the model, alpha is Lanczos-resized
    and re-attached. The result is validated; a blank falls back to LANCZOS
    rather than silently shipping an invisible body texture.
    """
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    original = Image.open(src_png).convert("RGBA")
    alpha = original.getchannel("A")
    has_alpha = alpha.getextrema()[0] < 255
    final = resize_long_edge(original, target).size  # aspect-preserving target

    bin_path = Path(cfg.get("upscaylBin") or "")
    models = Path(cfg.get("upscaylModels") or "")
    model = cfg.get("upscaylModel") or cfg.get("model") or "high-fidelity-4x"
    if bin_path.is_file() and models.is_dir():
        tmp_dir = dest_png.parent / "_upscayl_tmp"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        (tmp_dir / "in").mkdir(parents=True)
        (tmp_dir / "out").mkdir(parents=True)
        rgb_in = tmp_dir / "in" / src_png.name
        original.convert("RGB").save(rgb_in, format="PNG", compress_level=1)
        # No -r: this Upscayl build ignores it (verified), and it would force a
        # square output, distorting every non-square atlas.
        cmd = [str(bin_path), "-i", str(tmp_dir / "in"), "-o", str(tmp_dir / "out"),
               "-m", str(models), "-n", model, "-s", "4", "-f", "png"]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        produced = tmp_dir / "out" / src_png.name
        if proc.returncode == 0 and produced.is_file():
            up = Image.open(produced).convert("RGB")
            if not is_blank_rgb(up):
                if up.size != final:
                    up = up.resize(final, Image.LANCZOS)
                out = up
                if has_alpha:
                    a = alpha.resize(final, Image.LANCZOS)
                    out = up.convert("RGBA")
                    out.putalpha(a)
                out.save(dest_png, format="PNG")
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return
            log(f"  upscayl returned a BLANK image for {src_png.name} "
                f"(exit 0) - falling back to LANCZOS")
        else:
            log(f"  upscayl failed for {src_png.name}, falling back to LANCZOS")
        shutil.rmtree(tmp_dir, ignore_errors=True)

    im = resize_long_edge(original, target)
    im.save(dest_png, format="PNG")


BC_TO_FOURCC = {"BC1_UNORM": "DXT1", "BC2_UNORM": "DXT5", "BC3_UNORM": "DXT5"}


def pack_png_to_dds(cfg: dict, png: Path, dds_path: Path, *, bc: str = "BC3_UNORM") -> None:
    """PNG -> DDS with a full mip chain.

    Uses the bundled pure-Python writer (dds.py) so the addon works with no
    external tool installed. texconv is still honoured when configured, for
    anyone who prefers it.
    """
    dds_path.parent.mkdir(parents=True, exist_ok=True)
    texconv = Path(cfg.get("texconv") or "")
    if texconv.is_file():
        cmd = [str(texconv), "-nologo", "-y", "-f", bc, "-m", "0",
               "-o", str(dds_path.parent), str(png)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        produced = dds_path.parent / (png.stem + ".dds")
        if proc.returncode == 0 and produced.is_file():
            if produced.resolve() != dds_path.resolve():
                shutil.move(str(produced), str(dds_path))
            return
        log(f"  texconv fail {png.name}: {(proc.stderr or proc.stdout)[-200:]}")

    fourcc = BC_TO_FOURCC.get(bc.upper(), "DXT5")
    with Image.open(png) as im:
        blob = dds.write(im.convert("RGBA"), fourcc, mipmaps=True)
    dds_path.write_bytes(blob)


def cmd_scan(cfg: dict, _args) -> int:
    paths = app_paths(cfg)
    ftp = resolve_files_to_patch(cfg)
    log("=== POST-AIO workflow scan ===")
    log("Prerequisite: run BDO-AIO first (bodies / pubes / genital -> files_to_patch).")
    log(f"files_to_patch: {ftp}")
    log(f"exists: {ftp.is_dir()}")
    sources, layer_of, how = collect_sources(cfg)
    jobs = plan_jobs(cfg, sources, layer_of)
    report = {
        "workflow": "AFTER BDO-AIO -> BodyMats -> PartCutGen -> Meta Injector",
        "bdo_aio_root": cfg.get("bdoAioRoot", ""),
        "files_to_patch": str(ftp),
        "source_mode": how,
        "addon_root": str(APP_DIR),
        "source_count": len(sources),
        "job_count": len(jobs),
        "rules": {
            "skin_albedo_target": cfg.get("skinAlbedoTarget", 4096),
            "other_albedo_target": cfg.get("otherAlbedoTarget", 2048),
            "note": "Upscale albedo if below target. Resize EXISTING companions only (no invent).",
            "layer_priority": "pubic_hair > genital > midnight",
        },
        "jobs": [asdict(j) for j in jobs],
    }
    out = paths["logs"] / "scan.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log(f"Source mode: {how}")
    log(f"Jobs needing work: {len(jobs)}")
    need_up = sum(1 for j in jobs if j.needs_albedo_upscale)
    need_mat = sum(1 for j in jobs if j.companions)
    log(f"  albedo upscale needed: {need_up}")
    log(f"  companion resize:      {need_mat}")
    if "FALLBACK" in how:
        log("WARNING: files_to_patch had no body DDS - run BDO-AIO deploy first for correct pubes/bodies.")
    for j in jobs[:15]:
        tag = "SKIN->4096" if j.albedo_target >= 4096 else f"->{j.albedo_target}"
        layer = j.source_layer or "?"
        comps = list((j.companions or {}).keys()) or "-"
        log(f"  {j.stem:32} {j.w}x{j.h}  {tag}  companions={comps}  [{layer}]")
    if len(jobs) > 15:
        log(f"  ... +{len(jobs)-15} more")
    log(f"Wrote {out}")
    log("Next: process -> stage -> PartCutGen -> Meta Injector. BDO-AIO not modified.")
    return 0


def _load_jobs(cfg: dict) -> list[Job]:
    paths = app_paths(cfg)
    scan = paths["logs"] / "scan.json"
    if not scan.is_file():
        sources, layer_of, _ = collect_sources(cfg)
        return plan_jobs(cfg, sources, layer_of)
    data = json.loads(scan.read_text(encoding="utf-8-sig"))
    jobs = []
    allowed = {f.name for f in fields(Job)}
    for row in data.get("jobs", []):
        # tolerate older scans missing new fields / invent-era "missing" list
        row.setdefault("source_layer", "")
        row.setdefault("archive_rel", f"character/texture/{row.get('stem', 'x')}.dds")
        row.setdefault("companions", {})
        row.setdefault("missing", [])
        # Drop invent-only jobs from old scans (had missing mats, no upscale)
        if not row.get("needs_albedo_upscale") and not row.get("companions"):
            continue
        row["missing"] = []
        clean = {k: v for k, v in row.items() if k in allowed}
        jobs.append(Job(**clean))
    return jobs


def cmd_process(cfg: dict, args) -> int:
    paths = app_paths(cfg)
    jobs = _load_jobs(cfg)
    if args.limit:
        jobs = jobs[: args.limit]
    if not jobs:
        die("no jobs - run scan first or check sourceGlobs / bdoAioRoot")

    manifest = []
    for i, job in enumerate(jobs, 1):
        src = Path(job.source)
        comps = job.companions or {}
        log(f"[{i}/{len(jobs)}] {job.stem}  {job.w}x{job.h}  "
            f"companions={list(comps) or '-'}")
        # 1) load albedo
        try:
            albedo = open_image(src)
        except Exception as e:
            log(f"  FAIL open: {e}")
            continue
        src_png = paths["src"] / f"{job.stem}.png"
        albedo.save(src_png, format="PNG")

        # 2) upscale albedo only if below target
        alb_png = paths["albedo"] / f"{job.stem}.png"
        if job.needs_albedo_upscale:
            log(f"  upscale albedo -> {job.albedo_target}px long edge")
            upscayl_or_resize(cfg, src_png, alb_png, job.albedo_target)
            albedo = Image.open(alb_png).convert("RGBA")
        else:
            log(f"  albedo already >= {job.albedo_target} - skip upscale")
            shutil.copy2(src_png, alb_png)
            albedo = Image.open(alb_png).convert("RGBA")

        max_edge = int(cfg.get("materialMaxEdge", 4096))
        if max(albedo.size) > max_edge:
            albedo = resize_long_edge(albedo, max_edge, grow_only=False)
            albedo.save(alb_png, format="PNG")

        final_w, final_h = albedo.size
        row = {
            "stem": job.stem,
            "source": job.source,
            "albedo_size": list(albedo.size),
            "upscaled": job.needs_albedo_upscale,
            "materials": {},
        }

        # 3) resize EXISTING companions to final albedo size (no invent)
        for kind, cpath in comps.items():
            cp = Path(cpath)
            if not cp.is_file():
                log(f"  skip missing companion _{kind}: {cp}")
                continue
            log(f"  resize existing _{kind} -> {final_w}x{final_h}")
            try:
                mat = open_image(cp).convert("RGBA")
                if mat.size != (final_w, final_h):
                    mat = mat.resize((final_w, final_h), Image.LANCZOS)
                # Preserve real suffix from source filename when possible
                suffix = cp.stem[len(job.stem):] if cp.stem.startswith(job.stem) else f"_{kind}"
                mat_png = paths["mats"] / f"{job.stem}{suffix}.png"
                mat.save(mat_png, format="PNG")
                row["materials"][kind] = str(mat_png)
            except Exception as e:
                log(f"  FAIL companion _{kind}: {e}")

        # 4) pack DDS into export/character/texture/
        export_tex = paths["export"] / "character" / "texture"
        export_tex.mkdir(parents=True, exist_ok=True)
        try:
            dds_a = export_tex / f"{job.stem}.dds"
            pack_png_to_dds(cfg, alb_png, dds_a, bc="BC1_UNORM")
            row["export_albedo"] = str(dds_a)
            for kind, png in list(row["materials"].items()):
                if not str(png).lower().endswith(".png"):
                    continue
                p = Path(png)
                # Keep original companion basename (stem+suffix).dds
                dds_m = export_tex / (p.stem + ".dds")
                pack_png_to_dds(cfg, p, dds_m, bc="BC3_UNORM")
                row["materials"][kind] = str(dds_m)
        except RuntimeError as e:
            log(f"  pack warn: {e}")

        row["source_layer"] = job.source_layer
        manifest.append(row)

    man_path = paths["logs"] / "manifest.json"
    man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"Done. Export: {paths['export']}")
    log(f"Manifest: {man_path}")
    log("Next: stage -> PartCutGen -> Meta Injector. BDO-AIO was NOT modified.")
    return 0


def _is_aio_owned_layer_name(name: str) -> bool:
    low = name.lower()
    if skip_layer(name):
        return False
    return any(h in low for h in AIO_LAYER_HINTS)


def cmd_stage(cfg: dict, args) -> int:
    """Copy export into OUR layer only. Never touch AIO layer folders."""
    paths = app_paths(cfg)
    export = paths["export"]
    dds_files = list(export.rglob("*.dds"))
    if not dds_files and not any(export.rglob("*.png")):
        die("export empty - run process first")

    layer = cfg.get("stageLayerName") or "_bdo_aio_bodymats"
    if not layer.startswith("_"):
        die(f"stageLayerName must be an organizer folder starting with '_': {layer}")
    if _is_aio_owned_layer_name(layer) or layer.lower() in (
        "_midnight_xyzw", "_pubic_hair_perclass",
    ):
        die(f"refusing to use AIO layer name as our stage: {layer}")

    ftp = resolve_files_to_patch(cfg)
    dest_root = ftp / layer

    # Hard rule: never write under BDO-AIO install
    aio_root = Path(cfg.get("bdoAioRoot") or "").resolve()
    if aio_root.is_dir():
        try:
            dest_root.resolve().relative_to(aio_root)
            die(f"refusing to stage inside BDO-AIO install: {dest_root}")
        except ValueError:
            pass

    # Guard: AIO choices should already be in files_to_patch
    aio_layers = []
    if ftp.is_dir():
        aio_layers = [
            p.name for p in ftp.iterdir()
            if p.is_dir() and _is_aio_owned_layer_name(p.name)
        ]
    other = []
    if ftp.is_dir():
        other = [
            p.name for p in ftp.iterdir()
            if p.is_dir() and p.name != layer and not skip_layer(p.name)
        ]
    if not aio_layers and not args.force:
        die(
            "files_to_patch has no BDO-AIO body/pube layers yet.\n"
            "  BDO-AIO must run FIRST so your body/pube choices exist.\n"
            "  1) BDO-AIO: pick bodies/pubes and DEPLOY\n"
            "  2) BodyMats: scan -> process -> stage\n"
            "  Or pass --force only if you know what you are doing."
        )

    # Only stage files we produced for stems that came from AIO winners
    # (export is already limited by process jobs from collect_sources).
    if args.dry_run:
        log(f"DRY-RUN would copy {len(dds_files)} DDS -> {dest_root}")
        log(f"AIO choice layers present: {', '.join(sorted(aio_layers)) or '(none)'}")
        log(f"Other layers: {', '.join(sorted(other)) or '(none)'}")
        log("AIO layer folders will NOT be modified.")
        return 0

    if dest_root.exists():
        shutil.rmtree(dest_root)
    # Copy tree but never into sibling AIO dirs
    shutil.copytree(export, dest_root)

    readme = dest_root / "README_BODYMATS.txt"
    readme.write_text(
        "BDO-AIO-BodyMats enhancement layer\n"
        "==================================\n"
        "Contains higher-res versions of bodies/pubes YOU already chose in BDO-AIO.\n"
        "Does not change which body/pube pack is selected - only pixel quality.\n"
        "BDO-AIO layer folders were not modified.\n"
        "Order: BDO-AIO deploy -> BodyMats stage -> PartCutGen -> Meta Injector\n"
        f"Addon: {APP_DIR}\n",
        encoding="utf-8",
    )

    log(f"Staged -> {dest_root}")
    log(f"AIO choice layers left untouched: {', '.join(sorted(aio_layers)) or '(none)'}")
    log("")
    log("Authority: BDO-AIO owns which bodies/pubes. This layer only upgrades them.")
    log("Next: PartCutGen (if used) -> Meta Injector on the game PAZ folder.")
    log("BDO-AIO install folder was not changed.")
    return 0


def cmd_status(cfg: dict, _args) -> int:
    paths = app_paths(cfg)
    ftp = resolve_files_to_patch(cfg)
    log("=== BDO-AIO BodyMats (post-AIO addon) ===")
    log(f"addon : {APP_DIR}")
    log(f"BDO-AIO pack (fallback only): {cfg.get('bdoAioRoot', '')}")
    log(f"files_to_patch (PRIMARY source): {ftp}")
    log(f"  exists: {ftp.is_dir()}")
    if ftp.is_dir():
        layers = [p.name for p in ftp.iterdir() if p.is_dir()]
        log(f"  layers: {', '.join(layers) or '(none)'}")
    log(f"work  : {paths['work']}")
    log(f"export: {cfg['exportDir']}")
    log(f"stage : {ftp / (cfg.get('stageLayerName') or '_bdo_aio_bodymats')}")
    log(f"skin albedo target : {cfg.get('skinAlbedoTarget', 4096)}")
    log(f"other albedo target: {cfg.get('otherAlbedoTarget', 2048)}")
    log("")
    log("Workflow: BDO-AIO -> BodyMats (scan/process/stage) -> PartCutGen -> Meta Injector")
    for name in ("01_src_png", "02_albedo_png", "03_materials_png", "04_packed_dds"):
        d = paths['work'] / name
        n = sum(1 for _ in d.rglob("*") if _.is_file()) if d.is_dir() else 0
        log(f"  {name}: {n} files")
    ex = Path(cfg["exportDir"])
    n = sum(1 for _ in ex.rglob("*") if _.is_file()) if ex.is_dir() else 0
    log(f"  export: {n} files")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="list nude/body jobs from BDO-AIO (read-only)").set_defaults(fn=cmd_scan)

    p = sub.add_parser(
        "process",
        help="upscale undersized albedos + resize EXISTING companion maps only",
    )
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_process)

    p = sub.add_parser("stage", help="copy export to files_to_patch/_bdo_aio_bodymats (after AIO)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="stage even if no AIO layers present")
    p.set_defaults(fn=cmd_stage)

    sub.add_parser("status", help="show paths and counts").set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    return args.fn(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
