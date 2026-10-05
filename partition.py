"""
Partition table detection for GPT and Apple Partition Map (APM).

Goal: given a raw disk image file, find the byte offset where an
HFS+/HFSX partition actually begins, so the rest of the parser
(volume_header.py onward) can be pointed at the right spot via
partition_offset.

We do this by:
  1. Trying to read a Volume Header directly at byte 1024 (no
     partition table at all - e.g. images made with -layout NONE).
  2. If that fails, checking for a GPT header at byte 512 (LBA 1),
     signature b"EFI PART".
  3. If that fails, checking for an Apple Partition Map at byte 512
     (block 1), signature b"PM".
  4. If none of these find a valid HFS+/HFSX signature, report failure
     clearly rather than guessing.

This module deliberately does NOT try to be a general-purpose GPT/APM
parser (no full attribute decoding, no CRC32 validation of GPT header/
entries) - only enough to locate partitions and identify HFS+ ones by
actually checking for the H+ /HX signature at each candidate's +1024
offset. Checking the real signature, rather than trusting a partition
type GUID/string alone, means we don't misidentify a partition due to
a mislabeled type field.
"""

import struct
from dataclasses import dataclass


HFS_SIGNATURES = (b"H+", b"HX")


def _has_hfsplus_signature_at(f, byte_offset: int) -> bool:
    """Check for H+ / HX signature at the given offset's +1024 volume header position."""
    try:
        f.seek(byte_offset + 1024)
        sig = f.read(2)
        return sig in HFS_SIGNATURES
    except OSError:
        return False


def _locate_volume(f, byte_offset: int):
    """Return the byte offset of the HFS+/HFSX volume that starts at (or is wrapped by an old
    HFS volume at) byte_offset, or None. A wrapper is a classic HFS volume ('BD' signature)
    whose embedded-volume fields point at the real HFS+ volume."""
    try:
        f.seek(byte_offset + 1024)
        mdb = f.read(162)
    except OSError:
        return None
    if mdb[:2] in HFS_SIGNATURES:
        return byte_offset
    if mdb[:2] == b"BD" and len(mdb) >= 130 and mdb[124:126] in (b"H+", b"HX"):
        al_blk_siz = struct.unpack(">I", mdb[20:24])[0]
        al_bl_st = struct.unpack(">H", mdb[28:30])[0]
        start_block, _count = struct.unpack(">HH", mdb[126:130])
        start = byte_offset + al_bl_st * 512 + start_block * al_blk_siz
        if _has_hfsplus_signature_at(f, start):
            return start
    return None


@dataclass
class PartitionInfo:
    scheme: str          # "none", "gpt", "apm" or "mbr"
    start_offset: int    # byte offset where the HFS+ volume begins
    size_bytes: int       # partition size in bytes, if known (0 if unknown)
    label: str = ""       # partition name/label, if the scheme provides one


def _try_no_partition_table(image_path: str) -> PartitionInfo | None:
    with open(image_path, "rb") as f:
        start = _locate_volume(f, 0)
        if start is not None:
            return PartitionInfo(scheme="none" if start == 0 else "hfs-wrapper",
                                 start_offset=start, size_bytes=0, label="")
    return None


def _try_gpt(image_path: str) -> PartitionInfo | None:
    """
    GPT header lives at LBA 1, signature b'EFI PART'. Partition entry array location/size/count
    come from the header. Each entry is 128 bytes (standard): type GUID, unique GUID, first LBA
    (UInt64 LE) at offset 32, last LBA at 40, UTF-16LE name at offset 56. Both 512-byte and
    4096-byte logical sectors are tried.
    """
    with open(image_path, "rb") as f:
        for sector in (512, 4096):
            f.seek(sector)
            gpt_header = f.read(92)
            if gpt_header[0:8] != b"EFI PART":
                continue
            partition_entry_lba = struct.unpack("<Q", gpt_header[72:80])[0]
            num_entries = struct.unpack("<I", gpt_header[80:84])[0]
            entry_size = struct.unpack("<I", gpt_header[84:88])[0]
            if not (0 < num_entries <= 4096 and entry_size >= 128):
                continue

            f.seek(partition_entry_lba * sector)
            entries_raw = f.read(num_entries * entry_size)

            for i in range(num_entries):
                entry = entries_raw[i * entry_size: (i + 1) * entry_size]
                if len(entry) < 128 or entry[0:16] == b"\x00" * 16:
                    continue  # unused entry
                first_lba, last_lba = struct.unpack("<QQ", entry[32:48])
                name = entry[56:128].decode("utf-16-le", errors="ignore").rstrip("\x00")
                start = _locate_volume(f, first_lba * sector)
                if start is not None:
                    return PartitionInfo(scheme="gpt", start_offset=start,
                                         size_bytes=(last_lba - first_lba + 1) * sector, label=name)
    return None


def _try_apm(image_path: str) -> PartitionInfo | None:
    """
    Apple Partition Map: block 0 is the driver descriptor (signature
    b'ER'), blocks 1..N are partition map entries, each starting with
    signature b'PM' and describing one partition (including itself,
    as the first entry, which is the map describing all partitions).
    Standard block size for APM is 512 bytes.
    """
    BLOCK_SIZE = 512

    with open(image_path, "rb") as f:
        f.seek(0)
        driver_sig = f.read(2)
        if driver_sig != b"ER":
            return None

        # Read the first partition map entry to learn how many entries exist (pmMapBlkCnt)
        f.seek(1 * BLOCK_SIZE)
        first_entry = f.read(BLOCK_SIZE)
        if first_entry[0:2] != b"PM":
            return None

        (map_block_count,) = struct.unpack(">I", first_entry[4:8])

        for block_num in range(1, map_block_count + 1):
            f.seek(block_num * BLOCK_SIZE)
            entry = f.read(BLOCK_SIZE)
            if entry[0:2] != b"PM":
                continue

            # pmPyPartStart (UInt32, blocks 8:12), pmPartBlkCnt (12:16), pmPartName (16:48, 32 bytes),
            # pmParType (48:80, 32 bytes) - all big-endian per classic Mac OS struct layout
            part_start_block, part_block_count = struct.unpack(">II", entry[8:16])
            part_name = entry[16:48].split(b"\x00")[0].decode("macroman", errors="ignore")

            start_offset = part_start_block * BLOCK_SIZE
            size_bytes = part_block_count * BLOCK_SIZE

            located = _locate_volume(f, start_offset)
            if located is not None:
                return PartitionInfo(
                    scheme="apm", start_offset=located,
                    size_bytes=size_bytes, label=part_name,
                )

    return None


def _try_mbr(image_path: str) -> PartitionInfo | None:
    """Classic MBR: four 16-byte entries at byte 446 (type at +4, start LBA at +8, sectors at +12)."""
    with open(image_path, "rb") as f:
        f.seek(0)
        sector0 = f.read(512)
        if len(sector0) < 512 or sector0[510:512] != b"\x55\xaa":
            return None
        for i in range(4):
            entry = sector0[446 + 16 * i: 462 + 16 * i]
            ptype = entry[4]
            start_lba, num_sectors = struct.unpack("<II", entry[8:16])
            if ptype == 0 or ptype == 0xEE or start_lba == 0:
                continue
            for sector in (512, 4096):
                start = _locate_volume(f, start_lba * sector)
                if start is not None:
                    return PartitionInfo(scheme="mbr", start_offset=start,
                                         size_bytes=num_sectors * sector, label=f"type 0x{ptype:02x}")
    return None


def find_hfsplus_partition(image_path: str) -> PartitionInfo:
    """
    Try, in order: no partition table (or HFS wrapper), GPT, APM, MBR.
    Raises ValueError with a clear message if none find an HFS+/HFSX volume.
    """
    for finder in (_try_no_partition_table, _try_gpt, _try_apm, _try_mbr):
        result = finder(image_path)
        if result is not None:
            return result

    raise ValueError(
        f"Could not locate an HFS+/HFSX volume in {image_path!r}. "
        f"Checked: no partition table / HFS wrapper (byte 0), GPT (512 and 4096-byte sectors), APM, MBR. "
        f"This image may use a different partition scheme (MBR-only), may not "
        f"contain an HFS+ volume, or may need a different sector size assumption."
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 partition.py <path_to_image>")
        sys.exit(1)

    info = find_hfsplus_partition(sys.argv[1])
    print(f"Scheme:       {info.scheme}")
    print(f"Start offset: {info.start_offset} (0x{info.start_offset:x})")
    print(f"Size:         {info.size_bytes} bytes")
    print(f"Label:        {info.label!r}")
