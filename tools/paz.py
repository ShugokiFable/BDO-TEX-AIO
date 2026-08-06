#!/usr/bin/env python3
"""Read-only Black Desert Online PAZ archive reader.

Port of the PAZ layer documented in bdo-data-extractor's FORMATS.md
(ICE cipher + BDO-LZ + pad00000.meta index). Read-only by construction:
nothing here opens a game file for writing.

  pad00000.meta:
    [u32 version][u32 pazCount][pazCount x 12B volume table]
    [u32 fileCount][fileCount x 28B PazFile]
    [u32 folderNamesLen][ICE folder table]
    [u32 fileNamesLen][ICE file table]

  PazFile = hash, folderId, fileId, pazNumber, offset, compSize, origSize (LE u32)

ICE is vectorised over numpy so decrypting multi-MB textures is not a
per-block Python loop.
"""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BDO_ICE_KEY = bytes((0x51, 0xF3, 0x0F, 0x11, 0x04, 0x24, 0x6A, 0x00))

_S_MOD = (
    (333, 313, 505, 369), (379, 375, 319, 391),
    (361, 445, 451, 397), (397, 425, 395, 505),
)
_S_XOR = (
    (0x83, 0x85, 0x9B, 0xCD), (0xCC, 0xA7, 0xAD, 0x41),
    (0x4B, 0x2E, 0xD4, 0x33), (0xEA, 0xCB, 0x2E, 0x04),
)
_P_BOX = (
    0x00000001, 0x00000080, 0x00000400, 0x00002000, 0x00080000, 0x00200000, 0x01000000, 0x40000000,
    0x00000008, 0x00000020, 0x00000100, 0x00004000, 0x00010000, 0x00800000, 0x04000000, 0x20000000,
    0x00000004, 0x00000010, 0x00000200, 0x00008000, 0x00020000, 0x00400000, 0x08000000, 0x10000000,
    0x00000002, 0x00000040, 0x00000800, 0x00001000, 0x00040000, 0x00100000, 0x02000000, 0x80000000,
)
_KEY_ROT = (0, 1, 2, 3, 2, 1, 3, 0, 1, 3, 2, 0, 3, 1, 0, 2)


def _gf_mult(a: int, b: int, m: int) -> int:
    res = 0
    while b:
        if b & 1:
            res ^= a
        a <<= 1
        b >>= 1
        if a >= 256:
            a ^= m
    return res


def _gf_exp7(b: int, m: int) -> int:
    if b == 0:
        return 0
    x = _gf_mult(b, b, m)
    x = _gf_mult(b, x, m)
    x = _gf_mult(x, x, m)
    return _gf_mult(b, x, m)


def _perm32(x: int) -> int:
    res = 0
    for pb in _P_BOX:
        if x & 1:
            res |= pb
        x >>= 1
    return res


def _build_sbox() -> list[np.ndarray]:
    boxes = []
    for q in range(4):
        box = np.zeros(1024, dtype=np.uint32)
        shift = 24 - q * 8
        for i in range(1024):
            col = (i >> 1) & 0xFF
            row = (i & 0x1) | ((i & 0x200) >> 8)
            box[i] = _perm32(_gf_exp7(col ^ _S_XOR[q][row], _S_MOD[q][row]) << shift)
        boxes.append(box)
    return boxes


_SBOX: list[np.ndarray] | None = None


def _sbox() -> list[np.ndarray]:
    global _SBOX
    if _SBOX is None:
        _SBOX = _build_sbox()
    return _SBOX


class ICE:
    """Thin-ICE level 0: 8-byte key, 8 rounds, big-endian 64-bit blocks."""

    ROUNDS = 8

    def __init__(self, key: bytes = BDO_ICE_KEY):
        if len(key) != 8:
            raise ValueError("ICE key must be 8 bytes")
        self.keysched = [[0, 0, 0] for _ in range(self.ROUNDS)]
        kb = [0, 0, 0, 0]
        for i in range(4):
            kb[3 - i] = (key[i * 2] << 8) | key[i * 2 + 1]
        for i in range(8):
            kr = _KEY_ROT[i]
            isk = self.keysched[i]
            isk[0] = isk[1] = isk[2] = 0
            for _ in range(5):
                for j in range(3):
                    cur = isk[j]
                    for k in range(4):
                        idx = (kr + k) & 3
                        bit = kb[idx] & 1
                        cur = ((cur << 1) | bit) & 0xFFFFFFFF
                        kb[idx] = ((kb[idx] >> 1) | ((bit ^ 1) << 15)) & 0xFFFF
                    isk[j] = cur

    @staticmethod
    def _f(p: np.ndarray, sk: list[int], sb: list[np.ndarray]) -> np.ndarray:
        tr = (p & 0x3FF) | ((p << 2) & 0xFFC00)
        tl = ((p >> 16) & 0x3FF) | (((p << 18) | (p >> 14)) & 0xFFC00)
        salt = np.uint32(sk[2]) & (tl ^ tr)
        al = salt ^ tl ^ np.uint32(sk[0])
        ar = salt ^ tr ^ np.uint32(sk[1])
        return (
            sb[0][(al >> 10) & 0x3FF]
            ^ sb[1][al & 0x3FF]
            ^ sb[2][(ar >> 10) & 0x3FF]
            ^ sb[3][ar & 0x3FF]
        )

    def _crypt(self, data: bytes, encrypt: bool) -> bytes:
        nblocks = len(data) // 8
        if nblocks == 0:
            return bytes(data)
        sb = _sbox()
        arr = np.frombuffer(bytes(data[: nblocks * 8]), dtype=">u4").reshape(nblocks, 2)
        left = arr[:, 0].copy()
        right = arr[:, 1].copy()
        ks = self.keysched
        if encrypt:
            for i in range(0, self.ROUNDS, 2):
                left ^= self._f(right, ks[i], sb)
                right ^= self._f(left, ks[i + 1], sb)
        else:
            for i in range(self.ROUNDS - 2, -1, -2):
                left ^= self._f(right, ks[i + 1], sb)
                right ^= self._f(left, ks[i], sb)
        out = np.empty((nblocks, 2), dtype=">u4")
        out[:, 0] = right  # halves are swapped on write-back
        out[:, 1] = left
        return out.tobytes() + bytes(data[nblocks * 8:])

    def decrypt(self, data: bytes) -> bytes:
        return self._crypt(data, False)

    def encrypt(self, data: bytes) -> bytes:
        return self._crypt(data, True)


# --------------------------------------------------------------------------
# BDO-LZ
# --------------------------------------------------------------------------

_LIT_LEN = (4, 0, 1, 0, 2, 0, 1, 0, 3, 0, 1, 0, 2, 0, 1, 0)


def _parse_file_header(data: memoryview) -> tuple[int, int, int]:
    """-> (decompLen, compLen, headerSize)"""
    if data[0] & 0x02:
        comp_len = int.from_bytes(data[1:5], "little")
        decomp_len = int.from_bytes(data[5:9], "little")
        return decomp_len, comp_len, 9
    return data[2], data[1], 3


def _parse_block_header(h: int) -> tuple[int, int, int]:
    """-> (dist, length, stepBytes)"""
    kind = h & 0x03
    if kind == 0x03:
        if (h & 0x7F) == 3:
            return h >> 15, ((h >> 7) & 0xFF) + 3, 4
        return (h >> 7) & 0x1FFFF, ((h >> 2) & 0x1F) + 2, 3
    if kind == 0x02:
        return (h & 0xFFFF) >> 6, ((h >> 2) & 0xF) + 3, 2
    if kind == 0x01:
        return (h & 0xFFFF) >> 2, 3, 2
    return (h & 0xFF) >> 2, 3, 1


def lz_decompress(data: bytes, original_size: int) -> bytes:
    """BDO's custom LZ77 variant. Stops gracefully on a corrupt match."""
    if not data:
        return b""
    mv = memoryview(data)
    flags = mv[0]
    target, comp_len, header_size = _parse_file_header(mv)
    if len(data) < comp_len:
        comp_len = len(data)
    mv = mv[:comp_len]
    n = comp_len

    if not flags & 0x01:  # stored
        end = min(header_size + target, n)
        return bytes(mv[header_size:end])

    out = bytearray(target)
    in_idx, out_idx = header_size, 0
    group = 1
    u32 = int.from_bytes

    while out_idx < target and in_idx < n:
        if group == 1:
            if in_idx + 4 > n:
                break
            group = u32(mv[in_idx:in_idx + 4], "little")
            in_idx += 4
        if group & 1:
            if in_idx + 4 > n:
                break
            dist, length, step = _parse_block_header(u32(mv[in_idx:in_idx + 4], "little"))
            in_idx += step
            if out_idx < dist or out_idx + length > target:
                break  # corrupt match
            src = out_idx - dist
            if dist >= length:  # non-overlapping: slice copy
                out[out_idx:out_idx + length] = out[src:src + length]
            else:
                for k in range(length):
                    out[out_idx + k] = out[src + k]
            out_idx += length
            group >>= 1
        else:
            lit_len = _LIT_LEN[group & 0xF]
            if out_idx + 4 > target or in_idx + 4 > n:
                break
            out[out_idx:out_idx + 4] = mv[in_idx:in_idx + 4]
            out_idx += lit_len
            in_idx += lit_len
            group >>= lit_len

    while out_idx < target:  # tail
        if group == 1:
            if in_idx + 4 <= n:
                in_idx += 4
            group = 0x80000000
        if in_idx >= n:
            break
        out[out_idx] = mv[in_idx]
        out_idx += 1
        in_idx += 1
        group >>= 1

    return bytes(out[:out_idx])


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------

DEFAULT_GAME_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Black Desert Online"


@dataclass(frozen=True, slots=True)
class PazFile:
    hash: int
    folder_id: int
    file_id: int
    paz_number: int
    offset: int
    comp_size: int
    orig_size: int


class Index:
    """Parsed pad00000.meta manifest."""

    def __init__(self, version: int, paz_count: int, files: list[PazFile],
                 folder_names: list[str], file_names: list[str]):
        self.version = version
        self.paz_count = paz_count
        self.files = files
        self.folder_names = folder_names
        self.file_names = file_names

    def path_of(self, f: PazFile) -> str:
        folder = (self.folder_names[f.folder_id]
                  if f.folder_id < len(self.folder_names) else f"<folder:{f.folder_id}>")
        name = (self.file_names[f.file_id]
                if f.file_id < len(self.file_names) else f"<file:{f.file_id}>")
        p = folder.rstrip("/") + "/" + name.lstrip("/")
        return p.replace("//", "/").replace("\\", "/")

    def __len__(self) -> int:
        return len(self.files)


def load_meta(game_dir: str | os.PathLike = DEFAULT_GAME_DIR) -> Index:
    meta_path = Path(game_dir) / "Paz" / "pad00000.meta"
    data = meta_path.read_bytes()

    version, paz_count = struct.unpack_from("<II", data, 0)
    cur = 8 + paz_count * 12  # skip the volume table

    (file_count,) = struct.unpack_from("<I", data, cur)
    cur += 4
    raw = np.frombuffer(data, dtype=np.uint32, count=file_count * 7,
                        offset=cur).reshape(file_count, 7)
    files = [PazFile(*(int(v) for v in row)) for row in raw]
    cur += file_count * 28

    (folder_len,) = struct.unpack_from("<I", data, cur)
    cur += 4
    folder_raw = data[cur:cur + folder_len]
    cur += folder_len

    (file_len,) = struct.unpack_from("<I", data, cur)
    cur += 4
    file_raw = data[cur:cur + file_len]

    ice = ICE()
    folder_names = _parse_folder_table(ice.decrypt(folder_raw))
    file_names = _parse_name_table(ice.decrypt(file_raw))
    return Index(version, paz_count, files, folder_names, file_names)


def _parse_folder_table(raw: bytes) -> list[str]:
    """repeating [8-byte header][NUL-terminated name]"""
    names: list[str] = []
    cur, limit = 0, len(raw) - 8
    while cur < limit:
        cur += 8
        nul = raw.find(b"\0", cur)
        if nul == -1:
            break
        names.append(raw[cur:nul].decode("utf-8", "replace"))
        cur = nul + 1
    return names


def _parse_name_table(raw: bytes) -> list[str]:
    """repeating [NUL-terminated name]"""
    return [n.decode("utf-8", "replace") for n in raw.split(b"\0")[:-1]] \
        if raw.endswith(b"\0") else [n.decode("utf-8", "replace") for n in raw.split(b"\0")]


class Archive:
    """Read-only access to decoded file content across the pad*.paz volumes."""

    def __init__(self, game_dir: str | os.PathLike = DEFAULT_GAME_DIR):
        self.game_dir = Path(game_dir)
        self.paz_dir = self.game_dir / "Paz"
        self._ice = ICE()
        self._handles: dict[int, object] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        for fh in self._handles.values():
            fh.close()
        self._handles.clear()

    def _volume(self, n: int):
        fh = self._handles.get(n)
        if fh is None:
            path = self.paz_dir / f"PAD{n:05d}.PAZ"
            if not path.exists():
                path = self.paz_dir / f"pad{n:05d}.paz"
            fh = open(path, "rb")  # READ-ONLY
            self._handles[n] = fh
        return fh

    def content(self, f: PazFile) -> bytes:
        """Read and fully decode one file (ICE-decrypt + LZ-decompress as needed)."""
        fh = self._volume(f.paz_number)
        fh.seek(f.offset)
        data = fh.read(f.comp_size)
        if len(data) != f.comp_size:
            raise OSError(f"short read in PAD{f.paz_number:05d}.PAZ at {f.offset}")

        if f.comp_size == f.orig_size:  # stored = plaintext
            return data

        needs_decrypt = len(data) % 8 == 0 and data[:4] != b"PABR"
        if needs_decrypt:
            data = self._ice.decrypt(data)

        is_container = (
            len(data) > 9
            and data[0] in (0x6E, 0x6F)
            and int.from_bytes(data[5:9], "little") == f.orig_size
        )
        if is_container:
            return lz_decompress(data, f.orig_size)
        return data[:f.orig_size] if f.orig_size <= len(data) else data


def assert_safe_out(path: str | os.PathLike, game_dir: str | os.PathLike = DEFAULT_GAME_DIR) -> None:
    """Refuse any output path inside the game directory."""
    out = Path(path).resolve()
    game = Path(game_dir).resolve()
    if out == game or game in out.parents:
        raise ValueError(f"refusing to write inside game dir: {out}")


# --------------------------------------------------------------------------
# Index cache (parsing the meta costs ~seconds; the manifest changes only on patch)
# --------------------------------------------------------------------------

def index_signature(game_dir: str | os.PathLike = DEFAULT_GAME_DIR) -> dict:
    st = (Path(game_dir) / "Paz" / "pad00000.meta").stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def load_cached_index(cache_path: Path, game_dir: str | os.PathLike = DEFAULT_GAME_DIR) -> Index:
    """load_meta() with an on-disk cache keyed to pad00000.meta's size+mtime."""
    sig = index_signature(game_dir)
    npz = cache_path.with_suffix(".npz")
    if cache_path.is_file() and npz.is_file():
        try:
            head = json.loads(cache_path.read_text(encoding="utf-8"))
            if head.get("signature") == sig:
                blob = np.load(npz)
                files = [PazFile(*(int(v) for v in row)) for row in blob["files"]]
                return Index(head["version"], head["paz_count"], files,
                             head["folder_names"], head["file_names"])
        except Exception:
            pass  # any cache problem -> reparse

    ix = load_meta(game_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        npz,
        files=np.array([[f.hash, f.folder_id, f.file_id, f.paz_number,
                         f.offset, f.comp_size, f.orig_size] for f in ix.files],
                       dtype=np.uint32),
    )
    cache_path.write_text(json.dumps({
        "signature": sig,
        "version": ix.version,
        "paz_count": ix.paz_count,
        "folder_names": ix.folder_names,
        "file_names": ix.file_names,
    }), encoding="utf-8")
    return ix
