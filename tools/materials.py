"""Material companion path helpers for BDO textures.

BDO does NOT auto-bind invent maps like OpenMW/Skyrim-with-ParallaxGen.
Meshes/shaders already decide which slots they sample. Evidence from
Suzu / Midnight packs: body mods usually ship albedo only (+ rare _n/_w).
PACs do not expose editable material tables in our tool chain.

So this module only knows how to NAME sibling maps that already exist in
the archive. Inventing Sobel normals / fake parallax is not offered here —
those would inject dead files with no shader effect.
"""
from __future__ import annotations

# Canonical BDO filename suffixes per kind (first match wins when looking up).
KIND_SUFFIXES: dict[str, tuple[str, ...]] = {
    "n": ("_n", "_nm", "_normal"),
    "sp": ("_sp", "_spec", "_s"),
    "m": ("_m", "_mask", "_msk"),
    "p": ("_p", "_height", "_h", "_disp"),
    "w": ("_w",),  # wet / secondary body map (seen on Suzu nudes)
    "ao": ("_ao",),
}

KIND_PRIMARY_SUFFIX: dict[str, str] = {
    "n": "_n",
    "sp": "_sp",
    "m": "_m",
    "p": "_p",
    "w": "_w",
    "ao": "_ao",
}

# Maps we will resize when present next to an upscaled colour texture.
# Order is stable for logs; only paths that exist in the archive are processed.
COMPANION_KINDS: tuple[str, ...] = ("n", "sp", "m", "p", "w", "ao")


def albedo_path_to_material(path: str, kind: str) -> str:
    """object/texture/foo.dds + n -> object/texture/foo_n.dds"""
    p = path.replace("\\", "/")
    if not p.lower().endswith(".dds"):
        raise ValueError(path)
    stem, ext = p[:-4], p[-4:]
    suffix = KIND_PRIMARY_SUFFIX[kind]
    return f"{stem}{suffix}{ext}"


def material_candidates(path: str, kind: str) -> list[str]:
    """All archive path spellings to try for a material sibling."""
    p = path.replace("\\", "/")
    stem, ext = p[:-4], p[-4:]
    return [f"{stem}{suf}{ext}" for suf in KIND_SUFFIXES[kind]]


def companion_kinds_enabled(cfg: dict) -> list[str]:
    """Which companion kinds to match. Default: all known kinds that may exist.

    Config may list materialsCompanionKinds: ["n","sp","m","p","w","ao"].
    """
    raw = cfg.get("materialsCompanionKinds")
    if raw:
        out = []
        for k in raw:
            k = str(k).lower().lstrip("_")
            if k in KIND_PRIMARY_SUFFIX and k not in out:
                out.append(k)
        return out or list(COMPANION_KINDS)
    return list(COMPANION_KINDS)
