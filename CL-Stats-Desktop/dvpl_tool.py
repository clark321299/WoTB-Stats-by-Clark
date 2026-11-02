"""
DVPL — decode / encode WoT Blitz DVPL files.
Footer: orig_size, comp_size, crc32, comp_type (0=none, 2=LZ4), magic DVPL.
"""

from __future__ import annotations

import struct
import zlib

MAGIC = b"DVPL"
FOOTER_SIZE = 20


def _lz4():
    try:
        import lz4.block as blk

        return blk
    except ImportError as e:
        raise ImportError("lz4 is required to decode/encode DVPL files (pip install lz4)") from e


def compression_type_from_bytes(data: bytes) -> int | None:
    if len(data) < FOOTER_SIZE or data[-4:] != MAGIC:
        return None
    return struct.unpack_from("<IIII", data[-FOOTER_SIZE:], 0)[3]


def decode_bytes(data: bytes) -> bytes:
    if len(data) < FOOTER_SIZE or data[-4:] != MAGIC:
        raise ValueError("Not a DVPL file")
    footer = data[-FOOTER_SIZE:]
    orig_size, comp_size, crc32, comp_type = struct.unpack_from("<IIII", footer, 0)
    compressed = data[:-FOOTER_SIZE]
    if len(compressed) != comp_size:
        raise ValueError(f"Size mismatch: expected {comp_size}, got {len(compressed)}")
    actual_crc = zlib.crc32(compressed) & 0xFFFFFFFF
    if actual_crc != crc32:
        raise ValueError(f"CRC mismatch: expected {crc32:#010x}, got {actual_crc:#010x}")
    if comp_type == 0:
        out = compressed
    elif comp_type == 2:
        out = _lz4().decompress(compressed, uncompressed_size=orig_size)
    else:
        raise ValueError(f"Unknown compression type: {comp_type}")
    if len(out) != orig_size:
        raise ValueError(f"Decompressed size mismatch: expected {orig_size}, got {len(out)}")
    return out


def decode(path_in: str, path_out: str | None = None) -> bytes:
    with open(path_in, "rb") as f:
        data = f.read()
    result = decode_bytes(data)
    if path_out:
        with open(path_out, "wb") as f:
            f.write(result)
    return result


def encode_bytes(data: bytes, comp_type: int = 2) -> bytes:
    if comp_type == 2:
        compressed = _lz4().compress(data, store_size=False)
    elif comp_type == 0:
        compressed = data
    else:
        raise ValueError(f"Unknown comp_type: {comp_type}")
    crc32 = zlib.crc32(compressed) & 0xFFFFFFFF
    footer = struct.pack("<IIII", len(data), len(compressed), crc32, comp_type) + MAGIC
    return compressed + footer
