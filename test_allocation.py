"""
Milestone check for the Allocation File: read the actual bitmap off a
real volume and verify its computed free/allocated counts match the
Volume Header's own free_blocks field. These come from two separate
on-disk structures maintained independently by the filesystem, so
agreement here is a genuine cross-check, not a tautology.
"""

import sys

from partition import find_hfsplus_partition
from volume_header import read_volume_header
from allocation_file import AllocationFile


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_allocation.py <path_to_image>")
        sys.exit(1)

    path = sys.argv[1]
    part_info = find_hfsplus_partition(path)
    partition_offset = part_info.start_offset
    print(f"Partition: scheme={part_info.scheme}, offset={partition_offset}")

    vh = read_volume_header(path, partition_offset)
    print(f"Volume Header says: total_blocks={vh.total_blocks}, free_blocks={vh.free_blocks}")
    print()

    af = AllocationFile(
        image_path=path,
        block_size=vh.block_size,
        extents=vh.allocation_file.active_extents(),
        total_blocks=vh.total_blocks,
        partition_offset=partition_offset,
    )

    print("=== Allocation File bitmap analysis ===")
    print(af.summary())
    print()

    match = af.count_free() == vh.free_blocks
    print(f"Cross-check vs Volume Header free_blocks: "
          f"{'OK MATCH' if match else 'MISMATCH'} "
          f"(bitmap says {af.count_free()}, header says {vh.free_blocks})")

    # Sanity spot-check: block 0 is always used (it's part of the boot blocks /
    # reserved area before the volume header, block 0 itself is typically free
    # or reserved depending on layout - block that holds the Volume Header's
    # own allocation block should be marked allocated). We just report it,
    # not assert, since exact reserved-block conventions can vary slightly.
    print(f"\nBlock 0 allocated: {af.is_allocated(0)}")
    print(f"Block 1 allocated: {af.is_allocated(1)} (often the Allocation File's own first block)")


if __name__ == "__main__":
    main()
