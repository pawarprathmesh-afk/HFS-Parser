"""
Diagnostic tool: dumps, for every leaf node in the Catalog B-tree,
each record's raw length and first bytes in hex. This deliberately
does NOT try to interpret record contents (no CatalogKey.parse call),
so it can't crash the way test_catalog.py did - it will show us
exactly which record (if any) is empty or malformed, and what
byte offsets produced it.
"""

import sys

from volume_header import read_volume_header
from fork_reader import ForkReader
from btree import BTree


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 diagnose_catalog.py <path_to_dmg> [partition_offset]")
        sys.exit(1)

    path = sys.argv[1]
    partition_offset = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    vh = read_volume_header(path, partition_offset)
    reader = ForkReader(
        image_path=path,
        block_size=vh.block_size,
        extents=vh.catalog_file.active_extents(),
        partition_offset=partition_offset,
    )
    btree = BTree(reader)

    print(btree.summary())

    for node_number, descriptor, records in btree.iter_leaf_nodes():
        print(f"--- Leaf node {node_number} (num_records={descriptor.num_records}, "
              f"f_link={descriptor.f_link}, b_link={descriptor.b_link}) ---")
        for i, raw in enumerate(records):
            hex_preview = raw[:24].hex(" ")
            print(f"  record[{i}] length={len(raw):<5} first_bytes={hex_preview}")
            if len(raw) < 2:
                print("    !!! EMPTY/TINY RECORD - this is the problem record !!!")


if __name__ == "__main__":
    main()
