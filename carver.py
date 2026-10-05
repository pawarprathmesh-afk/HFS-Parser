"""
Free-space carving fallback for HFS+.

Used when no deleted Catalog record can be found (non-journaled volume
whose catalog nodes were scrubbed). With no metadata, we only know
*where* unallocated data sits, not its name or exact size. We:

  1. Walk each free run from the Allocation File bitmap.
  2. Within it, find file signatures (JPEG, PNG, PDF, GIF, ZIP...) and cut
     at the format's footer where one exists.
  3. Everything else that is non-zero is emitted as a "raw" segment (this
     is how plain text files are recovered), with trailing zero padding
     trimmed.
"""

import struct
from dataclasses import dataclass
from typing import List

from allocation_file import AllocationFile

def _riff_size(d, p):   # RIFF/WAV: little-endian chunk size + 8
    return struct.unpack("<I", d[p + 4:p + 8])[0] + 8 if d[p + 8:p + 12] in (b"WAVE", b"AVI ", b"WEBP") else None


def _aiff_size(d, p):   # FORM/AIFF: big-endian chunk size + 8
    return struct.unpack(">I", d[p + 4:p + 8])[0] + 8 if d[p + 8:p + 12] in (b"AIFF", b"AIFC") else None


def _bmp_size(d, p):    # BMP: file size at offset 2, reserved fields must be zero
    return struct.unpack("<I", d[p + 2:p + 6])[0] if d[p + 6:p + 10] == b"\0\0\0\0" else None


def _sqlite_size(d, p):  # SQLite: page size (offset 16) * page count (offset 28)
    page = struct.unpack(">H", d[p + 16:p + 18])[0]
    page = 65536 if page == 1 else page
    return page * struct.unpack(">I", d[p + 28:p + 32])[0] or None


# (name, extension, header, footer or None, footer_extra_bytes, size_fn or None)
# size_fn (length encoded in the file header) is preferred over a footer search,
# because footers like GIF's 00 3B can also occur inside the image data.
SIGNATURES = [
    ("jpeg", "jpg", b"\xff\xd8\xff", b"\xff\xd9", 0, None),
    ("png", "png", b"\x89PNG\r\n\x1a\n", b"IEND", 4, None),   # + 4-byte CRC after the type
    ("pdf", "pdf", b"%PDF-", b"%%EOF", 1, None),
    ("zip", "zip", b"PK\x03\x04", b"PK\x05\x06", 18, None),
    ("riff", "wav", b"RIFF", None, 0, _riff_size),
    ("aiff", "aiff", b"FORM", None, 0, _aiff_size),
    ("bmp", "bmp", b"BM", None, 0, _bmp_size),
    ("sqlite", "sqlite", b"SQLite format 3\x00", None, 0, _sqlite_size),
    ("gif", "gif", b"GIF8", None, 0, None),  # no reliable footer: handled as raw, trailing zeros trimmed
]


@dataclass
class CarvedFile:
    start_block: int
    block_count: int
    kind: str          # signature name or "raw"
    extension: str
    data: bytes
    exact_end: bool    # True if a footer determined the size


def _is_file_tail(block: bytes) -> bool:
    """A block that has data followed by >=16 zero bytes is the last block of a file
    (the filesystem zero-pads the slack of a file's final block)."""
    return len(block) >= 16 and block[-16:] == b"\x00" * 16 and bool(block.strip(b"\x00"))


def _carve_run(data: bytes, base_block: int, block_size: int) -> List[CarvedFile]:
    out = []
    pos = 0
    n = len(data)
    while pos < n:
        # skip all-zero blocks
        blk = pos // block_size
        chunk_end = min(n, (blk + 1) * block_size)
        if not data[pos:chunk_end].strip(b"\x00"):
            pos = chunk_end
            continue

        sig = next((s for s in SIGNATURES if data.startswith(s[2], pos)), None)
        if sig:
            name, ext, header, footer, extra, size_fn = sig
            end = None
            if size_fn:
                try:
                    length = size_fn(data, pos)
                except struct.error:
                    length = None
                if length and pos + length <= n:
                    end = pos + length
            elif footer:
                idx = data.find(footer, pos + len(header))
                if idx != -1:
                    end = min(n, idx + len(footer) + extra)
            if end is not None:
                out.append(CarvedFile(base_block + pos // block_size,
                                      (end - pos + block_size - 1) // block_size,
                                      name, ext, data[pos:end], True))
                pos = ((end + block_size - 1) // block_size) * block_size
                continue

        # raw: extend through consecutive non-zero blocks, stop at a signature block
        end = chunk_end
        while end < n and not _is_file_tail(data[end - block_size:end]):
            nxt = min(n, end + block_size)
            seg = data[end:nxt]
            if not seg.strip(b"\x00") or any(len(s[2]) >= 4 and seg.startswith(s[2]) for s in SIGNATURES):
                break
            end = nxt
        payload = data[pos:end].rstrip(b"\x00")
        out.append(CarvedFile(base_block + pos // block_size,
                              (end - pos + block_size - 1) // block_size,
                              "raw", "bin", payload, False))
        pos = end
    return out


def carve_free_space(image_path: str, block_size: int, allocation_file: AllocationFile,
                     partition_offset: int = 0, chunk_bytes: int = 64 * 1024 * 1024) -> List[CarvedFile]:
    """Carve every free run. Large runs are read in chunks to bound memory; a file
    that straddles a chunk boundary is stitched back together."""
    chunk_bytes -= chunk_bytes % block_size
    carved: List[CarvedFile] = []
    with open(image_path, "rb") as f:
        for start, count in allocation_file.free_ranges():
            run_bytes = count * block_size
            prev_end_block = None   # block just after the last chunk, if it ended mid-file
            for off in range(0, run_bytes, chunk_bytes):
                f.seek(partition_offset + start * block_size + off)
                data = f.read(min(chunk_bytes, run_bytes - off))
                base = start + off // block_size
                pieces = _carve_run(data, base, block_size)
                if (pieces and carved and prev_end_block == base
                        and pieces[0].start_block == base and carved[-1].kind == "raw"
                        and len(carved[-1].data) == carved[-1].block_count * block_size):
                    head = pieces.pop(0)
                    last = carved[-1]
                    last.data += head.data
                    last.block_count += head.block_count
                prev_end_block = None
                carved.extend(pieces)
                if carved and carved[-1].kind == "raw" and \
                        carved[-1].start_block + carved[-1].block_count == base + len(data) // block_size:
                    prev_end_block = base + len(data) // block_size
    return carved
