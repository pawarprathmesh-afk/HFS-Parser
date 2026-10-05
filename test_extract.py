"""
Milestone check for file content extraction: walk the Catalog tree,
and for every FILE record found, extract its data fork bytes and
print them (or a hex preview for binary-looking content).

This is the "prove it end to end" step: file names/sizes/dates from
Phase 5 were metadata; this actually reads the bytes a real file
contains and checks them against what you know you wrote.
"""

import sys

from volume_header import read_volume_header
from fork_reader import ForkReader
from btree import BTree
from catalog import CatalogTree, CatalogFileRecord, ROOT_FOLDER_ID
from file_extract import extract_fork_data, FileExtractionError


def safe_display(name: str) -> str:
    return "".join(c if (c.isprintable() or c == " ") else repr(c)[1:-1] for c in name)


def looks_like_text(data: bytes) -> bool:
    if not data:
        return True
    # crude heuristic: mostly printable ASCII/UTF-8-ish bytes
    printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(data) > 0.9


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_extract.py <path_to_dmg> [partition_offset]")
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
    catalog = CatalogTree(btree)

    print("=== Extracting content of every file found ===")
    for full_path, record in catalog.walk(ROOT_FOLDER_ID):
        if not isinstance(record, CatalogFileRecord):
            continue

        display_path = safe_display(full_path)
        print(f"\n--- {display_path} (file_id={record.file_id}, "
              f"logical_size={record.data_fork.logical_size}) ---")

        try:
            data = extract_fork_data(path, vh.block_size, record.data_fork, partition_offset)
        except FileExtractionError as e:
            print(f"  EXTRACTION FAILED: {e}")
            continue

        if len(data) != record.data_fork.logical_size:
            print(f"  WARNING: extracted {len(data)} bytes but logical_size says "
                  f"{record.data_fork.logical_size} - mismatch!")

        if looks_like_text(data):
            preview = data.decode("utf-8", errors="replace")
            print(f"  content (text): {preview!r}")
        else:
            print(f"  content (binary, first 64 bytes hex): {data[:64].hex(' ')}")


if __name__ == "__main__":
    main()
