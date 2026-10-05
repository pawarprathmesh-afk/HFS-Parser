"""
Deleted file recovery for HFS+.

Technique: when a Catalog B-tree leaf node is deleted from the live
tree, HFS+ only updates the tree's structure (unlinking the node and
marking it free in the B-tree map record) - it does NOT zero the
node's actual bytes. So a deleted file's Catalog record can often
still be read directly out of that now-"free" node, as long as
nothing has reused and overwritten it yet.

Approach:
  1. Walk the LIVE Catalog tree normally (via fLink from the header's
     first_leaf_node) to build the set of node numbers currently in
     use, and the set of file_ids currently visible in the live tree.
  2. Scan EVERY node in the Catalog fork (0 .. total_nodes-1), not
     just the live ones. For any node whose descriptor still says
     kind=LEAF but which ISN'T in the live set, attempt to parse its
     records as normal Catalog leaf records.
  3. Keep only FILE records whose file_id isn't already visible in
     the live tree (i.e. genuinely deleted, not just found via a
     second reference).
  4. For each recovered file, cross-check its data fork's extents
     against the Allocation File bitmap: if every block is still free,
     the content is very likely intact and extractable; if any block
     is now allocated, something else has overwritten at least part
     of it.

This only recovers files whose Catalog leaf node hasn't yet been
reused for something else - it cannot recover files whose node has
already been overwritten, and it cannot recover files at all if the
volume has no leftover freed nodes to find (e.g. right after a fresh
reformat, or if the B-tree happened to compact/reuse that exact node
already). This is a real, inherent limitation of this technique, not
a bug - the same limitation applies to real forensic tools using the
same approach.
"""

from dataclasses import dataclass
from typing import List, Optional

from btree import BTNodeKind, BTNodeDescriptor
from catalog import parse_catalog_leaf_record, CatalogKey, CatalogFileRecord
from allocation_file import AllocationFile
from journal import (read_journal_info, read_journal_bytes, scan_for_file_records,
                     scan_for_decmpfs_attributes)


@dataclass
class RecoveredFile:
    key: CatalogKey
    record: CatalogFileRecord
    node_number: int
    fully_free: bool          # True if ALL data blocks are still unallocated
    blocks_checked: int
    blocks_free: int
    source: str = "orphan-node"   # "orphan-node", "catalog-slack" or "journal"
    decmpfs: Optional[bytes] = None   # recovered com.apple.decmpfs attribute, for compressed files


class DeletedFileScanner:
    def __init__(self, catalog_btree, allocation_file: Optional[AllocationFile] = None,
                 image_path: Optional[str] = None, volume_header=None, partition_offset: int = 0):
        """image_path + volume_header enable the journal scan (journaled volumes)."""
        self.btree = catalog_btree
        self.allocation_file = allocation_file
        self.image_path = image_path
        self.vh = volume_header
        self.partition_offset = partition_offset

    def _live_node_numbers(self) -> set:
        return {node_number for node_number, _, _ in self.btree.iter_leaf_nodes()}

    def _live_file_ids(self) -> set:
        ids = set()
        for node_number, descriptor, records in self.btree.iter_leaf_nodes():
            for raw in records:
                try:
                    key, record = parse_catalog_leaf_record(raw)
                except Exception:
                    continue
                if isinstance(record, CatalogFileRecord):
                    ids.add(record.file_id)
        return ids

    def _check_extent_freedom(self, record: CatalogFileRecord):
        """Returns (blocks_checked, blocks_free). If no AllocationFile was
        given, returns (0, 0) and callers should treat freedom as unknown."""
        if self.allocation_file is None:
            return 0, 0
        checked = 0
        free = 0
        # compressed files keep their content in the resource fork, so check both forks
        extents = list(record.data_fork.active_extents()) + list(record.resource_fork.active_extents())
        for ext in extents:
            for b in range(ext.start_block, ext.start_block + ext.block_count):
                if b >= self.allocation_file.total_blocks:
                    continue
                checked += 1
                if not self.allocation_file.is_allocated(b):
                    free += 1
        return checked, free

    def _scan_orphan_nodes(self, live_file_ids: set) -> List[RecoveredFile]:
        live_nodes = self._live_node_numbers()

        recovered = []
        total_nodes = self.btree.header.total_nodes

        for node_number in range(total_nodes):
            if node_number in live_nodes:
                continue
            if node_number == 0:
                continue  # header node, never a candidate

            try:
                raw = self.btree.read_node_raw(node_number)
                descriptor = BTNodeDescriptor.parse(raw[0:14])
            except Exception:
                continue

            if descriptor.kind != BTNodeKind.LEAF:
                continue
            if descriptor.num_records == 0 or descriptor.num_records > 400:
                continue  # implausible - likely a stale/corrupt or non-leaf reinterpretation

            try:
                _, records = self.btree.read_node(node_number)
            except Exception:
                continue

            for raw_record in records:
                if len(raw_record) < 2:
                    continue
                try:
                    key, record = parse_catalog_leaf_record(raw_record)
                except Exception:
                    continue

                if not isinstance(record, CatalogFileRecord):
                    continue
                if record.file_id in live_file_ids:
                    continue  # already visible in the live tree, not actually deleted

                checked, free = self._check_extent_freedom(record)
                fully_free = (checked > 0 and free == checked)
                recovered.append(RecoveredFile(
                    key=key, record=record, node_number=node_number,
                    fully_free=fully_free, blocks_checked=checked, blocks_free=free,
                ))

        return recovered

    def _make(self, key, record, node_number, source) -> RecoveredFile:
        checked, free = self._check_extent_freedom(record)
        return RecoveredFile(
            key=key, record=record, node_number=node_number,
            fully_free=(checked > 0 and free == checked),
            blocks_checked=checked, blocks_free=free, source=source,
        )

    def _scan_raw(self, buf: bytes, total_blocks: int, source: str, live_file_ids: set):
        found = []
        for offset, key, record in scan_for_file_records(buf, total_blocks):
            if record.file_id in live_file_ids:
                continue
            found.append(self._make(key, record, offset, source))
        return found

    def _scan_catalog_slack(self, live_file_ids: set) -> List[RecoveredFile]:
        """Raw-scan the whole Catalog fork: finds records in node slack space."""
        if self.vh is None:
            return []
        size = self.btree.header.total_nodes * self.btree.node_size
        try:
            buf = self.btree.fork_reader.read(0, size)
        except Exception:
            return []
        return self._scan_raw(buf, self.vh.total_blocks, "catalog-slack", live_file_ids)

    def _journal_bytes(self) -> bytes:
        if self.vh is None or self.image_path is None:
            return b""
        info = read_journal_info(self.image_path, self.vh, self.partition_offset)
        if info is None or not info.in_volume:
            return b""
        return read_journal_bytes(self.image_path, info, self.partition_offset)

    def _scan_journal(self, live_file_ids: set, buf: bytes) -> List[RecoveredFile]:
        if not buf:
            return []
        return self._scan_raw(buf, self.vh.total_blocks, "journal", live_file_ids)

    def _recover_decmpfs(self, journal_buf: bytes) -> dict:
        """decmpfs attribute values for files whose Attributes records are gone (journal and
        stale space inside the Attributes file)."""
        found = scan_for_decmpfs_attributes(journal_buf) if journal_buf else {}
        if self.vh is not None and self.image_path is not None and self.vh.attributes_file.logical_size:
            from fork_reader import ForkReader
            try:
                reader = ForkReader(self.image_path, self.vh.block_size,
                                    self.vh.attributes_file.active_extents(), self.partition_offset)
                raw = reader.read(0, self.vh.attributes_file.logical_size)
                for fid, val in scan_for_decmpfs_attributes(raw).items():
                    if fid not in found or len(val) > len(found[fid]):
                        found[fid] = val
            except Exception:  # noqa: BLE001
                pass
        return found

    def scan(self) -> List[RecoveredFile]:
        """Orphaned nodes + catalog slack + journal, de-duplicated by file_id.
        When the same file_id turns up more than once, the copy with the
        newest modification date (then the most blocks) wins."""
        live_file_ids = self._live_file_ids()
        journal_buf = self._journal_bytes()
        candidates = (
            self._scan_orphan_nodes(live_file_ids)
            + self._scan_catalog_slack(live_file_ids)
            + self._scan_journal(live_file_ids, journal_buf)
        )
        best = {}
        for r in candidates:
            fid = r.record.file_id
            rank = (r.record.content_mod_date, r.record.data_fork.total_blocks)
            if fid not in best or rank > (best[fid].record.content_mod_date,
                                          best[fid].record.data_fork.total_blocks):
                best[fid] = r
        results = sorted(best.values(), key=lambda r: r.record.file_id)
        attrs = self._recover_decmpfs(journal_buf)
        for r in results:
            r.decmpfs = attrs.get(r.record.file_id)
            if r.blocks_checked == 0 and r.decmpfs is not None:
                r.fully_free = True       # content lives in the attribute, not in data blocks
        return results
