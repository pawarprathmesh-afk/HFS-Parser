"""
Deleted-file recovery for an HFS+ image.

Usage: python3 test_recover.py <image> [--out DIR] [--no-carve]

Strategy (best metadata first):
  1. Orphaned Catalog nodes, Catalog slack space, and the journal
     (journaled volumes) -> recovers NAME + extents.
  2. Free-space carving (--no-carve to skip) -> recovers content only,
     for volumes where the catalog records were scrubbed.
With --out, recovered files are written to that directory.
"""

import argparse
import hashlib
import os

from volume import open_volume
from recovery import DeletedFileScanner
from carver import carve_free_space


def safe_display(name: str) -> str:
    return "".join(c if (c.isprintable() or c == " ") else repr(c)[1:-1] for c in name)


def safe_filename(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "._- ") else "_" for c in name) or "unnamed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--out")
    ap.add_argument("--no-carve", action="store_true")
    args = ap.parse_args()
    path = args.image
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    vol = open_volume(path)
    path = vol.image_path
    partition_offset, vh = vol.partition_offset, vol.vh
    print(f"Partition: scheme={vol.part_info.scheme}, offset={partition_offset}")
    print(f"Journaled: {vh.is_journaled}")
    catalog_btree, af = vol.catalog_btree, vol.allocation_file()
    print(f"Catalog B-tree: {catalog_btree.header.total_nodes} total nodes, "
          f"{catalog_btree.header.leaf_records} live leaf records\n")

    print("=== Scanning catalog nodes / slack / journal for deleted file records ===")
    scanner = DeletedFileScanner(catalog_btree, allocation_file=af, image_path=path,
                                 volume_header=vh, partition_offset=partition_offset)
    recovered = scanner.scan()
    used_blocks = set()

    if not recovered:
        print("No deleted catalog records found.")

    for r in recovered:
        name = safe_display(r.key.node_name) or f"(file_id_{r.record.file_id})"
        fork = r.record.data_fork
        print(f"\n--- {name} ---")
        print(f"  file_id: {r.record.file_id}   parent_id: {r.key.parent_id}   source: {r.source}")
        print(f"  logical_size: {fork.logical_size}   blocks free: {r.blocks_free}/{r.blocks_checked}")
        for e in fork.active_extents():
            used_blocks.update(range(e.start_block, e.start_block + e.block_count))
        if not r.fully_free:
            print("  status: blocks reused/unknown - NOT recovering (content overwritten)")
            continue
        try:
            data = vol.read_deleted_file(r.record, r.decmpfs)
        except Exception as e:  # noqa: BLE001
            print(f"  extraction failed: {type(e).__name__}: {e}")
            continue
        print(f"  status: RECOVERED {len(data)} bytes, md5={hashlib.md5(data).hexdigest()}")
        printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
        if data and printable / len(data) > 0.9:
            print(f"  preview: {data[:80].decode('utf-8', errors='replace')!r}")
        if args.out:
            with open(os.path.join(args.out, f"{r.record.file_id}_{safe_filename(r.key.node_name)}"), "wb") as f:
                f.write(data)

    if args.no_carve:
        return
    print("\n=== Free-space carving (content only, no names) ===")
    carved = [c for c in carve_free_space(path, vh.block_size, af, partition_offset)
              if not set(range(c.start_block, c.start_block + c.block_count)) & used_blocks]
    if not carved:
        print("Nothing carved.")
    for i, c in enumerate(carved):
        print(f"  carved_{i}.{c.extension}: kind={c.kind} block={c.start_block} "
              f"len={len(c.data)} exact_end={c.exact_end} md5={hashlib.md5(c.data).hexdigest()}")
        if args.out:
            with open(os.path.join(args.out, f"carved_{i}_blk{c.start_block}.{c.extension}"), "wb") as f:
                f.write(c.data)


if __name__ == "__main__":
    main()
