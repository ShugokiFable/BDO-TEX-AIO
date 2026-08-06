#!/usr/bin/env python3
"""Minimal DDS reader/writer for BDO's texture formats (DXT1 / DXT5).

BDO ships every real texture as DXT1 or DXT5 with a full mip chain (measured:
9-11 levels). Pillow can decode both and encode both, but it only ever writes
mip 0 — a DDS with no mips makes distant surfaces shimmer badly. So the mip
chain is built here: each level is encoded through Pillow and the 128-byte
header it emits is stripped, leaving raw block data to concatenate behind one
patched header.
"""
from __future__ import annotations

import io
import struct

from PIL import Image

HEADER_SIZE = 128

DDSD_CAPS = 0x1
DDSD_HEIGHT = 0x2
DDSD_WIDTH = 0x4
DDSD_PIXELFORMAT = 0x1000
DDSD_MIPMAPCOUNT = 0x20000
DDSD_LINEARSIZE = 0x80000

DDSCAPS_COMPLEX = 0x8
DDSCAPS_MIPMAP = 0x400000
DDSCAPS_TEXTURE = 0x1000

BLOCK_BYTES = {"DXT1": 8, "DXT5": 16, "DXT3": 16}

# BDO uses DXT3 on a handful of effect textures; Pillow writes BC2 under the
# name "DXT3" only in newer builds, so it is mapped to DXT5 which is a strict
# superset for our purposes (interpolated alpha instead of explicit).
PILLOW_PIXEL_FORMAT = {"DXT1": "DXT1", "DXT5": "DXT5", "DXT3": "DXT5"}


class DDSError(ValueError):
    pass


def read_header(data: bytes) -> dict:
    """Parse the fields we care about out of a DDS header."""
    if len(data) < HEADER_SIZE or data[:4] != b"DDS ":
        raise DDSError("not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    (mip_count,) = struct.unpack_from("<I", data, 28)
    fourcc = data[84:88].decode("ascii", "replace")
    (bpp,) = struct.unpack_from("<I", data, 88)
    if not (0 < width <= 16384 and 0 < height <= 16384):
        raise DDSError(f"bad DDS dimensions {width}x{height}")
    return {
        "width": width,
        "height": height,
        "mip_count": mip_count,
        "fourcc": fourcc,
        "bpp": bpp,
        "compressed": fourcc in BLOCK_BYTES,
    }


def to_image(data: bytes) -> Image.Image:
    """Decode mip 0 to RGBA."""
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.convert("RGBA")


def _encode_level(im: Image.Image, pixel_format: str) -> bytes:
    """Encode one mip level and return only its block data."""
    buf = io.BytesIO()
    im.save(buf, "DDS", pixel_format=pixel_format)
    return buf.getvalue()[HEADER_SIZE:]


def _level_sizes(width: int, height: int, fourcc: str) -> int:
    bb = BLOCK_BYTES[fourcc]
    return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * bb


def mip_dimensions(width: int, height: int) -> list[tuple[int, int]]:
    """Full chain down to 1x1, the same convention the game's own files use."""
    levels = [(width, height)]
    w, h = width, height
    while w > 1 or h > 1:
        w = max(1, w // 2)
        h = max(1, h // 2)
        levels.append((w, h))
    return levels


def write(im: Image.Image, fourcc: str, mipmaps: bool = True) -> bytes:
    """Encode an RGBA image to a complete DDS with a full mip chain."""
    fourcc = fourcc.upper()
    if fourcc not in BLOCK_BYTES:
        raise DDSError(f"unsupported output format {fourcc!r}")
    pixel_format = PILLOW_PIXEL_FORMAT[fourcc]
    out_fourcc = pixel_format  # what the bytes actually are

    if im.mode != "RGBA":
        im = im.convert("RGBA")
    width, height = im.size

    levels = mip_dimensions(width, height) if mipmaps else [(width, height)]
    payload = bytearray()
    for i, (w, h) in enumerate(levels):
        level = im if i == 0 else im.resize((w, h), Image.LANCZOS)
        block = _encode_level(level, pixel_format)
        expected = _level_sizes(w, h, out_fourcc)
        if len(block) < expected:
            raise DDSError(f"encoder returned {len(block)}B for {w}x{h}, expected {expected}B")
        payload += block[:expected]

    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    caps = DDSCAPS_TEXTURE
    if len(levels) > 1:
        flags |= DDSD_MIPMAPCOUNT
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP

    header = bytearray(HEADER_SIZE)
    header[0:4] = b"DDS "
    struct.pack_into(
        "<IIIIII", header, 4,
        124,                                     # dwSize
        flags,
        height,
        width,
        _level_sizes(width, height, out_fourcc),  # dwPitchOrLinearSize
        0,                                        # dwDepth
    )
    struct.pack_into("<I", header, 28, len(levels))          # dwMipMapCount
    struct.pack_into("<II", header, 76, 32, 0x4)             # ddspf.dwSize, DDPF_FOURCC
    header[84:88] = out_fourcc.encode("ascii")
    struct.pack_into("<I", header, 108, caps)                # dwCaps
    return bytes(header) + bytes(payload)


def demo() -> None:
    """Self-check: round-trip, mip chain length, and header agreement."""
    for fourcc, size in (("DXT1", (256, 256)), ("DXT5", (128, 64)), ("DXT1", (64, 256))):
        src = Image.new("RGBA", size)
        src.putdata([
            ((x * 3) % 256, (y * 5) % 256, (x ^ y) % 256, 255 if (x + y) % 7 else 0)
            for y in range(size[1]) for x in range(size[0])
        ])
        blob = write(src, fourcc)
        h = read_header(blob)
        assert (h["width"], h["height"]) == size, (h, size)
        assert h["fourcc"] == fourcc, h
        levels = mip_dimensions(*size)
        assert h["mip_count"] == len(levels), (h["mip_count"], len(levels))

        expected = HEADER_SIZE + sum(_level_sizes(w, hh, fourcc) for w, hh in levels)
        assert len(blob) == expected, f"{fourcc} {size}: {len(blob)} != {expected}"

        back = to_image(blob)
        assert back.size == size, back.size
        print(f"  ok {fourcc} {size[0]}x{size[1]}  {len(levels)} mips  {len(blob)}B")

    # a 1x1 source must not produce a zero-length chain
    assert read_header(write(Image.new("RGBA", (1, 1)), "DXT1"))["mip_count"] == 1
    print("  ok 1x1 edge case")
    print("dds.py self-check passed")


if __name__ == "__main__":
    demo()
