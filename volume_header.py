"""
HFS+/HFSX Volume Header parser.

Reference: Apple Technical Note TN1150 (HFS Plus Volume Format)
Volume Header lives at a FIXED offset of 1024 bytes from the start
of the HFS+ volume (not necessarily the start of the file — if the
image has a partition table, add the partition's start offset first).
"""

import struct
from dataclasses import dataclass

HFS_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01 (Unix epoch)


def hfs_time_to_unix(hfs_timestamp: int) -> int:
    """Convert HFS+ timestamp (seconds since 1904) to Unix timestamp (seconds since 1970)."""
    if hfs_timestamp == 0:
        return 0
    return hfs_timestamp - HFS_EPOCH_OFFSET


@dataclass
class HFSPlusExtentDescriptor:
    start_block: int
    block_count: int

    @classmethod
    def parse(cls, data: bytes) -> "HFSPlusExtentDescriptor":
        start_block, block_count = struct.unpack(">II", data)
        return cls(start_block, block_count)

    @property
    def is_empty(self) -> bool:
        return self.start_block == 0 and self.block_count == 0


@dataclass
class HFSPlusForkData:
    logical_size: int
    clump_size: int
    total_blocks: int
    extents: list  # list[HFSPlusExtentDescriptor], length 8

    SIZE = 80  # bytes

    @classmethod
    def parse(cls, data: bytes) -> "HFSPlusForkData":
        if len(data) != cls.SIZE:
            raise ValueError(f"HFSPlusForkData requires exactly {cls.SIZE} bytes, got {len(data)}")

        logical_size, clump_size, total_blocks = struct.unpack(">QII", data[0:16])

        extents = []
        offset = 16
        for _ in range(8):
            extents.append(HFSPlusExtentDescriptor.parse(data[offset:offset + 8]))
            offset += 8

        return cls(logical_size, clump_size, total_blocks, extents)

    def active_extents(self):
        """Return only the non-empty extents (a fork rarely uses all 8 slots)."""
        return [e for e in self.extents if not e.is_empty]


@dataclass
class HFSPlusVolumeHeader:
    signature: str
    version: int
    attributes: int
    last_mounted_version: int
    journal_info_block: int

    create_date: int
    modify_date: int
    backup_date: int
    checked_date: int

    file_count: int
    folder_count: int

    block_size: int
    total_blocks: int
    free_blocks: int

    next_allocation: int
    rsrc_clump_size: int
    data_clump_size: int
    next_catalog_id: int

    write_count: int
    encodings_bitmap: int

    finder_info: tuple

    allocation_file: HFSPlusForkData
    extents_file: HFSPlusForkData
    catalog_file: HFSPlusForkData
    attributes_file: HFSPlusForkData
    startup_file: HFSPlusForkData

    HEADER_OFFSET = 1024   # fixed offset within the volume
    HEADER_SIZE = 512      # total size of the volume header structure

    # --- attribute bit flags (selected, most relevant ones) ---
    HFS_VOLUME_UNMOUNTED_BIT = 8
    HFS_VOLUME_JOURNALED_BIT = 13
    HFS_VOLUME_SOFTWARE_LOCK_BIT = 15

    @classmethod
    def parse(cls, raw: bytes) -> "HFSPlusVolumeHeader":
        """raw must be exactly the 512-byte Volume Header block."""
        if len(raw) != cls.HEADER_SIZE:
            raise ValueError(f"Volume header must be exactly {cls.HEADER_SIZE} bytes, got {len(raw)}")

        signature_raw, version, attributes, last_mounted_version, journal_info_block = \
            struct.unpack(">2sHII I", raw[0:16])

        signature = signature_raw.decode("ascii")
        if signature not in ("H+", "HX"):
            raise ValueError(f"Not an HFS+/HFSX volume header (signature={signature_raw!r})")

        create_date, modify_date, backup_date, checked_date = struct.unpack(">IIII", raw[16:32])
        file_count, folder_count = struct.unpack(">II", raw[32:40])
        block_size, total_blocks, free_blocks = struct.unpack(">III", raw[40:52])
        next_allocation, rsrc_clump_size, data_clump_size, next_catalog_id = \
            struct.unpack(">IIII", raw[52:68])
        write_count, encodings_bitmap = struct.unpack(">IQ", raw[68:80])
        finder_info = struct.unpack(">8I", raw[80:112])

        # 5 fork data structures, 80 bytes each, starting at offset 112
        offset = 112
        forks = []
        for _ in range(5):
            forks.append(HFSPlusForkData.parse(raw[offset:offset + 80]))
            offset += 80

        allocation_file, extents_file, catalog_file, attributes_file, startup_file = forks

        return cls(
            signature=signature,
            version=version,
            attributes=attributes,
            last_mounted_version=last_mounted_version,
            journal_info_block=journal_info_block,
            create_date=create_date,
            modify_date=modify_date,
            backup_date=backup_date,
            checked_date=checked_date,
            file_count=file_count,
            folder_count=folder_count,
            block_size=block_size,
            total_blocks=total_blocks,
            free_blocks=free_blocks,
            next_allocation=next_allocation,
            rsrc_clump_size=rsrc_clump_size,
            data_clump_size=data_clump_size,
            next_catalog_id=next_catalog_id,
            write_count=write_count,
            encodings_bitmap=encodings_bitmap,
            finder_info=finder_info,
            allocation_file=allocation_file,
            extents_file=extents_file,
            catalog_file=catalog_file,
            attributes_file=attributes_file,
            startup_file=startup_file,
        )

    @property
    def is_hfsx(self) -> bool:
        return self.signature == "HX"

    @property
    def is_journaled(self) -> bool:
        return bool(self.attributes & (1 << self.HFS_VOLUME_JOURNALED_BIT))

    def summary(self) -> str:
        lines = [
            f"Signature:        {self.signature} ({'HFSX' if self.is_hfsx else 'HFS+'})",
            f"Version:          {self.version}",
            f"Journaled:        {self.is_journaled}",
            f"Block size:       {self.block_size} bytes",
            f"Total blocks:     {self.total_blocks}  ({self.total_blocks * self.block_size / (1024*1024):.2f} MB)",
            f"Free blocks:      {self.free_blocks}",
            f"File count:       {self.file_count}",
            f"Folder count:     {self.folder_count}",
            f"Next Catalog ID:  {self.next_catalog_id}",
            f"Created:          {hfs_time_to_unix(self.create_date)} (unix)",
            f"Modified:         {hfs_time_to_unix(self.modify_date)} (unix)",
            "",
            f"Catalog file:     logical_size={self.catalog_file.logical_size}, "
            f"total_blocks={self.catalog_file.total_blocks}, "
            f"extents={self.catalog_file.active_extents()}",
            f"Extents file:     logical_size={self.extents_file.logical_size}, "
            f"total_blocks={self.extents_file.total_blocks}, "
            f"extents={self.extents_file.active_extents()}",
            f"Allocation file:  logical_size={self.allocation_file.logical_size}, "
            f"total_blocks={self.allocation_file.total_blocks}, "
            f"extents={self.allocation_file.active_extents()}",
            f"Attributes file:  logical_size={self.attributes_file.logical_size}, "
            f"total_blocks={self.attributes_file.total_blocks}, "
            f"extents={self.attributes_file.active_extents()}",
        ]
        return "\n".join(lines)


def read_volume_header(path: str, partition_offset: int = 0) -> HFSPlusVolumeHeader:
    """
    Read and parse the HFS+/HFSX Volume Header from a disk image.

    partition_offset: byte offset where the HFS+ volume itself begins
                       within the file (0 if the file IS the raw volume,
                       as with an image built with `hdiutil create -layout NONE`).
    """
    from fork_reader import read_physical      # honours a registered journal-replay overlay
    with open(path, "rb") as f:
        raw = read_physical(f, path, partition_offset + HFSPlusVolumeHeader.HEADER_OFFSET,
                            HFSPlusVolumeHeader.HEADER_SIZE)
    return HFSPlusVolumeHeader.parse(raw)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 volume_header.py <path_to_dmg> [partition_offset]")
        sys.exit(1)

    path = sys.argv[1]
    partition_offset = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    vh = read_volume_header(path, partition_offset)
    print(vh.summary())
