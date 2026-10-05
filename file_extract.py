"""
File content extraction: given a CatalogFileRecord (from catalog.py),
read the actual bytes of its data fork (or resource fork) off disk.

Handles fragmentation beyond the initial 8 extents stored in the
Catalog record by querying the Extents Overflow B-tree (see
extents_overflow.py) for the remaining extents, when one is provided.
"""

from fork_reader import ForkReader
from catalog import HFSPlusForkDataMini, HFSPlusExtentDescriptorMini
from extents_overflow import ExtentsOverflowTree


class FileExtractionError(Exception):
    pass


def extract_fork_data(image_path: str, block_size: int, fork: HFSPlusForkDataMini,
                       partition_offset: int = 0, file_id: int = None,
                       fork_type: int = None,
                       extents_overflow_tree: "ExtentsOverflowTree" = None) -> bytes:
    """
    Read and return the full logical content of a fork (data or resource).

    If the fork's initial 8 extents don't cover its total_blocks, and
    both file_id/fork_type and an ExtentsOverflowTree are provided,
    the remaining extents are looked up and appended automatically.
    """
    active_extents = fork.active_extents()
    blocks_covered = sum(e.block_count for e in active_extents)

    if blocks_covered < fork.total_blocks:
        if extents_overflow_tree is not None and file_id is not None and fork_type is not None:
            overflow_extents = extents_overflow_tree.get_overflow_extents(file_id, fork_type)
            # Convert to the same descriptor type active_extents() uses, so ForkReader
            # can treat the combined list uniformly.
            active_extents = list(active_extents) + [
                HFSPlusExtentDescriptorMini(e.start_block, e.block_count) for e in overflow_extents
            ]
            blocks_covered = sum(e.block_count for e in active_extents)

        if blocks_covered < fork.total_blocks:
            raise FileExtractionError(
                f"This fork needs {fork.total_blocks} blocks but only {blocks_covered} "
                f"are covered by known extents (initial 8"
                f"{' + Extents Overflow lookup' if extents_overflow_tree is not None else ', no Extents Overflow tree provided'}). "
                f"{'The overflow tree has no matching record for this file/fork - ' if extents_overflow_tree is not None else ''}"
                f"data cannot be fully reconstructed."
            )

    if fork.logical_size == 0:
        return b""

    reader = ForkReader(
        image_path=image_path,
        block_size=block_size,
        extents=active_extents,
        partition_offset=partition_offset,
    )

    allocated_size = fork.total_blocks * block_size
    raw = reader.read(0, allocated_size)
    return raw[:fork.logical_size]
