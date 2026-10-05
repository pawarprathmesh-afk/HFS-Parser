"""
Single entry point for reading an HFS+/HFSX volume.

    vol = open_volume("disk.dmg")
    for path, record in vol.walk(): ...
    data = vol.read_file(record)                 # data fork (decompressed if needed)
    rsrc = vol.read_file(record, resource=True)  # resource fork

Handles what the individual modules don't know about on their own:
  * non-raw image containers (compressed/sparse .dmg, sparsebundle) -> converted
    to a temporary raw image with hdiutil (macOS only)
  * hard links (Catalog stubs that point at iNode<N> files in the private data dir)
  * HFS+ transparent compression (com.apple.decmpfs)
  * resource forks
"""

import atexit
import os
import plistlib
import shutil
import subprocess
import tempfile
from typing import Dict, Iterator, Optional, Tuple

from partition import find_hfsplus_partition, PartitionInfo
from volume_header import read_volume_header, HFSPlusVolumeHeader
from fork_reader import ForkReader
from btree import BTree
from catalog import CatalogTree, CatalogFileRecord, ROOT_FOLDER_ID
from attributes import AttributesTree, AttributeInlineData, AttributeForkData
from allocation_file import AllocationFile
from extents_overflow import ExtentsOverflowTree, DATA_FORK_TYPE, RESOURCE_FORK_TYPE
from file_extract import extract_fork_data
from fork_reader import set_overlay, clear_overlay
from journal import read_journal_info, read_journal_bytes, pending_blocks
import decmpfs

PRIVATE_DATA_DIR = "\x00\x00\x00\x00HFS+ Private Data"
UF_COMPRESSED = 0x20
DECMPFS_ATTR = "com.apple.decmpfs"


class ImageError(Exception):
    pass


_converted: Dict[tuple, str] = {}


def ensure_raw_image(path: str) -> str:
    """Return a path to a raw image of `path`. Raw images are returned unchanged;
    other containers are converted to a temporary raw file (deleted at exit, cached per
    source file so repeated operations don't reconvert)."""
    try:
        st = os.stat(path)
        cache_key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key and cache_key in _converted and os.path.exists(_converted[cache_key]):
        return _converted[cache_key]
    result = _ensure_raw_image(path)
    if cache_key and result != path:
        _converted[cache_key] = result
    return result


def _ensure_raw_image(path: str) -> str:
    if os.path.isdir(path) or not _is_raw_hfs_image(path):
        if shutil.which("hdiutil") is None:
            raise ImageError(f"{path!r} is not a raw image and hdiutil (macOS) is unavailable to convert it. "
                             f"Convert it to a raw image first.")
        info = subprocess.run(["hdiutil", "imageinfo", "-plist", path], capture_output=True)
        if info.returncode != 0:
            raise ImageError(f"hdiutil cannot read {path!r}: {info.stderr.decode(errors='replace').strip()}")
        fmt = plistlib.loads(info.stdout).get("Format", "")
        if fmt == "UDRW" and not os.path.isdir(path):
            return path     # raw container whose volume is simply not HFS+: let the caller report it
        tmpdir = tempfile.mkdtemp(prefix="hfsparser_")
        atexit.register(shutil.rmtree, tmpdir, True)
        out = os.path.join(tmpdir, "converted")
        r = subprocess.run(["hdiutil", "convert", path, "-format", "UDRW", "-o", out],
                           capture_output=True)
        if r.returncode != 0:
            raise ImageError(f"hdiutil convert failed for {path!r}: {r.stderr.decode(errors='replace').strip()}")
        return out + ".dmg" if os.path.exists(out + ".dmg") else out
    return path


def _is_raw_hfs_image(path: str) -> bool:
    try:
        find_hfsplus_partition(path)
        return True
    except (ValueError, OSError):
        return False


class Volume:
    def __init__(self, image_path: str):
        self.source_path = image_path
        self.image_path = ensure_raw_image(image_path)
        self.header_source = "primary"
        try:
            self.part_info = find_hfsplus_partition(self.image_path)
            self.partition_offset = self.part_info.start_offset
            self.vh = read_volume_header(self.image_path, self.partition_offset)
        except ValueError:
            self._open_from_alternate_header()

        self._apply_pending_journal()

        self.catalog_btree = BTree(self._fork_reader(self.vh.catalog_file))
        self.catalog = CatalogTree(self.catalog_btree)

        self.extents_overflow_tree: Optional[ExtentsOverflowTree] = None
        if self.vh.extents_file.logical_size > 0:
            self.extents_overflow_tree = ExtentsOverflowTree(BTree(self._fork_reader(self.vh.extents_file)))

        self.attributes_tree: Optional[AttributesTree] = None
        self._attrs_by_file: Optional[Dict[int, list]] = None
        if self.vh.attributes_file.logical_size > 0:
            self.attributes_tree = AttributesTree(BTree(self._fork_reader(self.vh.attributes_file)))

    def _apply_pending_journal(self):
        """If the volume was not cleanly unmounted its journal holds committed-but-unapplied
        blocks. Present the volume as it will be after replay (in memory; the image file is
        never modified)."""
        self.journal_pending_blocks = 0
        self.journal_problem = None
        clear_overlay(self.image_path)
        if not self.vh.is_journaled:
            return
        info = read_journal_info(self.image_path, self.vh, self.partition_offset)
        if info is None or not info.in_volume:
            return
        blocks, problem = pending_blocks(read_journal_bytes(self.image_path, info, self.partition_offset))
        self.journal_problem = problem
        if blocks:
            set_overlay(self.image_path, [(self.partition_offset + off, data) for off, data in blocks])
            self.journal_pending_blocks = len(blocks)
            self.vh = read_volume_header(self.image_path, self.partition_offset)  # may itself be journaled

    def _open_from_alternate_header(self):
        """The primary Volume Header (1024 bytes in) is damaged or wiped. HFS+ keeps a copy 1024
        bytes before the end of the volume; use it if this file is a bare volume."""
        size = os.path.getsize(self.image_path)
        for end in (size, size - 512):           # some tools append a 512-byte trailer
            try:
                with open(self.image_path, "rb") as f:
                    f.seek(end - 1024)
                    vh = HFSPlusVolumeHeader.parse(f.read(512))
            except (ValueError, OSError):
                continue
            self.vh = vh
            self.partition_offset = 0
            self.part_info = PartitionInfo(scheme="none (primary header damaged)", start_offset=0,
                                           size_bytes=0, label="")
            self.header_source = "alternate"
            return
        raise ImageError(f"no HFS+/HFSX volume found in {self.source_path!r} "
                         f"(primary header missing and no alternate header at the end)")

    # -- plumbing ---------------------------------------------------------
    def _fork_reader(self, fork) -> ForkReader:
        return ForkReader(image_path=self.image_path, block_size=self.vh.block_size,
                          extents=fork.active_extents(), partition_offset=self.partition_offset)

    def allocation_file(self) -> AllocationFile:
        return AllocationFile(image_path=self.image_path, block_size=self.vh.block_size,
                              extents=self.vh.allocation_file.active_extents(),
                              total_blocks=self.vh.total_blocks, partition_offset=self.partition_offset)

    # -- namespace --------------------------------------------------------
    def walk(self) -> Iterator[Tuple[str, object]]:
        return self.catalog.walk(ROOT_FOLDER_ID)

    def resolve(self, record: CatalogFileRecord) -> CatalogFileRecord:
        """Hard-link stubs hold no data; return the iNode<N> record that does."""
        if not record.is_hard_link:
            return record
        private = self.catalog.lookup(ROOT_FOLDER_ID, PRIVATE_DATA_DIR)
        if private is None:
            raise ImageError("hard link found but the private data directory is missing")
        inode = self.catalog.lookup(private.folder_id, f"iNode{record.special}")
        if inode is None:
            raise ImageError(f"hard link target iNode{record.special} not found (orphaned link)")
        return inode

    # -- attributes -------------------------------------------------------
    def attributes(self, file_id: int) -> Dict[str, object]:
        if self.attributes_tree is None:
            return {}
        if self._attrs_by_file is None:
            self._attrs_by_file = {}
            for key, rec in self.attributes_tree.all_records_raw():
                self._attrs_by_file.setdefault(key.file_id, []).append((key, rec))
        return {k.attr_name: r for k, r in self._attrs_by_file.get(file_id, [])
                if not (hasattr(r, "extents") and not hasattr(r, "data") and not hasattr(r, "fork"))}

    def attribute_bytes(self, file_id: int, name: str) -> Optional[bytes]:
        rec = self.attributes(file_id).get(name)
        if rec is None:
            return None
        if isinstance(rec, AttributeInlineData):
            return rec.data
        if isinstance(rec, AttributeForkData):
            return extract_fork_data(self.image_path, self.vh.block_size, rec.fork, self.partition_offset,
                                     file_id=file_id, fork_type=DATA_FORK_TYPE)
        return None

    # -- content ----------------------------------------------------------
    def read_fork(self, record: CatalogFileRecord, resource: bool = False) -> bytes:
        fork = record.resource_fork if resource else record.data_fork
        return extract_fork_data(
            self.image_path, self.vh.block_size, fork, self.partition_offset,
            file_id=record.file_id, fork_type=RESOURCE_FORK_TYPE if resource else DATA_FORK_TYPE,
            extents_overflow_tree=self.extents_overflow_tree,
        )

    def is_compressed(self, record: CatalogFileRecord) -> bool:
        return bool(record.permissions.owner_flags & UF_COMPRESSED)

    def read_file(self, record: CatalogFileRecord, resource: bool = False) -> bytes:
        """Full logical content of a file's data fork (transparently decompressed) or resource fork."""
        rec = self.resolve(record)
        if resource:
            return self.read_fork(rec, resource=True)
        if self.is_compressed(rec):
            header = self.attribute_bytes(rec.file_id, DECMPFS_ATTR)
            if header is not None:
                return decmpfs.decompress(header, lambda: self.read_fork(rec, resource=True))
        return self.read_fork(rec)

    def read_deleted_file(self, record: CatalogFileRecord, decmpfs_header: Optional[bytes] = None) -> bytes:
        """Content of a deleted file from its recovered Catalog record. Compressed files have an
        empty data fork: their bytes are in the (recovered) com.apple.decmpfs attribute and/or the
        resource fork. Does not rely on the UF_COMPRESSED flag, since journal snapshots can be
        taken before it was set."""
        if record.data_fork.logical_size > 0:
            return self.read_fork(record)
        rsrc = lambda: self.read_fork(record, resource=True)  # noqa: E731
        if decmpfs_header is not None:
            return decmpfs.decompress(decmpfs_header, rsrc)
        if record.resource_fork.logical_size > 0:
            return decmpfs.decompress_resource_fork_only(rsrc())
        return b""

    def file_size(self, record: CatalogFileRecord) -> int:
        """Logical size as the OS reports it (uncompressed size for compressed files)."""
        rec = self.resolve(record)
        if self.is_compressed(rec):
            header = self.attribute_bytes(rec.file_id, DECMPFS_ATTR)
            if header is not None:
                return decmpfs.uncompressed_size(header)
        return rec.data_fork.logical_size


def open_volume(image_path: str) -> Volume:
    return Volume(image_path)
