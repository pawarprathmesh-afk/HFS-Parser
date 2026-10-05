"""
Raw hex dump of a specific B-tree node: the node descriptor (first 14
bytes) and the tail of the node (where the offset array should live),
plus a full hex dump if requested. This bypasses ALL of our own
offset-parsing logic - it just shows the literal bytes read from disk,
so we can verify by eye whether the offset array is really at the
position we assume, and whether it contains non-zero, sane values.
"""

import sys

from volume_header import read_volume_header
from fork_reader import ForkReader
from btree import BTree


def hex_dump(data: bytes, base_offset: int = 0, width: int = 16):
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {base_offset + i:08x}  {hex_part:<{width*3}}  {ascii_part}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 dump_node.py <path_to_dmg> [node_number] [partition_offset]")
        sys.exit(1)

    path = sys.argv[1]
    node_number = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    partition_offset = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    vh = read_volume_header(path, partition_offset)
    reader = ForkReader(
        image_path=path,
        block_size=vh.block_size,
        extents=vh.catalog_file.active_extents(),
        partition_offset=partition_offset,
    )
    btree = BTree(reader)

    print(f"node_size = {btree.node_size}")
    print(f"header.num_records reported for node {node_number}: (reading descriptor below)")
    print()

    raw = btree.read_node_raw(node_number)
    print(f"Total bytes read for node {node_number}: {len(raw)}")
    print()

    print("=== First 32 bytes (node descriptor + start of first record) ===")
    hex_dump(raw[0:32], base_offset=0)

    print()
    print("=== Last 128 bytes (where the offset array should be) ===")
    tail_start = len(raw) - 128
    hex_dump(raw[tail_start:], base_offset=tail_start)

    print()
    print("=== What our BTree.read_node() logic computes ===")
    import struct
    from btree import BTNodeDescriptor
    descriptor = BTNodeDescriptor.parse(raw[0:14])
    print(f"descriptor: f_link={descriptor.f_link}, b_link={descriptor.b_link}, "
          f"kind={descriptor.kind}, height={descriptor.height}, num_records={descriptor.num_records}")

    num_offsets = descriptor.num_records + 1
    print(f"num_offsets to read = num_records + 1 = {num_offsets}")
    raw_offsets_in_read_order = []
    for i in range(num_offsets):
        pos = btree.node_size - 2 * (i + 1)
        (val,) = struct.unpack(">H", raw[pos:pos + 2])
        raw_offsets_in_read_order.append((pos, val))
    print("Position -> value pairs, in the order our code reads them:")
    for pos, val in raw_offsets_in_read_order:
        print(f"  pos={pos} (0x{pos:04x})  value={val} (0x{val:04x})")

    descriptor2, records = btree.read_node(node_number)
    print()
    print(f"Resulting record count: {len(records)}")
    print(f"Resulting record lengths: {[len(r) for r in records]}")


if __name__ == "__main__":
    main()
