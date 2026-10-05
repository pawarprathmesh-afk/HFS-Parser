"""
Milestone check for Phase 2: open the Catalog B-tree via the Volume
Header's fork data, read its header node, and walk all leaf nodes,
confirming the total leaf record count matches what the B-tree header
claims (h.leaf_records).

We don't interpret record CONTENTS yet (that's Phase 5, Catalog-specific
key/record parsing) -- this just proves the node/record framing works.
"""

import sys

from volume_header import read_volume_header
from fork_reader import ForkReader
from btree import BTree


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_btree.py <path_to_dmg> [partition_offset]")
        sys.exit(1)

    path = sys.argv[1]
    partition_offset = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    vh = read_volume_header(path, partition_offset)
    print("=== Volume Header ===")
    print(vh.summary())
    print()

    for fork_name in ("catalog_file", "extents_file", "attributes_file"):
        fork = getattr(vh, fork_name)
        if fork.logical_size == 0:
            print(f"=== {fork_name} === (empty / unused, skipping)")
            continue

        print(f"=== {fork_name} ===")
        reader = ForkReader(
            image_path=path,
            block_size=vh.block_size,
            extents=fork.active_extents(),
            partition_offset=partition_offset,
        )
        btree = BTree(reader)
        print(btree.summary())

        total_leaf_records_seen = 0
        leaf_node_count = 0
        zero_length_records = 0
        for node_number, descriptor, records in btree.iter_leaf_nodes():
            leaf_node_count += 1
            total_leaf_records_seen += len(records)
            for r in records:
                if len(r) == 0:
                    zero_length_records += 1

        print(f"Leaf nodes walked:        {leaf_node_count}")
        print(f"Leaf records counted:     {total_leaf_records_seen}")
        print(f"Leaf records (header):    {btree.header.leaf_records}")
        print(f"Zero-length records:      {zero_length_records}  "
              f"(must be 0 - a nonzero count here means the offset-array parsing is broken, "
              f"even if the count above matches)")
        count_ok = total_leaf_records_seen == btree.header.leaf_records
        no_empties = zero_length_records == 0
        match = "OK MATCH" if (count_ok and no_empties) else "MISMATCH / CORRUPT"
        print(f"Consistency check:        {match}")
        print()


if __name__ == "__main__":
    main()
