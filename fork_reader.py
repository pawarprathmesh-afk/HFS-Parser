"""
Fork reader: turns a fork's extent list into something you can read
arbitrary byte ranges from, as if it were a single contiguous file.

This does NOT yet handle Extents Overflow (extents beyond the initial 8
stored in the Volume Header / Catalog record) - that's added once the
Extents Overflow B-tree is implemented. For now it raises clearly if you
try to read past what the given extents cover, so you always know when
you've hit that wall rather than silently reading wrong data.
"""



class ForkReadError(Exception):
    pass


# Patches applied on top of an image when it is read, keyed by image path. Used to present a
# volume as it will look once its pending journal transactions are replayed, without ever
# modifying the evidence file. Each patch is (absolute byte offset in the image, data).
_OVERLAYS = {}


def set_overlay(image_path: str, patches):
    _OVERLAYS[image_path] = list(patches)


def clear_overlay(image_path: str):
    _OVERLAYS.pop(image_path, None)


def read_physical(f, image_path: str, pos: int, length: int) -> bytes:
    """Read from an open image file, with any registered overlay applied."""
    f.seek(pos)
    data = f.read(length)
    patches = _OVERLAYS.get(image_path)
    if not patches:
        return data
    buf = bytearray(data)
    end = pos + len(buf)
    for start, patch in patches:                       # in commit order: later patches win
        p_end = start + len(patch)
        if p_end <= pos or start >= end:
            continue
        a, b = max(pos, start), min(end, p_end)
        buf[a - pos:b - pos] = patch[a - start:b - start]
    return bytes(buf)


class ForkReader:
    def __init__(self, image_path: str, block_size: int, extents: list, partition_offset: int = 0):
        """
        image_path:       path to the disk image / volume file
        block_size:       allocation block size from the Volume Header
        extents:          list of HFSPlusExtentDescriptor (active ones only, no empty padding)
        partition_offset: byte offset where the HFS+ volume starts in the file
        """
        self.image_path = image_path
        self.block_size = block_size
        self.extents = extents
        self.partition_offset = partition_offset

        # Precompute cumulative logical block ranges covered by each extent,
        # so we can binary-search / linear-scan which extent a logical block falls in.
        self._ranges = []  # list of (logical_start_block, logical_end_block_exclusive, extent)
        cursor = 0
        for ext in extents:
            self._ranges.append((cursor, cursor + ext.block_count, ext))
            cursor += ext.block_count
        self.total_blocks_covered = cursor

    def _locate(self, logical_block: int):
        """Returns (physical byte offset of that block, blocks remaining in its extent)."""
        for start, end, ext in self._ranges:
            if start <= logical_block < end:
                physical_block = ext.start_block + (logical_block - start)
                return self.partition_offset + physical_block * self.block_size, end - logical_block
        raise ForkReadError(
            f"Logical block {logical_block} is beyond the extents provided "
            f"(covers 0..{self.total_blocks_covered}). "
            f"This likely means the fork needs Extents Overflow B-tree lookups, "
            f"which are not yet wired in."
        )

    def read(self, logical_offset: int, length: int) -> bytes:
        """
        Read `length` bytes starting at `logical_offset` bytes into the fork.
        Handles reads that span multiple extents / allocation blocks.
        """
        if length == 0:
            return b""

        result = bytearray()
        remaining = length
        current_offset = logical_offset

        with open(self.image_path, "rb") as f:
            while remaining > 0:
                logical_block = current_offset // self.block_size
                offset_within_block = current_offset % self.block_size

                physical_block_start, blocks_left = self._locate(logical_block)
                physical_read_pos = physical_block_start + offset_within_block

                # Read to the end of the current extent in one go (extents are physically
                # contiguous), then loop again if the read continues into the next extent.
                bytes_left_in_extent = blocks_left * self.block_size - offset_within_block
                chunk_len = min(remaining, bytes_left_in_extent)

                chunk = read_physical(f, self.image_path, physical_read_pos, chunk_len)
                if len(chunk) != chunk_len:
                    raise ForkReadError(
                        f"Short read at physical offset {physical_read_pos}: "
                        f"expected {chunk_len} bytes, got {len(chunk)}"
                    )

                result.extend(chunk)
                current_offset += chunk_len
                remaining -= chunk_len

        return bytes(result)
