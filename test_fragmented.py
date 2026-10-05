"""
Milestone check for Extents Overflow support: find any file(s) whose
initial 8 extents don't cover their total_blocks, extract them using
the Extents Overflow B-tree, and report success/failure clearly.

Run this against a volume where you've deliberately created a
fragmented file (see the shell steps suggested alongside this script)
so the Extents Overflow path actually gets exercised, not just the
"no fragmentation needed" default case.
"""

import sys
import hashlib

from partition import find_hfsplus_partition
from volume_header import read_volume_header
from fork_reader import ForkReader
from btree import BTree
from catalog import CatalogTree, CatalogFileRecord, ROOT_FOLDER_ID
from file_extract import extract_fork_data, FileExtractionError
from extents_overflow import ExtentsOverflowTree, DATA_FORK_TYPE


def safe_display(name: str) -> str:
    return "".join(c if (c.isprintable() or c == " ") else repr(c)[1:-1] for c in name)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_fragmented.py <path_to_image>")
        sys.exit(1)

    path = sys.argv[1]

    part_info = find_hfsplus_partition(path)
    partition_offset = part_info.start_offset
    print(f"Partition: scheme={part_info.scheme}, offset={partition_offset}")

    vh = read_volume_header(path, partition_offset)

    reader = ForkReader(
        image_path=path, block_size=vh.block_size,
        extents=vh.catalog_file.active_extents(), partition_offset=partition_offset,
    )
    btree = BTree(reader)
    catalog = CatalogTree(btree)

    extents_overflow_tree = None
    if vh.extents_file.logical_size > 0:
        eo_reader = ForkReader(
            image_path=path, block_size=vh.block_size,
            extents=vh.extents_file.active_extents(), partition_offset=partition_offset,
        )
        eo_btree = BTree(eo_reader)
        extents_overflow_tree = ExtentsOverflowTree(eo_btree)
        print("Extents Overflow tree summary:")
        print(extents_overflow_tree.summary())
    else:
        print("Extents Overflow file is empty on this volume - no fragmented files exist.")

    print()
    print("=== Scanning for fragmented files ===")
    found_fragmented = False
    for full_path, record in catalog.walk(ROOT_FOLDER_ID):
        if not isinstance(record, CatalogFileRecord):
            continue

        covered = sum(e.block_count for e in record.data_fork.active_extents())
        if covered >= record.data_fork.total_blocks:
            continue  # not fragmented, nothing interesting to test here

        found_fragmented = True
        display_path = safe_display(full_path)
        print(f"\n--- FRAGMENTED: {display_path} ---")
        print(f"  logical_size:      {record.data_fork.logical_size}")
        print(f"  total_blocks:      {record.data_fork.total_blocks}")
        print(f"  initial extents:   {record.data_fork.active_extents()}")
        print(f"  blocks covered by initial 8: {covered}")

        try:
            data = extract_fork_data(
                path, vh.block_size, record.data_fork, partition_offset,
                file_id=record.file_id, fork_type=DATA_FORK_TYPE,
                extents_overflow_tree=extents_overflow_tree,
            )
        except FileExtractionError as e:
            print(f"  EXTRACTION FAILED: {e}")
            continue

        if len(data) != record.data_fork.logical_size:
            print(f"  WARNING: extracted {len(data)} bytes, expected {record.data_fork.logical_size}")
        else:
            print(f"  Extracted {len(data)} bytes successfully (length matches logical_size)")

        md5 = hashlib.md5(data).hexdigest()
        print(f"  MD5 of extracted content: {md5}")
        print("  (compare this to `md5 <original_file>` you ran on your Mac BEFORE deleting it)")

    if not found_fragmented:
        print("  No fragmented files found on this volume.")
        print("  (If you expected one, check that your test file is actually large/fragmented")
        print("   enough to exceed 8 extents - see the shell steps for forcing fragmentation.)")


if __name__ == "__main__":
    main()
