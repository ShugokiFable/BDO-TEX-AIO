#!/usr/bin/env python3
"""Which BDO textures may be AI-upscaled, and which must never be touched.

Every rule here was derived from the live pad00000.meta index (842,628 files /
294,245 .dds), not from assumption. See README.md for the measured counts.

Two independent safety properties:

  1. Only *colour* textures are eligible. Normal / specular / mask / AO /
     displacement / emissive maps encode vectors or coefficients, not colour;
     an AI upscaler invents plausible-looking detail that is physically wrong
     in those channels, so they are excluded by suffix.

  2. Nothing a player character wears or is, is eligible. Playable-class assets
     under character/texture/ are named with a class prefix (phw, pew, pkww,
     prsa, ...) and are exactly the surface BDO-AIO / Midnight already mods.
     Excluding the whole p* namespace guarantees the two mods cannot fight.
     NPCs (n*), monsters (m####) and everything else stay eligible.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Path allowlist. Everything not under one of these roots is out of scope.
#
# CHARACTER_ROOT is allowed by classify() but OFF by default in config:
# NPC/monster skins are usually already high-res enough; enabling them
# roughly doubles the job count. World roots are the normal pipeline.
# --------------------------------------------------------------------------
CHARACTER_ROOT = "character/texture/"

# Default scan/extract roots (world only). Character is opt-in via config.
DEFAULT_ROOTS: tuple[str, ...] = (
    "object/texture/",           # houses, props, world weapons, structures
    "speedtreedata/texture/",    # trees and foliage
    "texture/terraindetailmap/", # tiling ground detail (grass / rock / dirt)
)

# Full set classify() may accept (includes optional character).
INCLUDE_ROOTS: tuple[str, ...] = (
    CHARACTER_ROOT,              # NPCs, monsters, mounts (player classes filtered below)
    *DEFAULT_ROOTS,
)

# Excluded roots, with the reason each one is a bad upscale target. These are
# siblings of the allowlist, listed so the intent survives a future edit.
EXCLUDED_ROOTS = {
    "ui_texture/": "UI and icons — upscaling breaks pixel-exact layout",
    "ui_customize/": "UI",
    "character/texture_thumbnail/": "thumbnails",
    "object/texture_thumbnail/": "thumbnails",
    "effect/": "particle sprites and gradients — alpha ramps, not colour art",
    "mapdata_real/terraincolortexture/": "baked 128x128 per-sector maps (50,974 of them)",
    "mapdata_real/hloddata/": "distance LOD imposters",
    "mapdata_real/sectormapinfo_combine/": "packed sector data",
    "mapdata_real/probe/": "lighting probes",
    "mapdata_instancedungeon/": "packed sector data",
    "texture/lut/": "colour grading lookup tables — resampling destroys them",
    "texture/water/": "flow and normal data",
}

# --------------------------------------------------------------------------
# Non-colour channel suffixes (final _token of the basename).
# Measured counts in the live index are in the comments.
# --------------------------------------------------------------------------
NON_COLOR_SUFFIXES: frozenset[str] = frozenset({
    "n",       # 17,142  normal map
    "nm",      #         normal map (alt spelling)
    "normal",  #  1,811  normal map
    "sp",      # 17,168  specular
    "s",       #         specular (alt)
    "spec",
    "m",       #  7,686  mask / metallic
    "mask",
    "msk",
    "ao",      #  2,162  ambient occlusion
    "dm",      #  1,850  detail / displacement map
    "em",      #  1,919  emissive mask
    "p",       #    924  parallax / height
    "height",
    "disp",
})

# Basename tokens that mark distance / impostor art. AI-upscaling these is
# wasteful and can pop worse at range (SpeedTree billboards, mesh LODs).
# Token match is underscore-split only: "farm" is NOT "far".
LOD_TOKENS: frozenset[str] = frozenset({
    "lod", "hlod", "far",
    "billboard", "billboards",
    "impostor", "imposter", "impost",
})

# Substrings anywhere in the path (folder or name) that always mean impostor
# / distance data. mapdata hlod roots are already EXCLUDED_ROOTS; this catches
# speedtreedata/texture/*_billboards.dds and similar under allowlisted roots.
IMPOSTOR_PATH_MARKERS: tuple[str, ...] = (
    "/hloddata/",
    "/hlod/",
    "_billboards",
    "_billboard",
    "/billboards/",
    "/billboard/",
)

# Playable-class prefix under character/texture/: 'p' + 1-4 letters as the first
# token (phm, pvw, pkww, prsa, pmyf, pdkl...). Deliberately a superset of the
# classes that exist today so a class added in a future patch is excluded by
# default — over-exclusion costs a missed texture, under-exclusion collides
# with BDO-AIO.
PLAYER_PREFIX_RE = re.compile(r"^p[a-z]{1,4}$")

# The class prefixes BDO-AIO/Midnight is known to own, kept explicit so the
# overlap guarantee is auditable and testable rather than only regex-implied.
KNOWN_PLAYER_PREFIXES: frozenset[str] = frozenset({
    "phw", "pew", "pbw", "pvw", "pww", "pgw", "pnw", "plw", "pdw", "pcw",
    "psw", "ppw", "pkww", "pfw", "pqw", "pkow", "pmyf", "pnyw", "pwge",
    "pdkl", "phm", "pgm", "pkm", "pwm", "pwmm", "pem", "pnm", "pcm", "pam",
    "ppm", "prsa", "pgms", "pkw", "prw", "pjkd",
})


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def is_lod_or_billboard(path: str) -> tuple[bool, str]:
    """True if path is distance LOD / billboard / impostor art."""
    p = path.lower().replace("\\", "/")
    name = _basename(p)
    if not name.endswith(".dds"):
        return False, ""
    for mark in IMPOSTOR_PATH_MARKERS:
        if mark in p:
            return True, "billboard-or-impostor"
    stem = name[:-4]
    tokens = set(stem.split("_"))
    hit = LOD_TOKENS & tokens
    if hit:
        return True, f"lod({','.join(sorted(hit))})"
    return False, ""


def classify(path: str, *, include_lod_billboards: bool = False) -> tuple[bool, str]:
    """Return (eligible, reason). `path` is a forward-slash archive path.

    The reason is returned for rejects *and* accepts so a scan report can
    explain every decision instead of silently dropping files.

    include_lod_billboards: default False. When True, SpeedTree billboards and
    mesh LOD colour maps under allowlisted roots may pass (high/experimental).
    mapdata HLOD roots stay out of scope either way.
    """
    p = path.lower().replace("\\", "/")
    name = _basename(p)

    if not name.endswith(".dds"):
        return False, "not-a-dds"

    root = next((r for r in INCLUDE_ROOTS if p.startswith(r)), None)
    if root is None:
        return False, "path-not-in-scope"

    stem = name[:-4]
    tokens = stem.split("_")

    if tokens[0] in ("", "."):
        return False, "unnamed"

    # LOD / billboard / impostor — off by default (distance art, poor ROI).
    is_dist, dist_reason = is_lod_or_billboard(p)
    if is_dist and not include_lod_billboards:
        return False, dist_reason

    if tokens[-1] in NON_COLOR_SUFFIXES:
        return False, f"non-color-map(_{tokens[-1]})"

    if root == "character/texture/" and PLAYER_PREFIX_RE.match(tokens[0]):
        owned = " (BDO-AIO owns this)" if tokens[0] in KNOWN_PLAYER_PREFIXES else ""
        return False, f"playable-class({tokens[0]}){owned}"

    if is_dist and include_lod_billboards:
        return True, f"eligible-lod-billboard:{root.rstrip('/')}"

    return True, f"eligible:{root.rstrip('/')}"


def is_eligible(path: str, *, include_lod_billboards: bool = False) -> bool:
    return classify(path, include_lod_billboards=include_lod_billboards)[0]


# --------------------------------------------------------------------------
# Size gate — applied after the DDS header is read, since pixel dimensions are
# not in the meta index.
# --------------------------------------------------------------------------

def size_verdict(width: int, height: int, min_size: int, target: int,
                 max_out: int = 4096) -> tuple[bool, float, tuple[int, int], str]:
    """Decide whether a texture of this size is worth upscaling.

    Returns (accept, scale, (out_w, out_h), reason). Aspect ratio is preserved
    (changing it would break every UV that samples the texture), so the scale
    comes from the long edge and both edges are snapped to a multiple of 4 for
    block compression.
    """
    long_edge = max(width, height)
    if long_edge <= min_size:
        return False, 1.0, (width, height), f"too-small(<={min_size})"
    if long_edge >= target:
        return False, 1.0, (width, height), f"already->={target}"

    scale = target / long_edge
    out_w = max(4, int(round(width * scale / 4)) * 4)
    out_h = max(4, int(round(height * scale / 4)) * 4)
    if max(out_w, out_h) > max_out:
        return False, 1.0, (width, height), f"output-exceeds-{max_out}"
    return True, scale, (out_w, out_h), "accept"


def flat_name(path: str) -> str:
    """Reversible archive-path -> flat filename, for batch upscaler directories."""
    return path.lower().replace("\\", "/").replace("/", "@")


def unflat_name(flat: str) -> str:
    return flat.replace("@", "/")
