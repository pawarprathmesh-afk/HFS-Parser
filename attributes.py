"""
HFS+ Attributes B-tree parsing.

Reference: TN1150, "The Attributes File".

Stores extended attributes (xattrs) keyed by (fileID, attrName).
Three record subtypes exist, distinguished by a recordType UInt32 at
the start of the record data (after the key):

    0x10 = kHFSPlusAttrInlineData  - attribute value stored directly in the record
    0x20 = kHFSPlusAttrForkData    - attribute value stored as its own fork (large values)
    0x30 = kHFSPlusAttrExtents     - continuation extents for a large attribute's fork

Common real-world attributes you'll see on macOS-created volumes:
com.apple.quarantine, com.apple.FinderInfo, com.apple.decmpfs (HFS+
transparent compression metadata), com.apple.ResourceFork (when >0 and
not using the classic resource fork field), ACL-related attributes.
"""

import struct
from dataclasses import dataclass
from enum import IntEnum

from catalog import HFSPlusForkDataMini


class AttrRecordType(IntEnum):
    INLINE_DATA = 0x10
    FORK_DATA = 0x20
    EXTENTS = 0x30


@dataclass
class AttributeKey:
    file_id: int
    start_block: int
    attr_name: str

    @classmethod
    def parse(cls, raw: bytes) -> "AttributeKey":
        (key_length,) = struct.unpack(">H", raw[0:2])
        # bytes 2:4 = pad
        file_id, start_block = struct.unpack(">II", raw[4:12])
        (name_length,) = struct.unpack(">H", raw[12:14])
        name_bytes = raw[14:14 + name_length * 2]
        attr_name = name_bytes.decode("utf-16-be", errors="replace")
        total_length = 2 + key_length
        return cls(file_id=file_id, start_block=start_block, attr_name=attr_name), total_length


@dataclass
class AttributeInlineData:
    data: bytes

    @classmethod
    def parse(cls, raw: bytes) -> "AttributeInlineData":
        record_type, _reserved1, _reserved2, logical_size = struct.unpack(">IIII", raw[0:16])
        assert record_type == AttrRecordType.INLINE_DATA
        data = raw[16:16 + logical_size]
        return cls(data=data)


@dataclass
class AttributeForkData:
    fork: HFSPlusForkDataMini

    @classmethod
    def parse(cls, raw: bytes) -> "AttributeForkData":
        record_type, _reserved = struct.unpack(">II", raw[0:8])
        assert record_type == AttrRecordType.FORK_DATA
        fork = HFSPlusForkDataMini.parse(raw[8:88])
        return cls(fork=fork)


@dataclass
class AttributeExtents:
    extents: list  # list of (start_block, block_count)

    @classmethod
    def parse(cls, raw: bytes) -> "AttributeExtents":
        record_type, _reserved = struct.unpack(">II", raw[0:8])
        assert record_type == AttrRecordType.EXTENTS
        extents = []
        offset = 8
        for _ in range(8):
            start_block, block_count = struct.unpack(">II", raw[offset:offset + 8])
            if not (start_block == 0 and block_count == 0):
                extents.append((start_block, block_count))
            offset += 8
        return cls(extents=extents)


def parse_attribute_leaf_record(raw: bytes):
    """raw = one full leaf record from the Attributes B-tree."""
    key, key_total_length = AttributeKey.parse(raw)
    record_data = raw[key_total_length:]

    (record_type_raw,) = struct.unpack(">I", record_data[0:4])

    if record_type_raw == AttrRecordType.INLINE_DATA:
        record = AttributeInlineData.parse(record_data)
    elif record_type_raw == AttrRecordType.FORK_DATA:
        record = AttributeForkData.parse(record_data)
    elif record_type_raw == AttrRecordType.EXTENTS:
        record = AttributeExtents.parse(record_data)
    else:
        raise ValueError(f"Unknown attribute record type: 0x{record_type_raw:x}")

    return key, record


class AttributesTree:
    def __init__(self, btree):
        self.btree = btree
        self._all_records = None

    def _load_all_records(self):
        if self._all_records is not None:
            return
        all_records = []
        for node_number, descriptor, raw_records in self.btree.iter_leaf_nodes():
            for raw in raw_records:
                key, record = parse_attribute_leaf_record(raw)
                all_records.append((key, record))
        self._all_records = all_records

    def get_attributes_for_file(self, file_id: int):
        """Return [(attr_name, record)] for all attributes belonging to file_id."""
        self._load_all_records()
        return [(key.attr_name, record) for key, record in self._all_records if key.file_id == file_id]

    def all_records_raw(self):
        self._load_all_records()
        return self._all_records
