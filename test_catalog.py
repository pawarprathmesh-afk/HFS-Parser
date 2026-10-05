"""
Milestone check for Phase 5: walk the actual Catalog B-tree of a real
HFS+ volume and print every file/folder found, with basic metadata.
This is the first point where the parser produces genuinely readable
output (names, sizes, dates) rather than just structural counts.
"""

import sys

from volume_header import read_volume_header, hfs_time_to_unix
from fork_reader import ForkReader
from btree import BTree
from catalog import CatalogTree, CatalogFolderRecord, CatalogFileRecord, ROOT_FOLDER_ID
import datetime


def format_hfs_time(hfs_ts: int) -> str:
    unix_ts = hfs_time_to_unix(hfs_ts)
    if unix_ts == 0:
        return "(none)"
    return datetime.datetime.fromtimestamp(unix_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_display(name: str) -> str:
    """
    Escape control characters (like the literal \\r Apple uses in
    '.HFS+ Private Directory Data\\r') so they don't corrupt terminal
    output. The underlying parsed data is untouched - this only
    affects what we print.
    """
    return "".join(c if (c.isprintable() or c == " ") else repr(c)[1:-1] for c in name)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_catalog.py <path_to_dmg> [partition_offset]")
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

    print(f"=== Raw leaf records ({btree.header.leaf_records} total) ===")
    for key, record in catalog.all_records_raw():
        kind = type(record).__name__
        print(f"  parent_id={key.parent_id:<6} name={safe_display(key.node_name)!r:<32} -> {kind}")

    print()
    print("=== Filesystem tree (walked from root, CNID 2) ===")
    found_any = False
    for full_path, record in catalog.walk(ROOT_FOLDER_ID):
        found_any = True
        display_path = safe_display(full_path)
        if isinstance(record, CatalogFolderRecord):
            print(f"  [DIR]  {display_path}  (valence={record.valence}, folder_id={record.folder_id})")
        elif isinstance(record, CatalogFileRecord):
            size = record.data_fork.logical_size
            print(f"  [FILE] {display_path}  ({size} bytes, file_id={record.file_id}, "
                  f"modified={format_hfs_time(record.content_mod_date)})")

    if not found_any:
        print("  (empty - no files or folders found under root)")


if __name__ == "__main__":
    main()
