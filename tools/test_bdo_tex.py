#!/usr/bin/env python3
"""Self-checks for the filter rules, the DDS writer and the collision guard.

Run:  python test_bdo_tex.py          (no framework, plain asserts)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bdo_tex  # noqa: E402
import dds  # noqa: E402
import materials as mats  # noqa: E402
import texfilter as tf  # noqa: E402


def test_player_classes_never_eligible() -> None:
    """The core promise: nothing BDO-AIO/Midnight owns can be picked up."""
    for prefix in tf.KNOWN_PLAYER_PREFIXES:
        for name in (f"{prefix}_00_ub_0001.dds", f"{prefix}_10_hel_0009.dds",
                     f"{prefix}_00_nude_0001.dds", f"{prefix}_vw_nrg_0003.dds"):
            path = f"character/texture/{name}"
            ok, reason = tf.classify(path)
            assert not ok, f"{path} must be excluded, got {reason}"
            assert "playable-class" in reason, reason
    # a class prefix invented by a future patch is still excluded
    assert not tf.is_eligible("character/texture/pzzz_00_ub_0001.dds")


def test_npcs_monsters_objects_are_eligible() -> None:
    for path in (
        "character/texture/nhm_adult_poor_0114.dds",
        "character/texture/m0098_fogan_armor_0006.dds",
        "character/texture/m0524_demonlandnem_weapon_0001.dds",
        "object/texture/serendia_sereno_roof_wood_02.dds",
        "object/texture/common_decals_grunge_04_dec.dds",
        "speedtreedata/texture/weed_flower_g.dds",
        "texture/terraindetailmap/rock_01.dds",
    ):
        ok, reason = tf.classify(path)
        assert ok, f"{path} should be eligible, got {reason}"


def test_default_roots_exclude_character() -> None:
    """World pipeline default: character is opt-in, not in DEFAULT_ROOTS."""
    assert tf.CHARACTER_ROOT not in tf.DEFAULT_ROOTS
    assert "object/texture/" in tf.DEFAULT_ROOTS
    assert tf.CHARACTER_ROOT in tf.INCLUDE_ROOTS  # still classifiable when enabled
    # config DEFAULTS must match
    assert tf.CHARACTER_ROOT not in bdo_tex.DEFAULTS["roots"]


def test_material_path_helpers() -> None:
    assert mats.albedo_path_to_material("object/texture/foo.dds", "n") == \
        "object/texture/foo_n.dds"
    assert mats.albedo_path_to_material("object/texture/foo.dds", "p") == \
        "object/texture/foo_p.dds"
    cands = mats.material_candidates("object/texture/bar.dds", "n")
    assert "object/texture/bar_n.dds" in cands
    assert "object/texture/bar_normal.dds" in cands
    kinds = mats.companion_kinds_enabled({})
    assert "n" in kinds and "w" in kinds
    kinds2 = mats.companion_kinds_enabled({"materialsCompanionKinds": ["n", "sp"]})
    assert kinds2 == ["n", "sp"]
    # no invent API — generate_map must not exist
    assert not hasattr(mats, "generate_map")
    flat = bdo_tex._material_flat("object@texture@foo.png", "n")
    assert flat == "object@texture@foo_n.png"


def test_non_color_maps_excluded() -> None:
    for suffix in ("n", "sp", "m", "ao", "dm", "em", "p", "normal"):
        path = f"object/texture/wall_01_{suffix}.dds"
        ok, reason = tf.classify(path)
        assert not ok, f"{path} must be excluded"
        assert "non-color" in reason, reason
    # the colour half of a decal pair stays in
    assert tf.is_eligible("object/texture/wall_01_dec.dds")
    assert not tf.is_eligible("object/texture/wall_01_dec_n.dds")


def test_lod_and_out_of_scope_excluded() -> None:
    assert not tf.is_eligible("object/texture/valencia_city_1_far_lod.dds")
    assert not tf.is_eligible("object/texture/tree_0_0_1_lod.dds")
    # SpeedTree distance billboards — default OFF
    bb = "speedtreedata/texture/tree_bamboo_bamboo_billboards.dds"
    assert not tf.is_eligible(bb)
    assert not tf.is_eligible(
        "speedtreedata/texture/bush_bigleaf_billboards.dds"
    )
    ok, reason = tf.classify(
        "speedtreedata/texture/tree_pine_scotspine_billboards.dds"
    )
    assert not ok and ("billboard" in reason or "lod" in reason), reason
    # High option: billboards can be included when asked
    ok_hi, why_hi = tf.classify(bb, include_lod_billboards=True)
    assert ok_hi and "lod-billboard" in why_hi, why_hi
    # "farm" must NOT match the "far" LOD token
    ok_farm, why_farm = tf.classify("object/texture/worldmap_serendia_farm_01.dds")
    assert ok_farm, f"farm must not be treated as far-LOD, got {why_farm}"
    for path in (
        "ui_texture/icon/00009359.dds",
        "mapdata_real/terraincolortexture/0_0_0.dds",
        "mapdata_real/hloddata/sectorhlod_-1_-1_-1_0_0.dds",
        "texture/lut/grading_01.dds",
        "character/texture_thumbnail/m0098.dds",
        "effect/texture/spark_01.dds",
    ):
        assert not tf.is_eligible(path), f"{path} must be out of scope"


def test_size_gate() -> None:
    # too small / already big enough
    assert not tf.size_verdict(128, 128, 128, 1024)[0]
    assert not tf.size_verdict(64, 64, 128, 1024)[0]
    assert not tf.size_verdict(1024, 1024, 128, 1024)[0]
    assert not tf.size_verdict(2048, 2048, 128, 1024)[0]
    # accepted, aspect preserved, dims multiple of 4
    ok, _s, (w, h), _r = tf.size_verdict(512, 512, 128, 1024)
    assert (ok, w, h) == (True, 1024, 1024)
    ok, _s, (w, h), _r = tf.size_verdict(256, 512, 128, 1024)
    assert (ok, w, h) == (True, 512, 1024), (w, h)
    ok, _s, (w, h), _r = tf.size_verdict(768, 768, 128, 2048)
    assert (ok, w, h) == (True, 2048, 2048)
    for src in (129, 200, 333, 640, 1000):
        ok, _s, (w, h), _r = tf.size_verdict(src, src, 128, 1024)
        assert ok and w % 4 == 0 and h % 4 == 0, (src, w, h)


def test_flat_name_roundtrip() -> None:
    for path in ("character/texture/m0098_fogan_armor_0006.dds",
                 "object/texture/serendia_sereno_roof_wood_02.dds"):
        assert tf.unflat_name(tf.flat_name(path)) == path


def test_internal_path_marker_rules() -> None:
    """Mirrors Meta Injector: leading '_' / '.' directories are organizers."""
    ip = bdo_tex.internal_path
    assert ip(Path("_bdo_tex_upscale/object/texture/a.dds")) == "object/texture/a.dds"
    assert ip(Path("_midnight_xyzw/character/texture/b.dds")) == "character/texture/b.dds"
    assert ip(Path("object/texture/c.dds")) == "object/texture/c.dds"
    assert ip(Path("_layer/_add/object/texture/d.dds")) == "object/texture/d.dds"
    assert ip(Path("_layer/_readme.txt")) is None


def test_collision_guard_detects_an_overlap() -> None:
    """A path already claimed by another layer must be reported, not overwritten."""
    with tempfile.TemporaryDirectory() as tmp:
        ftp = Path(tmp) / "files_to_patch"
        claimed = ftp / "_midnight_xyzw" / "character" / "texture" / "phw_00_nude_0001.dds"
        claimed.parent.mkdir(parents=True)
        claimed.write_bytes(b"x")
        (ftp / "_body_size_limits" / "gamecommondata").mkdir(parents=True)
        (ftp / "_body_size_limits" / "gamecommondata" / "body.xml").write_bytes(b"x")

        owned = bdo_tex.existing_layers(ftp)
        assert owned["character/texture/phw_00_nude_0001.dds"] == "_midnight_xyzw"
        assert owned["gamecommondata/body.xml"] == "_body_size_limits"
        # our own layer is never counted as a competitor against itself
        own = ftp / bdo_tex.STAGE_LAYER / "object" / "texture" / "wall.dds"
        own.parent.mkdir(parents=True)
        own.write_bytes(b"x")
        assert "object/texture/wall.dds" not in bdo_tex.existing_layers(ftp)


def test_refuses_to_write_into_the_game_dir() -> None:
    import paz
    game = paz.DEFAULT_GAME_DIR
    paz.assert_safe_out(Path(tempfile.gettempdir()) / "ok", game)
    for bad in (Path(game) / "Paz", Path(game)):
        try:
            paz.assert_safe_out(bad, game)
        except ValueError:
            continue
        raise AssertionError(f"should have refused {bad}")


def test_blank_rgb_detection() -> None:
    from PIL import Image
    assert bdo_tex.is_blank_rgb(Image.new("RGB", (32, 32), (0, 0, 0)))
    assert not bdo_tex.is_blank_rgb(Image.new("RGB", (32, 32), (1, 0, 0)))
    assert not bdo_tex.is_blank_rgb(Image.new("RGB", (32, 32), (0, 0, 1)))


def test_resize_rejects_blank_and_reattaches_alpha() -> None:
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        blank = tmp / "blank.png"
        good = tmp / "good.png"
        alpha = tmp / "alpha.png"
        out_b = tmp / "out_b.png"
        out_g = tmp / "out_g.png"
        Image.new("RGB", (256, 256), (0, 0, 0)).save(blank)
        Image.new("RGB", (256, 256), (40, 80, 120)).save(good)
        Image.new("L", (64, 64), 180).save(alpha)
        r1 = bdo_tex._resize_one({
            "src": str(blank), "dest": str(out_b), "flat": "blank.png",
            "w": 128, "h": 128, "alpha_src": "",
        })
        r2 = bdo_tex._resize_one({
            "src": str(good), "dest": str(out_g), "flat": "good.png",
            "w": 128, "h": 128, "alpha_src": str(alpha),
        })
        assert r1.startswith("blank:"), r1
        assert not out_b.is_file(), "blank output must not be written"
        assert r2 == "ok", r2
        with Image.open(out_g) as im:
            assert im.size == (128, 128)
            assert im.mode == "RGBA"
            assert im.getchannel("A").getextrema() == (180, 180)


def test_split_rgb_alpha_strips_channel() -> None:
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dest = tmp / "rgb.png"
        alpha_dest = tmp / "a.png"
        rgba = Image.new("RGBA", (16, 16), (10, 20, 30, 200))
        has = bdo_tex._split_rgb_alpha(rgba, dest, alpha_dest)
        assert has is True
        with Image.open(dest) as im:
            assert im.mode == "RGB"
            assert im.getpixel((0, 0)) == (10, 20, 30)
        with Image.open(alpha_dest) as a:
            assert a.mode in ("L", "I") or a.getpixel((0, 0)) in (200, (200,))
        # fully opaque: no sidecar
        dest2 = tmp / "rgb2.png"
        alpha2 = tmp / "a2.png"
        has2 = bdo_tex._split_rgb_alpha(
            Image.new("RGBA", (8, 8), (1, 2, 3, 255)), dest2, alpha2
        )
        assert has2 is False
        assert not alpha2.is_file()


def test_safe_upscayl_defaults() -> None:
    """Claude's measured-safe defaults: tile auto, larger resume batches."""
    assert bdo_tex.DEFAULTS["upscaylTile"] == 0
    assert bdo_tex.DEFAULTS["upscaleBatch"] == 256
    assert bdo_tex.DEFAULTS["upscaylThreads"] == "2:4:2"
    presets = bdo_tex.load_presets()
    assert "fast" in presets
    assert presets["fast"]["model"] == "upscayl-lite-4x"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    dds.demo()
    print(f"\n{len(tests)} filter/staging checks + dds self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
