"""
Milestone check for the Attributes B-tree: dump every extended
attribute record found on a real volume, decoding inline data where
present.
"""

import sys

from partition import find_hfsplus_partition
from volume_header import read_volume_header
from fork_reader import ForkReader
from btree import BTree
from attributes import AttributesTree, AttributeInlineData, AttributeForkData, AttributeExtents


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_attributes.py <path_to_image>")
        sys.exit(1)

    path = sys.argv[1]
    part_info = find_hfsplus_partition(path)
    partition_offset = part_info.start_offset
    print(f"Partition: scheme={part_info.scheme}, offset={partition_offset}")

    vh = read_volume_header(path, partition_offset)

    if vh.attributes_file.logical_size == 0:
        print("Attributes file is empty on this volume - no extended attributes exist.")
        return

    reader = ForkReader(
        image_path=path, block_size=vh.block_size,
        extents=vh.attributes_file.active_extents(), partition_offset=partition_offset,
    )
    btree = BTree(reader)
    attrs = AttributesTree(btree)

    print(f"=== Attribute records ({btree.header.leaf_records} total) ===")
    for key, record in attrs.all_records_raw():
        print(f"\nfile_id={key.file_id}  attr_name={key.attr_name!r}")
        if isinstance(record, AttributeInlineData):
            print(f"  type: inline data, {len(record.data)} bytes")
            printable = sum(1 for b in record.data if 32 <= b < 127)
            if record.data and printable / len(record.data) > 0.8:
                print(f"  value (text): {record.data.decode('utf-8', errors='replace')!r}")
            else:
                print(f"  value (hex):  {record.data[:64].hex(' ')}")
        elif isinstance(record, AttributeForkData):
            print(f"  type: fork data, logical_size={record.fork.logical_size}, "
                  f"total_blocks={record.fork.total_blocks}")
        elif isinstance(record, AttributeExtents):
            print(f"  type: extents continuation, {len(record.extents)} extent(s): {record.extents}")


if __name__ == "__main__":
    main()
