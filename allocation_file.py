"""
HFS+ Allocation File parser.

Reference: TN1150, "The Allocation File".

The Allocation File is a simple bitmap: one bit per allocation block
in the volume. Bit N corresponds to allocation block N. A bit set to
1 means the block is allocated (in use); 0 means it's free.

Bits are packed MSB-first within each byte (bit 7 of byte 0 = block 0,
bit 6 of byte 0 = block 1, ..., bit 0 of byte 0 = block 7, bit 7 of
byte 1 = block 8, and so on).
"""

from fork_reader import ForkReader


class AllocationFile:
    def __init__(self, image_path: str, block_size: int, extents: list,
                 total_blocks: int, partition_offset: int = 0):
        """
        total_blocks: total number of allocation blocks in the volume
                      (from the Volume Header) - this is how many bits
                      we actually care about, even though the bitmap
                      itself is padded up to a whole allocation block.
        """
        self.total_blocks = total_blocks
        self.reader = ForkReader(
            image_path=image_path,
            block_size=block_size,
            extents=extents,
            partition_offset=partition_offset,
        )
        num_bytes_needed = (total_blocks + 7) // 8
        self._bitmap = self.reader.read(0, num_bytes_needed)

    def is_allocated(self, block_number: int) -> bool:
        if block_number < 0 or block_number >= self.total_blocks:
            raise IndexError(f"Block {block_number} out of range (0..{self.total_blocks - 1})")
        byte_index = block_number // 8
        bit_index = 7 - (block_number % 8)  # MSB-first
        return bool(self._bitmap[byte_index] & (1 << bit_index))

    def count_allocated(self) -> int:
        count = 0
        for block_number in range(self.total_blocks):
            if self.is_allocated(block_number):
                count += 1
        return count

    def count_free(self) -> int:
        return self.total_blocks - self.count_allocated()

    def free_ranges(self):
        """
        Yield (start_block, block_count) for each contiguous run of free
        blocks. Useful for cross-checking against fragmentation patterns,
        or as a building block for a future "recover deleted file data"
        feature (free space is where deleted file content often still sits,
        until overwritten).
        """
        start = None
        for block_number in range(self.total_blocks):
            allocated = self.is_allocated(block_number)
            if not allocated and start is None:
                start = block_number
            elif allocated and start is not None:
                yield (start, block_number - start)
                start = None
        if start is not None:
            yield (start, self.total_blocks - start)

    def summary(self) -> str:
        allocated = self.count_allocated()
        free = self.count_free()
        ranges = list(self.free_ranges())
        return (
            f"Total blocks:      {self.total_blocks}\n"
            f"Allocated blocks:  {allocated}\n"
            f"Free blocks:       {free}\n"
            f"Free space runs:   {len(ranges)} contiguous run(s)\n"
            f"Largest free run:  {max((r[1] for r in ranges), default=0)} blocks"
        )
