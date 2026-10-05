"""
Extents Overflow B-tree parsing.

Reference: TN1150, "The Extents Overflow File".

When a file's data or resource fork needs more than the 8 extents
stored directly in its Catalog record, the REMAINING extents are
stored here, keyed by (fileID, forkType, startBlock). Each leaf
record holds up to 8 MORE extents, continuing from wherever the
previous set (Catalog record's 8, or a previous overflow record's 8)
left off.

Key structure (HFSPlusExtentKey):
    UInt16  keyLength      (bytes following this field: 1+1+4+4 = 10)
    UInt8   forkType       (0x00 = data fork, 0xFF = resource fork)
    UInt8   pad
    UInt32  fileID
    UInt32  startBlock     (the LOGICAL start block this record continues from)

Record: HFSPlusExtentRecord = 8 x HFSPlusExtentDescriptor (8 bytes each) = 64 bytes
"""

import struct
from dataclasses import dataclass

DATA_FORK_TYPE = 0x00
RESOURCE_FORK_TYPE = 0xFF


@dataclass
class ExtentsOverflowKey:
    fork_type: int
    file_id: int
    start_block: int  # the logical block number this overflow record continues from

    KEY_TOTAL_LENGTH = 12  # 2 (keyLength field) + 10 (forkType+pad+fileID+startBlock)

    @classmethod
    def parse(cls, raw: bytes) -> "ExtentsOverflowKey":
        (key_length,) = struct.unpack(">H", raw[0:2])
        fork_type, _pad, file_id, start_block = struct.unpack(">BBII", raw[2:12])
        return cls(fork_type=fork_type, file_id=file_id, start_block=start_block)


@dataclass
class SimpleExtent:
    start_block: int
    block_count: int


def parse_extent_record(raw_after_key: bytes) -> list:
    """Parse the 8 extent descriptors following the key. Returns only non-empty ones."""
    extents = []
    offset = 0
    for _ in range(8):
        start_block, block_count = struct.unpack(">II", raw_after_key[offset:offset + 8])
        if not (start_block == 0 and block_count == 0):
            extents.append(SimpleExtent(start_block, block_count))
        offset += 8
    return extents


def parse_extents_leaf_record(raw: bytes):
    """raw = one full leaf record from the Extents Overflow B-tree."""
    key = ExtentsOverflowKey.parse(raw)
    record_data = raw[ExtentsOverflowKey.KEY_TOTAL_LENGTH:]
    extents = parse_extent_record(record_data)
    return key, extents


class ExtentsOverflowTree:
    """
    Wraps a BTree (opened over the Extents Overflow fork) and provides
    lookup of additional extents for a given file/fork beyond what's
    stored directly in the Catalog record.
    """

    def __init__(self, btree):
        self.btree = btree
        self._by_file_fork = None  # lazy: {(file_id, fork_type): [(start_block, [extents]), ...]}

    def _load(self):
        if self._by_file_fork is not None:
            return
        index = {}
        for node_number, descriptor, raw_records in self.btree.iter_leaf_nodes():
            for raw in raw_records:
                key, extents = parse_extents_leaf_record(raw)
                index.setdefault((key.file_id, key.fork_type), []).append(
                    (key.start_block, extents)
                )
        # sort each file/fork's overflow records by their starting logical block,
        # so we can concatenate them in the right order
        for k in index:
            index[k].sort(key=lambda pair: pair[0])
        self._by_file_fork = index

    def get_overflow_extents(self, file_id: int, fork_type: int) -> list:
        """
        Return a flat, ordered list of SimpleExtent for all overflow
        records belonging to this file/fork (empty list if none exist -
        i.e. the file wasn't fragmented beyond its initial 8 extents).
        """
        self._load()
        entries = self._by_file_fork.get((file_id, fork_type), [])
        result = []
        for start_block, extents in entries:
            result.extend(extents)
        return result

    def summary(self) -> str:
        self._load()
        lines = [f"Tracked (file_id, fork_type) pairs with overflow extents: {len(self._by_file_fork)}"]
        for (file_id, fork_type), entries in self._by_file_fork.items():
            fork_name = "data" if fork_type == DATA_FORK_TYPE else "resource"
            total_extents = sum(len(ext) for _, ext in entries)
            lines.append(f"  file_id={file_id} fork={fork_name}: "
                         f"{len(entries)} overflow record(s), {total_extents} extent(s)")
        return "\n".join(lines)
