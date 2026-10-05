"""
Full pipeline, now with automatic partition detection:
  1. Find the HFS+/HFSX partition (no table / GPT / APM).
  2. Parse its Volume Header.
  3. Walk its Catalog tree.
  4. Extract every file's content.

This supersedes needing to manually pass a partition_offset - it's
now detected automatically, which means this should work on both
your -layout NONE test image AND a real, partitioned disk image
(like the original SPUD-layout test.dmg from earlier).
"""

import sys

from partition import find_hfsplus_partition
from volume_header import read_volume_header
from fork_reader import ForkReader
from btree import BTree
from catalog import CatalogTree, CatalogFileRecord, CatalogFolderRecord, ROOT_FOLDER_ID
from file_extract import extract_fork_data, FileExtractionError
from extents_overflow import ExtentsOverflowTree, DATA_FORK_TYPE


def safe_display(name: str) -> str:
    return "".join(c if (c.isprintable() or c == " ") else repr(c)[1:-1] for c in name)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 run_parser.py <path_to_image>")
        sys.exit(1)

    path = sys.argv[1]

    print("=== Partition detection ===")
    part_info = find_hfsplus_partition(path)
    print(f"Scheme:       {part_info.scheme}")
    print(f"Start offset: {part_info.start_offset} (0x{part_info.start_offset:x})")
    if part_info.size_bytes:
        print(f"Size:         {part_info.size_bytes} bytes")
    if part_info.label:
        print(f"Label:        {part_info.label!r}")
    print()

    partition_offset = part_info.start_offset

    print("=== Volume Header ===")
    vh = read_volume_header(path, partition_offset)
    print(vh.summary())
    print()

    reader = ForkReader(
        image_path=path,
        block_size=vh.block_size,
        extents=vh.catalog_file.active_extents(),
        partition_offset=partition_offset,
    )
    btree = BTree(reader)
    catalog = CatalogTree(btree)

    # Set up the Extents Overflow tree too, so fragmented files can be fully read.
    # If the overflow fork is empty (no fragmented files exist), this tree just
    # has zero records and get_overflow_extents() always returns [].
    extents_overflow_tree = None
    if vh.extents_file.logical_size > 0:
        eo_reader = ForkReader(
            image_path=path,
            block_size=vh.block_size,
            extents=vh.extents_file.active_extents(),
            partition_offset=partition_offset,
        )
        eo_btree = BTree(eo_reader)
        extents_overflow_tree = ExtentsOverflowTree(eo_btree)

    print("=== Filesystem tree ===")
    for full_path, record in catalog.walk(ROOT_FOLDER_ID):
        display_path = safe_display(full_path)
        if isinstance(record, CatalogFolderRecord):
            print(f"  [DIR]  {display_path}  (valence={record.valence})")
        elif isinstance(record, CatalogFileRecord):
            fragmented_note = ""
            covered = sum(e.block_count for e in record.data_fork.active_extents())
            if covered < record.data_fork.total_blocks:
                fragmented_note = "  [FRAGMENTED - needs Extents Overflow]"
            print(f"  [FILE] {display_path}  ({record.data_fork.logical_size} bytes){fragmented_note}")

    print()
    print("=== Sample content extraction (first text-looking file found) ===")
    for full_path, record in catalog.walk(ROOT_FOLDER_ID):
        if isinstance(record, CatalogFileRecord) and record.data_fork.logical_size > 0:
            try:
                data = extract_fork_data(
                    path, vh.block_size, record.data_fork, partition_offset,
                    file_id=record.file_id, fork_type=DATA_FORK_TYPE,
                    extents_overflow_tree=extents_overflow_tree,
                )
            except FileExtractionError:
                continue
            printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
            if data and printable / len(data) > 0.9:
                print(f"  {safe_display(full_path)}: {data.decode('utf-8', errors='replace')!r}")
                break


if __name__ == "__main__":
    main()
