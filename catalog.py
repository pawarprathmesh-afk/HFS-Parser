"""
HFS+ Catalog B-tree key and record parsing.

Reference: TN1150, "The Catalog File".

The Catalog B-tree's leaf records come in 4 flavors, distinguished by
a signed 16-bit `recordType` at the START of the record data (right
after the key):

    1 = kHFSPlusFolderRecord
    2 = kHFSPlusFileRecord
    3 = kHFSPlusFolderThreadRecord
    4 = kHFSPlusFileThreadRecord

Every leaf record = [ CatalogKey ][ record data, one of the 4 above ]
The key's own length is self-describing (keyLength field), so we can
always find where the key ends and the record data begins.
"""

import struct
from dataclasses import dataclass
from enum import IntEnum

HFS_EPOCH_OFFSET = 2082844800  # 1904 -> 1970


def hfs_time_to_unix(hfs_timestamp: int) -> int:
    if hfs_timestamp == 0:
        return 0
    return hfs_timestamp - HFS_EPOCH_OFFSET


class CatalogRecordType(IntEnum):
    FOLDER = 1
    FILE = 2
    FOLDER_THREAD = 3
    FILE_THREAD = 4


ROOT_FOLDER_ID = 2  # CNID 2 is always the volume's root folder, per spec


@dataclass
class CatalogKey:
    parent_id: int
    node_name: str  # decoded from UTF-16BE
    total_length: int  # bytes consumed by the whole key, including the keyLength field itself

    @classmethod
    def parse(cls, raw: bytes) -> "CatalogKey":
        (key_length,) = struct.unpack(">H", raw[0:2])
        # key_length counts everything AFTER the keyLength field itself
        parent_id, name_length = struct.unpack(">IH", raw[2:8])

        name_bytes = raw[8:8 + name_length * 2]
        node_name = name_bytes.decode("utf-16-be", errors="replace")

        total_length = 2 + key_length  # +2 for the keyLength field itself
        return cls(parent_id=parent_id, node_name=node_name, total_length=total_length)


@dataclass
class HFSPlusBSDInfo:
    owner_id: int
    group_id: int
    admin_flags: int
    owner_flags: int
    file_mode: int

    SIZE = 16

    @classmethod
    def parse(cls, data: bytes) -> "HFSPlusBSDInfo":
        owner_id, group_id, admin_flags, owner_flags, file_mode = \
            struct.unpack(">IIBBH", data[0:12])
        # remaining 4 bytes are a union (special device / link count) - not needed for basic parsing
        return cls(owner_id, group_id, admin_flags, owner_flags, file_mode)


@dataclass
class CatalogFolderRecord:
    flags: int
    valence: int  # number of items directly inside this folder
    folder_id: int
    create_date: int
    content_mod_date: int
    attribute_mod_date: int
    access_date: int
    backup_date: int
    permissions: HFSPlusBSDInfo
    text_encoding: int

    SIZE = 88

    @classmethod
    def parse(cls, data: bytes) -> "CatalogFolderRecord":
        record_type, flags, valence, folder_id = struct.unpack(">hHII", data[0:12])
        assert record_type == CatalogRecordType.FOLDER
        create_date, content_mod_date, attribute_mod_date, access_date, backup_date = \
            struct.unpack(">IIIII", data[12:32])
        permissions = HFSPlusBSDInfo.parse(data[32:48])
        # userInfo (16 bytes) + finderInfo (16 bytes) skipped for now - Finder-specific, rarely needed
        (text_encoding,) = struct.unpack(">I", data[80:84])
        return cls(
            flags=flags, valence=valence, folder_id=folder_id,
            create_date=create_date, content_mod_date=content_mod_date,
            attribute_mod_date=attribute_mod_date, access_date=access_date,
            backup_date=backup_date, permissions=permissions, text_encoding=text_encoding,
        )


@dataclass
class HFSPlusExtentDescriptorMini:
    start_block: int
    block_count: int


@dataclass
class HFSPlusForkDataMini:
    logical_size: int
    clump_size: int
    total_blocks: int
    extents: list

    SIZE = 80

    @classmethod
    def parse(cls, data: bytes) -> "HFSPlusForkDataMini":
        logical_size, clump_size, total_blocks = struct.unpack(">QII", data[0:16])
        extents = []
        offset = 16
        for _ in range(8):
            start_block, block_count = struct.unpack(">II", data[offset:offset + 8])
            extents.append(HFSPlusExtentDescriptorMini(start_block, block_count))
            offset += 8
        return cls(logical_size, clump_size, total_blocks, extents)

    def active_extents(self):
        return [e for e in self.extents if not (e.start_block == 0 and e.block_count == 0)]


@dataclass
class CatalogFileRecord:
    flags: int
    file_id: int
    create_date: int
    content_mod_date: int
    attribute_mod_date: int
    access_date: int
    backup_date: int
    permissions: HFSPlusBSDInfo
    text_encoding: int
    data_fork: HFSPlusForkDataMini
    resource_fork: HFSPlusForkDataMini
    file_type: bytes = b"\0\0\0\0"     # Finder fileType; b"hlnk" marks a hard link
    creator: bytes = b"\0\0\0\0"       # Finder creator;  b"hfs+" marks a hard link
    special: int = 0                      # BSD union: iNodeNum (hard link) or link count (inode)

    SIZE = 248

    @classmethod
    def parse(cls, data: bytes) -> "CatalogFileRecord":
        record_type, flags, _reserved1, file_id = struct.unpack(">hHII", data[0:12])
        assert record_type == CatalogRecordType.FILE
        create_date, content_mod_date, attribute_mod_date, access_date, backup_date = \
            struct.unpack(">IIIII", data[12:32])
        permissions = HFSPlusBSDInfo.parse(data[32:48])
        # userInfo (16) + finderInfo (16) skipped, same as folder record
        file_type, creator = data[48:52], data[52:56]
        (special,) = struct.unpack(">I", data[44:48])
        (text_encoding,) = struct.unpack(">I", data[80:84])
        # bytes 84:88 = reserved2
        data_fork = HFSPlusForkDataMini.parse(data[88:168])
        resource_fork = HFSPlusForkDataMini.parse(data[168:248])
        return cls(
            flags=flags, file_id=file_id,
            create_date=create_date, content_mod_date=content_mod_date,
            attribute_mod_date=attribute_mod_date, access_date=access_date,
            backup_date=backup_date, permissions=permissions, text_encoding=text_encoding,
            data_fork=data_fork, resource_fork=resource_fork,
            file_type=file_type, creator=creator, special=special,
        )

    @property
    def is_hard_link(self) -> bool:
        return self.file_type == b"hlnk" and self.creator == b"hfs+"


@dataclass
class CatalogThreadRecord:
    is_folder_thread: bool
    parent_id: int
    node_name: str

    @classmethod
    def parse(cls, data: bytes) -> "CatalogThreadRecord":
        record_type, _reserved = struct.unpack(">hh", data[0:4])
        assert record_type in (CatalogRecordType.FOLDER_THREAD, CatalogRecordType.FILE_THREAD)
        (parent_id,) = struct.unpack(">I", data[4:8])
        (name_length,) = struct.unpack(">H", data[8:10])
        name_bytes = data[10:10 + name_length * 2]
        node_name = name_bytes.decode("utf-16-be", errors="replace")
        return cls(
            is_folder_thread=(record_type == CatalogRecordType.FOLDER_THREAD),
            parent_id=parent_id,
            node_name=node_name,
        )


def parse_catalog_leaf_record(raw: bytes):
    """
    raw = one full leaf record's bytes, as returned by BTree.read_node().
    Returns (CatalogKey, record) where record is one of:
        CatalogFolderRecord, CatalogFileRecord, CatalogThreadRecord
    """
    key = CatalogKey.parse(raw)
    record_data = raw[key.total_length:]

    (record_type_raw,) = struct.unpack(">h", record_data[0:2])

    if record_type_raw == CatalogRecordType.FOLDER:
        record = CatalogFolderRecord.parse(record_data)
    elif record_type_raw == CatalogRecordType.FILE:
        record = CatalogFileRecord.parse(record_data)
    elif record_type_raw in (CatalogRecordType.FOLDER_THREAD, CatalogRecordType.FILE_THREAD):
        record = CatalogThreadRecord.parse(record_data)
    else:
        raise ValueError(f"Unknown catalog record type: {record_type_raw}")

    return key, record


class CatalogTree:
    """
    Wraps a BTree (already opened over the Catalog fork) and provides
    filesystem-tree-shaped access: listing children, resolving paths.
    """

    def __init__(self, btree):
        self.btree = btree
        self._all_records = None  # lazy cache: list of (CatalogKey, record)
        self._by_parent = None    # parent_id -> [(CatalogKey, record)]
        self._by_key = None       # (parent_id, name) -> record

    def _load_all_records(self):
        if self._all_records is not None:
            return
        all_records = []
        self.skipped_records = 0      # malformed records are skipped, not fatal
        for node_number, descriptor, raw_records in self.btree.iter_leaf_nodes():
            for raw in raw_records:
                try:
                    key, record = parse_catalog_leaf_record(raw)
                except Exception:  # noqa: BLE001 - corrupt/unknown record
                    self.skipped_records += 1
                    continue
                all_records.append((key, record))
        self._all_records = all_records
        self._by_parent = {}
        self._by_key = {}
        for key, record in all_records:
            self._by_parent.setdefault(key.parent_id, []).append((key, record))
            self._by_key[(key.parent_id, key.node_name)] = record

    def lookup(self, parent_id: int, name: str):
        """Exact (parent_id, name) lookup; None if absent."""
        self._load_all_records()
        return self._by_key.get((parent_id, name))

    def list_children(self, parent_id: int):
        """
        Return [(name, record)] for all direct children of parent_id.
        Only folder/file records are returned (thread records are used
        internally for path resolution, not exposed here).
        """
        self._load_all_records()
        children = []
        for key, record in self._by_parent.get(parent_id, ()):
            if not isinstance(record, CatalogThreadRecord):
                children.append((key.node_name, record))
        return children

    def walk(self, parent_id: int = ROOT_FOLDER_ID, path_prefix: str = ""):
        """
        Yield (full_path, record) for every file and folder under parent_id (defaults to the
        volume root, CNID 2), depth-first. Iterative, and each folder is visited once, so
        corrupt (cyclic) or very deep trees cannot hang or overflow the stack.
        """
        visited = {parent_id}
        # explicit stack of (iterator over children, path prefix)
        stack = [(iter(self.list_children(parent_id)), path_prefix)]
        while stack:
            children, prefix = stack[-1]
            try:
                name, record = next(children)
            except StopIteration:
                stack.pop()
                continue
            full_path = f"{prefix}/{name}"
            yield full_path, record
            if isinstance(record, CatalogFolderRecord) and record.folder_id not in visited:
                visited.add(record.folder_id)
                stack.append((iter(self.list_children(record.folder_id)), full_path))

    def all_records_raw(self):
        """Expose the flat (key, record) list for debugging/inspection."""
        self._load_all_records()
        return self._all_records
