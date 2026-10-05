"""
Generic HFS+ B-tree node reader.

Reference: TN1150, "B-trees".

Every B-tree (Catalog, Extents Overflow, Attributes) is built from
fixed-size nodes. Node 0 is always the Header Node. Every node --
header, index, leaf, or map -- starts with the same 14-byte
BTNodeDescriptor, and every node's records are located via an
offset array stored backwards from the END of the node.

This module only understands the generic node/record framing.
It does NOT know how to interpret record CONTENTS (e.g. a Catalog
key vs an Extents key) -- that's layered on top, per B-tree type.
"""

import struct
from dataclasses import dataclass
from enum import IntEnum

from fork_reader import ForkReader


class BTNodeKind(IntEnum):
    LEAF = 0xFF      # -1 as signed byte
    INDEX = 0x00
    HEADER = 0x01
    MAP = 0x02


def _signed_byte_to_kind(b: int) -> BTNodeKind:
    # kind is stored as a signed 8-bit value: -1 (leaf), 0 (index), 1 (header), 2 (map)
    signed = b - 256 if b > 127 else b
    mapping = {-1: BTNodeKind.LEAF, 0: BTNodeKind.INDEX, 1: BTNodeKind.HEADER, 2: BTNodeKind.MAP}
    if signed not in mapping:
        raise ValueError(f"Unknown BTree node kind byte: {b}")
    return mapping[signed]


@dataclass
class BTNodeDescriptor:
    f_link: int          # forward link (next node of same kind), 0 = none
    b_link: int           # backward link
    kind: BTNodeKind
    height: int
    num_records: int

    SIZE = 14

    @classmethod
    def parse(cls, data: bytes) -> "BTNodeDescriptor":
        f_link, b_link, kind_byte, height, num_records, _reserved = \
            struct.unpack(">IIBBHH", data[0:14])
        return cls(f_link, b_link, _signed_byte_to_kind(kind_byte), height, num_records)


@dataclass
class BTHeaderRecord:
    tree_depth: int
    root_node: int
    leaf_records: int
    first_leaf_node: int
    last_leaf_node: int
    node_size: int
    max_key_length: int
    total_nodes: int
    free_nodes: int
    clump_size: int
    btree_type: int
    key_compare_type: int
    attributes: int

    SIZE = 106  # fixed portion right after the node descriptor, within node 0

    FIXED_FIELDS_SIZE = 42  # everything up to and including `attributes`; reserved3[16] (64 bytes) follows

    @classmethod
    def parse(cls, data: bytes) -> "BTHeaderRecord":
        (
            tree_depth, root_node, leaf_records, first_leaf_node, last_leaf_node,
            node_size, max_key_length, total_nodes, free_nodes,
            _reserved1, clump_size, btree_type, key_compare_type, attributes
        ) = struct.unpack(">HIIII HHII HIBBI", data[0:cls.FIXED_FIELDS_SIZE])
        return cls(
            tree_depth, root_node, leaf_records, first_leaf_node, last_leaf_node,
            node_size, max_key_length, total_nodes, free_nodes,
            clump_size, btree_type, key_compare_type, attributes,
        )

    # Actual TN1150 constants (NOT 0/1 - easy mistake, learned the hard way)
    KHFS_BINARY_COMPARE = 0xBC   # 188 - case-sensitive
    KHFS_CASE_FOLDING = 0xCF     # 207 - case-insensitive (HFS+ default)

    @property
    def is_case_sensitive(self) -> bool:
        return self.key_compare_type == self.KHFS_BINARY_COMPARE


class BTreeError(ValueError):
    pass


class BTree:
    """
    Generic reader: given a ForkReader over a B-tree's fork (Catalog,
    Extents Overflow, or Attributes), reads the header and lets you
    fetch any node by number as (descriptor, list_of_raw_record_bytes).
    """

    def __init__(self, fork_reader: ForkReader):
        self.fork_reader = fork_reader
        self.node_size = None
        self.header = None
        self._load_header()

    def _load_header(self):
        # We don't know node_size yet, so first read a conservative chunk
        # (512 bytes is always enough to cover descriptor + header record)
        probe = self.fork_reader.read(0, 512)
        descriptor = BTNodeDescriptor.parse(probe[0:14])
        if descriptor.kind != BTNodeKind.HEADER:
            raise ValueError(f"Node 0 is not a header node (got {descriptor.kind})")

        header_record = BTHeaderRecord.parse(probe[14:14 + BTHeaderRecord.SIZE])
        ns = header_record.node_size
        if ns < 512 or ns > 65536 or ns & (ns - 1):
            raise BTreeError(f"implausible B-tree node size {ns}")
        self.header = header_record
        self.node_size = header_record.node_size

    def read_node_raw(self, node_number: int) -> bytes:
        offset = node_number * self.node_size
        return self.fork_reader.read(offset, self.node_size)

    def read_node(self, node_number: int):
        """
        Returns (BTNodeDescriptor, [raw_record_bytes, ...])
        Record boundaries come from the offset array at the end of the node:
        it holds (num_records + 1) 16-bit big-endian offsets, read backwards
        from the very end of the node. The last one marks the "free space"
        boundary, so record i spans offsets[i] .. offsets[i+1].
        """
        if node_number < 0 or node_number >= self.header.total_nodes:
            raise BTreeError(f"node {node_number} outside tree (0..{self.header.total_nodes - 1})")
        raw = self.read_node_raw(node_number)
        descriptor = BTNodeDescriptor.parse(raw[0:14])

        num_offsets = descriptor.num_records + 1
        if 14 + 2 * num_offsets > self.node_size:
            raise BTreeError(f"node {node_number}: record count {descriptor.num_records} cannot fit")
        offsets = []
        for i in range(num_offsets):
            pos = self.node_size - 2 * (i + 1)
            (val,) = struct.unpack(">H", raw[pos:pos + 2])
            offsets.append(val)
        # NOTE: these come out already in ascending order (offsets[0] = start of
        # record 0, nearest the node's tail; offsets[-1] = free-space marker,
        # furthest from the tail). Verified against a real HFS+ volume: offset[0]
        # pointed exactly at byte 14 (right after the 14-byte node descriptor),
        # containing a real CatalogKey. No reversal needed - a reversal here was
        # an earlier bug based on a wrong assumption about array direction.

        records = []
        limit = self.node_size - 2 * num_offsets
        for i in range(descriptor.num_records):
            start = offsets[i]
            end = offsets[i + 1]
            if not (14 <= start <= end <= limit):
                raise BTreeError(f"node {node_number}: bad record offsets {start}..{end}")
            records.append(raw[start:end])

        return descriptor, records

    def iter_leaf_nodes(self):
        """Walk the leaf node chain from first_leaf_node to last, via fLink."""
        node_number = self.header.first_leaf_node
        seen = set()
        # first_leaf_node can be 0 if the tree is empty
        while node_number != 0:
            if node_number in seen:
                raise BTreeError(f"leaf chain loops back to node {node_number} (corrupt B-tree)")
            seen.add(node_number)
            descriptor, records = self.read_node(node_number)
            yield node_number, descriptor, records
            node_number = descriptor.f_link

    def summary(self) -> str:
        h = self.header
        return (
            f"Tree depth:       {h.tree_depth}\n"
            f"Root node:        {h.root_node}\n"
            f"Leaf records:     {h.leaf_records}\n"
            f"First leaf node:  {h.first_leaf_node}\n"
            f"Last leaf node:   {h.last_leaf_node}\n"
            f"Node size:        {h.node_size}\n"
            f"Total nodes:      {h.total_nodes}\n"
            f"Free nodes:       {h.free_nodes}\n"
            f"Key compare type: {h.key_compare_type} "
            f"({'binary/case-sensitive' if h.is_case_sensitive else 'case-folding/case-insensitive'})\n"
        )
