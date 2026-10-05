"""
HFS+ journal reader + raw catalog-record scanner.

Reference: TN1150 "Journaling" / xnu bsd/hfs/hfs_journal.h.

Why this exists: when a file is deleted, the live Catalog B-tree record
is removed (and macOS scrubs the leftover bytes in that node), but on a
JOURNALED volume the transaction that did the deletion - and earlier
transactions that wrote the same catalog node - are still sitting in
the journal buffer. Those old node images contain the file's full
Catalog record (name, parent, file_id, dates, data-fork extents).

Rather than replaying the journal transaction by transaction (which
depends on endianness and on which transactions are still valid), we
scan the raw journal buffer for catalog FILE records. Stale/overwritten
transactions are exactly what we want, so this is deliberately lenient.
The same scanner is reused over the Catalog fork itself to catch
records left in node slack space.
"""

import re
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

from catalog import CatalogKey, CatalogFileRecord

JOURNAL_MAGIC = 0x4A4E4C78      # 'JNLx'
JOURNAL_ENDIAN = 0x12345678
JI_IN_FS = 0x1                  # kJIJournalInFSMask
JI_ON_OTHER_DEVICE = 0x2
JI_NEED_INIT = 0x4


@dataclass
class JournalInfo:
    flags: int
    offset: int       # byte offset of the journal header, relative to the volume start
    size: int         # total journal size in bytes (header + buffer)

    @property
    def in_volume(self) -> bool:
        return bool(self.flags & JI_IN_FS) and not (self.flags & JI_ON_OTHER_DEVICE)


def read_journal_info(image_path: str, vh, partition_offset: int = 0) -> Optional[JournalInfo]:
    """Returns None if the volume is not journaled or the info block is unusable."""
    if not vh.is_journaled or vh.journal_info_block == 0:
        return None
    with open(image_path, "rb") as f:
        f.seek(partition_offset + vh.journal_info_block * vh.block_size)
        raw = f.read(52)
    if len(raw) < 52:
        return None
    flags = struct.unpack(">I", raw[0:4])[0]
    # 32 bytes device_signature follow, then offset(8) and size(8)
    offset, size = struct.unpack(">QQ", raw[36:52])
    if size == 0:
        return None
    return JournalInfo(flags=flags, offset=offset, size=size)


def read_journal_bytes(image_path: str, info: JournalInfo, partition_offset: int = 0) -> bytes:
    with open(image_path, "rb") as f:
        f.seek(partition_offset + info.offset)
        return f.read(info.size)


def journal_header_ok(buf: bytes) -> bool:
    """True if the journal header carries the JNLx magic in either byte order."""
    if len(buf) < 8:
        return False
    magic_be, endian_be = struct.unpack(">II", buf[0:8])
    magic_le, endian_le = struct.unpack("<II", buf[0:8])
    return (magic_be == JOURNAL_MAGIC and endian_be == JOURNAL_ENDIAN) or \
           (magic_le == JOURNAL_MAGIC and endian_le == JOURNAL_ENDIAN)


# --- raw catalog file-record scanner -------------------------------------

# File record starts: recordType=2 (int16), flags (u16), reserved1 == 0 (u32)
_FILE_REC_RE = re.compile(rb"\x00\x02..\x00\x00\x00\x00", re.DOTALL)
_MAX_NAME_UNITS = 255
_HFS_EPOCH_MAX = 0xF0000000  # sanity bound on HFS timestamps


def _plausible_name(name: str) -> bool:
    return bool(name) and all(c.isprintable() for c in name) and "/" not in name


def scan_for_file_records(buf: bytes, total_blocks: int,
                          min_file_id: int = 16) -> List[Tuple[int, CatalogKey, CatalogFileRecord]]:
    """
    Scan arbitrary bytes for intact Catalog file records. Returns
    (offset_in_buf, key, record) for each plausible hit.

    A hit must have: a key whose length is consistent with its name length,
    a file record directly after it, file_id >= 16, sane dates, and data-fork
    extents that lie inside the volume and are consistent with total_blocks.
    """
    hits = []
    for m in _FILE_REC_RE.finditer(buf):
        rec_start = m.start()
        if rec_start + CatalogFileRecord.SIZE > len(buf):
            continue
        # The key sits immediately before the record: find n such that
        # key_length == 6 + 2*n and name_length == n
        found = None
        for n in range(1, _MAX_NAME_UNITS + 1):
            key_len = 6 + 2 * n
            key_start = rec_start - (2 + key_len)
            if key_start < 0:
                break
            kl, parent_id, name_len = struct.unpack(">HIH", buf[key_start:key_start + 8])
            if kl == key_len and name_len == n and parent_id >= 1:
                found = (key_start, parent_id, n)
                break
        if not found:
            continue
        key_start, parent_id, n = found
        try:
            key = CatalogKey.parse(buf[key_start:rec_start])
            record = CatalogFileRecord.parse(buf[rec_start:rec_start + CatalogFileRecord.SIZE])
        except Exception:
            continue
        if not _plausible_name(key.node_name) or record.file_id < min_file_id:
            continue
        if not (0 < record.create_date < _HFS_EPOCH_MAX):
            continue
        df = record.data_fork
        exts = df.active_extents()
        covered = sum(e.block_count for e in exts)
        if covered > df.total_blocks:
            continue
        if any(e.start_block + e.block_count > total_blocks for e in exts):
            continue
        if df.logical_size > df.total_blocks * 0x10000000:  # absurd
            continue
        hits.append((key_start, key, record))
    return hits


# --- raw scan for com.apple.decmpfs attribute records ------------------------

_DECMPFS_NAME = "com.apple.decmpfs".encode("utf-16-be")


def scan_for_decmpfs_attributes(buf: bytes):
    """
    Find inline com.apple.decmpfs attribute records in raw bytes (journal or Attributes-file
    slack). Small compressed files keep their whole content in this attribute, so it is needed
    to recover a deleted compressed file. Returns {file_id: header_and_payload_bytes}; when a
    file turns up more than once the longest payload wins.

    Key layout: keyLength(2) pad(2) fileID(4) startBlock(4) nameLength(2) name(UTF-16BE);
    record: type=0x10 (4) reserved(8) logicalSize(4) data.
    """
    found = {}
    name_units = len(_DECMPFS_NAME) // 2
    key_length = 12 + 2 * name_units
    pos = 0
    while True:
        i = buf.find(_DECMPFS_NAME, pos)
        if i == -1:
            break
        pos = i + 1
        key_start = i - 14
        if key_start < 0:
            continue
        kl, _pad, file_id, start_block, nlen = struct.unpack(">HHIIH", buf[key_start:key_start + 14])
        if kl != key_length or nlen != name_units or start_block != 0 or file_id < 16:
            continue
        rec = key_start + 2 + kl
        rtype, _r1, _r2, size = struct.unpack(">IIII", buf[rec:rec + 16])
        if rtype != 0x10 or size < 16 or size > 3802 or rec + 16 + size > len(buf):
            continue
        value = buf[rec + 16:rec + 16 + size]
        if value[:4] != b"fpmc":
            continue
        if file_id not in found or len(value) > len(found[file_id]):
            found[file_id] = value
    return found


# --- journal replay -------------------------------------------------------------

@dataclass
class JournalHeader:
    start: int          # offset (from the journal header) of the oldest unapplied transaction
    end: int            # offset one past the newest transaction
    size: int
    blhdr_size: int
    jhdr_size: int      # device block size: block numbers in the journal count in these units
    byte_order: str     # "<" or ">"

    @property
    def dirty(self) -> bool:
        return self.start != self.end


def parse_journal_header(buf: bytes) -> Optional[JournalHeader]:
    for order in ("<", ">"):
        if len(buf) < 44:
            return None
        magic, endian, start, end, size, blhdr, _cks, jhdr = struct.unpack(order + "IIQQQIII", buf[:44])
        if magic == JOURNAL_MAGIC and endian == JOURNAL_ENDIAN:
            return JournalHeader(start, end, size, blhdr, jhdr, order)
    return None


def pending_blocks(buf: bytes) -> Tuple[List[Tuple[int, bytes]], Optional[str]]:
    """
    Blocks from transactions that were committed to the journal but not yet written to their
    final place (start != end). Returns ([(byte_offset_in_volume, data), ...] in commit order,
    problem or None). Later entries must win over earlier ones when applied.

    Layout: at `start` a block list header (u16 max_blocks, u16 num_blocks, u32 bytes_used,
    u32 checksum, u32 pad) followed by num_blocks 16-byte block_info records
    (u64 block number, u32 size, u32 checksum/sequence). binfo[0] describes the header itself.
    The data for binfo[1:] follows at start + blhdr_size, back to back. The next header starts
    bytes_used after this one. Offsets wrap from `size` back to `jhdr_size`.
    """
    h = parse_journal_header(buf)
    if h is None:
        return [], "journal header not recognised"
    if not h.dirty:
        return [], None
    o = h.byte_order

    def wrap(p):
        return p - h.size + h.jhdr_size if p >= h.size else p

    def read(p, n):
        """n bytes at journal offset p, following the circular buffer's wrap-around."""
        p = wrap(p)
        if p + n <= h.size:
            return buf[p:p + n]
        first = h.size - p
        return buf[p:h.size] + buf[h.jhdr_size:h.jhdr_size + n - first]

    blocks = []
    pos = h.start
    guard = 0
    while pos != h.end:
        guard += 1
        if guard > 100000:
            return blocks, "journal transaction chain did not terminate"
        max_blocks, num_blocks, bytes_used, _chk, _pad = struct.unpack(o + "HHIII", read(pos, 16))
        if not (1 <= num_blocks <= max_blocks) or bytes_used < h.blhdr_size or bytes_used > h.size:
            return blocks, f"invalid transaction header at journal offset {pos}"
        binfo = [struct.unpack(o + "QII", read(pos + 16 + 16 * i, 16)) for i in range(num_blocks)]
        data_pos = pos + h.blhdr_size
        for bnum, bsize, _x in binfo[1:]:
            if bsize == 0 or bsize % 512 or bsize > h.size:
                return blocks, f"invalid block size {bsize} in transaction at {pos}"
            blocks.append((bnum * h.jhdr_size, read(data_pos, bsize)))
            data_pos += bsize
        pos = wrap(pos + bytes_used)
    return blocks, None
