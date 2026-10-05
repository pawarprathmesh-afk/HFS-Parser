"""
HFS+/HFSX Parser - Graphical Interface

Toolbar:  Evidence Image picker, Output Folder picker, operation buttons
          (Info / List / Extract / Recover / Carve / Verify), Run.
Left:     grouped, filterable result tree.
Right:    details for the selected result (double-click extracts/opens it).

Operations:
    Info     volume header summary
    List     live files and folders from the Catalog
    Extract  extract live files
    Recover  deleted files with names (orphaned nodes, catalog slack, journal)
    Carve    deleted file contents from free space (no names)
    Verify   B-tree and allocation consistency checks
"""

import os
import sys
import subprocess
import hashlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
    QScrollArea, QTextEdit, QLineEdit, QFileDialog, QProgressBar,
    QStatusBar, QFrame, QMessageBox,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QFont

from volume_header import hfs_time_to_unix
from fork_reader import ForkReader
from btree import BTree
from catalog import CatalogFileRecord, CatalogFolderRecord, ROOT_FOLDER_ID
from allocation_file import AllocationFile
from recovery import DeletedFileScanner
from carver import carve_free_space
from volume import Volume

# ---------------------------------------------------------------------------
# Theme (identical palette/style to the reference APFS-Parser GUI)
# ---------------------------------------------------------------------------

BG_DARK = "#1e1e1e"
BG_PANEL = "#252526"
BG_HOVER = "#2a2d2e"
BG_SELECT = "#0c2840"
FG_PRIMARY = "#ffffff"
FG_DIM = "#d0d0d0"
FG_FAINT = "#a8a8a8"
BORDER = "#3c3c3c"
STATUS_BAR = "#007acc"

COL_GREEN = "#4ec9b0"
COL_AMBER = "#dca800"
COL_RED = "#f44747"
COL_BLUE = "#569cd6"
COL_ORANGE = "#ce9178"
COL_PURPLE = "#b267e6"
COL_CYAN = "#9cdcfe"

DARK_QSS = f"""
QMainWindow, QWidget                {{ background:{BG_DARK}; color:{FG_PRIMARY};
                                       font-family:'Segoe UI',Arial,sans-serif;
                                       font-size:14px; font-weight:600; }}
QSplitter::handle                   {{ background:{BORDER}; }}
QScrollBar:vertical                 {{ background:{BG_DARK}; width:10px; border:none; }}
QScrollBar::handle:vertical         {{ background:#3c3c3c; border-radius:5px; min-height:28px; }}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical       {{ height:0; }}
QTreeWidget                         {{ background:{BG_PANEL}; border:none; outline:none; }}
QTreeWidget::item                   {{ padding:8px 4px; font-size:14px; font-weight:600; }}
QTreeWidget::item:selected          {{ background:{BG_SELECT}; }}
QTreeWidget::item:hover             {{ background:{BG_HOVER}; }}
QTextEdit                           {{ background:{BG_DARK}; border:none;
                                       font-family:'Segoe UI',Arial,sans-serif; font-size:13px; }}
QLineEdit                           {{ background:#1a1a1a; color:{COL_CYAN}; border:1px solid {BORDER};
                                       padding:6px 12px; border-radius:4px;
                                       font-family:'Segoe UI',Arial,sans-serif; font-size:14px;
                                       font-weight:600; }}
QProgressBar                        {{ background:#181818; border:none; max-height:10px;
                                       border-radius:5px; }}
QProgressBar::chunk                 {{ background:{COL_AMBER}; border-radius:5px; }}
QStatusBar                          {{ background:{STATUS_BAR}; color:white;
                                       font-family:'Segoe UI',Arial,sans-serif; font-size:13px;
                                       font-weight:600; }}
QPushButton                         {{ background:#0e639c; color:white; border:none;
                                       padding:8px 16px; border-radius:5px;
                                       font-family:'Segoe UI',Arial,sans-serif; font-size:14px;
                                       font-weight:700; }}
QPushButton:hover                   {{ background:#1177bb; }}
QPushButton:disabled                {{ background:#2a2a2a; color:{FG_FAINT}; }}
QPushButton#btn_start               {{ background:#0d5a2e; color:{COL_GREEN};
                                       font-weight:bold; font-size:15px; }}
QPushButton#btn_start:hover         {{ background:#0f6e38; }}
QPushButton#btn_start:disabled      {{ background:#1a2a1a; color:#3a5a3a; }}
"""

STATUS_COLORS = {
    "ok": COL_GREEN, "pass": COL_GREEN, "allocated": COL_GREEN, "clean": COL_GREEN,
    "warn": COL_AMBER, "unallocated": COL_AMBER, "fragmented": COL_AMBER,
    "fail": COL_RED, "error": COL_RED,
    "info": COL_BLUE, "unknown": FG_DIM,
}


def human_size(n: int) -> str:
    step = 1024.0
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    v = float(n)
    for u in units:
        if v < step or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} {u}"
        v /= step
    return f"{n} B"


def safe_display(name: str) -> str:
    return "".join(c if (c.isprintable() or c == " ") else repr(c)[1:-1] for c in name)


def format_hfs_time(hfs_ts: int) -> str:
    import datetime
    unix_ts = hfs_time_to_unix(hfs_ts)
    if unix_ts == 0:
        return "-"
    return datetime.datetime.fromtimestamp(unix_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class ResultItem:
    label: str
    group: str
    status: str = "info"
    badge: str = ""
    sections: List[Tuple[str, List[Tuple[str, str, str]]]] = field(default_factory=list)
    full_path: str = ""             # already on disk -> double-click opens it
    extract_fn: object = None       # callable() -> bytes, for lazy extract-then-open
    rel_path: str = ""              # where to save if extract_fn is used

    @property
    def color(self) -> str:
        return STATUS_COLORS.get(self.status.lower(), FG_DIM)

    def to_html(self) -> str:
        def sec(t):
            return (
                f'<p style="font-family:\'Segoe UI\',Arial;font-size:14px;font-weight:800;'
                f'color:{COL_BLUE};letter-spacing:2px;text-transform:uppercase;'
                f'border-bottom:1px solid #2a2a2a;padding-bottom:5px;margin:16px 0 8px;">'
                f'{t}</p>'
            )

        def row(k, v, c):
            return (
                f'<tr><td style="color:{FG_DIM};font-size:13px;font-weight:700;'
                f'padding:4px 14px 4px 0;white-space:nowrap;vertical-align:top;'
                f'width:150px;">{k}</td>'
                f'<td style="color:{c};font-size:13px;font-weight:600;padding:4px 0;'
                f'word-break:break-all;line-height:1.5;">{v}</td></tr>'
            )

        body = "".join(
            sec(name) + '<table style="width:100%;border-collapse:collapse;">'
            + "".join(row(k, v, c) for k, v, c in rows) + "</table>"
            for name, rows in self.sections
        )
        return (
            f'<html><body style="background:{BG_PANEL};color:{FG_PRIMARY};'
            f'font-family:\'Segoe UI\',Arial,sans-serif;font-size:13px;margin:16px 18px;">'
            f'{body}</body></html>'
        )


FILTER_TABS: Dict[str, List[str]] = {
    "info": ["All"],
    "list": ["All", "Files", "Directories", "Fragmented"],
    "recover": ["All", "Unallocated", "Allocated"],
    "carve": ["All"],
    "verify": ["All", "Pass", "Warn", "Fail"],
    "extract": ["All", "Ok", "Failed"],
}

OPERATIONS = [
    ("info", "Info"), ("list", "List"), ("extract", "Extract"),
    ("recover", "Recover"), ("carve", "Carve"), ("verify", "Verify"),
]


# ---------------------------------------------------------------------------
# Shared helper: open a volume and everything needed to walk/extract it.
# ---------------------------------------------------------------------------

class OpenVolume(Volume):
    """A Volume plus the helpers the GUI needs."""

    def extract_fn_for(self, record: CatalogFileRecord) -> Callable[[], bytes]:
        return lambda: self.read_file(record)


# ---------------------------------------------------------------------------
# Backend: each function returns (items, summary) for one operation.
# These call the existing parser modules only - no parsing logic is
# duplicated here.
# ---------------------------------------------------------------------------

def run_info(image_path: str, _outdir: str) -> Tuple[List[ResultItem], dict]:
    items: List[ResultItem] = []
    ov = OpenVolume(image_path)
    image_path = ov.image_path      # converted raw copy for non-raw containers
    p, vh = ov.part_info, ov.vh

    items.append(ResultItem(
        label=f"{p.scheme.upper()} @ offset {p.start_offset}" if p.scheme != "none" else "No partition table",
        group="Partition",
        status="ok",
        sections=[("Partition", [
            ("Scheme", p.scheme, FG_PRIMARY),
            ("Start offset", f"{p.start_offset} (0x{p.start_offset:x})", COL_CYAN),
            ("Size", human_size(p.size_bytes) if p.size_bytes else "-", FG_DIM),
            ("Label", p.label or "-", FG_DIM),
        ])],
    ))

    sig_ok = vh.signature in ("H+", "HX")
    items.append(ResultItem(
        label=f"Volume Header  ({'HFSX' if vh.is_hfsx else 'HFS+'}, v{vh.version})",
        group="Volume Header",
        status="ok" if sig_ok else "fail",
        sections=[("Volume Header", [
            ("Signature", vh.signature, FG_PRIMARY if sig_ok else COL_RED),
            ("Journaled", str(vh.is_journaled), FG_DIM),
            ("Journal state",
             (f"{ov.journal_pending_blocks} pending block(s) replayed in memory (volume was not cleanly unmounted)"
              if ov.journal_pending_blocks else
              f"journal problem: {ov.journal_problem}" if ov.journal_problem else "clean"), FG_DIM),
            ("Header read from", ov.header_source + (" (primary header damaged)" if ov.header_source == "alternate" else ""), FG_DIM),
            ("Block size", str(vh.block_size), FG_PRIMARY),
            ("Total blocks", f"{vh.total_blocks} ({human_size(vh.total_blocks * vh.block_size)})", FG_PRIMARY),
            ("Free blocks", str(vh.free_blocks), FG_PRIMARY),
            ("File count", str(vh.file_count), FG_DIM),
            ("Folder count", str(vh.folder_count), FG_DIM),
            ("Next Catalog ID", str(vh.next_catalog_id), FG_DIM),
            ("Created", format_hfs_time(vh.create_date), COL_CYAN),
            ("Modified", format_hfs_time(vh.modify_date), COL_CYAN),
        ])],
    ))

    af = AllocationFile(
        image_path=image_path, block_size=vh.block_size,
        extents=vh.allocation_file.active_extents(),
        total_blocks=vh.total_blocks, partition_offset=ov.partition_offset,
    )
    match = af.count_free() == vh.free_blocks
    ranges = list(af.free_ranges())
    items.append(ResultItem(
        label=f"Allocation File  ({af.count_free()} free / {vh.total_blocks} total)",
        group="Allocation",
        status="ok" if match else "warn",
        sections=[("Allocation Bitmap", [
            ("Allocated blocks", str(af.count_allocated()), FG_PRIMARY),
            ("Free blocks", str(af.count_free()), FG_PRIMARY),
            ("Free space runs", str(len(ranges)), FG_DIM),
            ("Largest free run", f"{max((r[1] for r in ranges), default=0)} blocks", FG_DIM),
            ("Cross-check vs Volume Header", "MATCH" if match else "MISMATCH",
             COL_GREEN if match else COL_RED),
        ])],
    ))

    if vh.attributes_file.logical_size > 0:
        attr_reader = ForkReader(
            image_path=image_path, block_size=vh.block_size,
            extents=vh.attributes_file.active_extents(), partition_offset=ov.partition_offset,
        )
        attrs_btree = BTree(attr_reader)
        n_attrs = attrs_btree.header.leaf_records
        items.append(ResultItem(
            label=f"Extended Attributes  ({n_attrs} record(s))",
            group="Extended Attributes",
            status="info",
            sections=[("Attributes File", [
                ("Leaf records", str(n_attrs), FG_PRIMARY),
                ("Node size", str(attrs_btree.header.node_size), FG_DIM),
            ])],
        ))
    else:
        items.append(ResultItem(
            label="No extended attributes on this volume",
            group="Extended Attributes", status="info",
        ))

    summary = {"total": len(items)}
    return items, summary


def run_list(image_path: str, outdir: str) -> Tuple[List[ResultItem], dict]:
    items: List[ResultItem] = []
    ov = OpenVolume(image_path)
    image_path = ov.image_path      # converted raw copy for non-raw containers

    for full_path, record in ov.catalog.walk(ROOT_FOLDER_ID):
        display_path = safe_display(full_path)
        if isinstance(record, CatalogFolderRecord):
            items.append(ResultItem(
                label=f"{display_path}/",
                group="Directories",
                status="info",
                sections=[("Folder", [
                    ("Path", display_path, FG_PRIMARY),
                    ("Folder ID", str(record.folder_id), FG_DIM),
                    ("Valence (item count)", str(record.valence), FG_PRIMARY),
                    ("Created", format_hfs_time(record.create_date), COL_CYAN),
                    ("Content modified", format_hfs_time(record.content_mod_date), COL_CYAN),
                ])],
            ))
        elif isinstance(record, CatalogFileRecord):
            try:
                target = ov.resolve(record)
            except Exception:
                target = record      # orphaned hard link: show the stub itself
            covered = sum(e.block_count for e in target.data_fork.active_extents())
            fragmented = covered < target.data_fork.total_blocks
            size_s = human_size(ov.file_size(target))
            items.append(ResultItem(
                label=f"{display_path}  ({size_s})",
                group="Fragmented Files" if fragmented else "Files",
                status="fragmented" if fragmented else "ok",
                badge=" [frag]" if fragmented else "",
                sections=[("File", [
                    ("Path", display_path, FG_PRIMARY),
                    ("File ID", str(record.file_id), FG_DIM),
                    ("Size", size_s, FG_PRIMARY),
                    ("Kind", "hard link" if record.is_hard_link else
                     "compressed (decmpfs)" if ov.is_compressed(target) else "regular", FG_DIM),
                    ("Total blocks", str(target.data_fork.total_blocks), FG_DIM),
                    ("Extents (initial 8)", str(len(target.data_fork.active_extents())), FG_DIM),
                    ("Resource fork", human_size(target.resource_fork.logical_size), FG_DIM),
                    ("Fragmented (needs overflow)", str(fragmented),
                     COL_AMBER if fragmented else COL_GREEN),
                    ("Modified", format_hfs_time(record.content_mod_date), COL_CYAN),
                ])],
                extract_fn=ov.extract_fn_for(record),
                rel_path=display_path.lstrip("/"),
            ))

    summary = {"total": len(items),
               "fragmented": sum(1 for i in items if i.status == "fragmented")}
    return items, summary


def run_extract(image_path: str, outdir: str) -> Tuple[List[ResultItem], dict]:
    items: List[ResultItem] = []
    ov = OpenVolume(image_path)
    image_path = ov.image_path      # converted raw copy for non-raw containers
    outdir = outdir or os.path.expanduser("~")
    os.makedirs(outdir, exist_ok=True)

    for full_path, record in ov.catalog.walk(ROOT_FOLDER_ID):
        if not isinstance(record, CatalogFileRecord):
            continue
        display_path = safe_display(full_path)

        try:
            data = ov.read_file(record)
            ok, err = True, None
        except Exception as e:  # noqa: BLE001 - report per file, keep going
            data, ok, err = b"", False, f"{type(e).__name__}: {e}"

        full_out_path = ""
        if ok:
            rel = display_path.lstrip("/")
            dest = os.path.join(outdir, rel)
            os.makedirs(os.path.dirname(dest) or outdir, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            full_out_path = dest
            try:    # uncompressed resource forks are saved next to the file as <name>.rsrc
                target = ov.resolve(record)
                if target.resource_fork.logical_size and not ov.is_compressed(target):
                    with open(dest + ".rsrc", "wb") as f:
                        f.write(ov.read_file(record, resource=True))
            except Exception:  # noqa: BLE001
                pass

        md5 = hashlib.md5(data).hexdigest() if ok else "-"
        items.append(ResultItem(
            label=f"{display_path}  ({human_size(len(data))})",
            group="Extraction",
            status="ok" if ok else "fail",
            sections=[("Extraction", [
                ("Path", display_path, FG_PRIMARY),
                ("Result", "OK" if ok else f"ERROR: {err}", COL_GREEN if ok else COL_RED),
                ("Size", human_size(len(data)), FG_PRIMARY),
                ("MD5", md5, COL_CYAN),
                ("Saved to", full_out_path or "-", FG_DIM),
            ])],
            full_path=full_out_path,
        ))

    summary = {"total": len(items), "ok": sum(1 for i in items if i.status == "ok")}
    return items, summary


def run_verify(image_path: str, _outdir: str) -> Tuple[List[ResultItem], dict]:
    items: List[ResultItem] = []
    ov = OpenVolume(image_path)
    image_path = ov.image_path      # converted raw copy for non-raw containers
    vh = ov.vh

    sig_ok = vh.signature in ("H+", "HX")
    items.append(ResultItem(
        label="Volume Header signature", group="PASS" if sig_ok else "FAIL",
        status="pass" if sig_ok else "fail",
        sections=[("Check", [("Name", "Volume Header signature", FG_PRIMARY),
                              ("Detail", f"signature={vh.signature!r}", FG_DIM)])],
    ))

    def check_btree(name, btree):
        total = 0
        zero_len = 0
        for _, _, records in btree.iter_leaf_nodes():
            total += len(records)
            zero_len += sum(1 for r in records if len(r) == 0)
        count_ok = total == btree.header.leaf_records
        no_empties = zero_len == 0
        status = "pass" if (count_ok and no_empties) else "fail"
        items.append(ResultItem(
            label=f"{name} B-tree consistency", group=status.upper(), status=status,
            sections=[("Check", [
                ("Name", f"{name} B-tree consistency", FG_PRIMARY),
                ("Leaf records counted", str(total), FG_DIM),
                ("Leaf records (header)", str(btree.header.leaf_records), FG_DIM),
                ("Zero-length records", str(zero_len), COL_RED if zero_len else COL_GREEN),
            ])],
        ))

    check_btree("Catalog", ov.catalog_btree)

    if vh.extents_file.logical_size > 0:
        eo_reader = ForkReader(
            image_path=image_path, block_size=vh.block_size,
            extents=vh.extents_file.active_extents(), partition_offset=ov.partition_offset,
        )
        check_btree("Extents Overflow", BTree(eo_reader))

    if vh.attributes_file.logical_size > 0:
        attr_reader = ForkReader(
            image_path=image_path, block_size=vh.block_size,
            extents=vh.attributes_file.active_extents(), partition_offset=ov.partition_offset,
        )
        check_btree("Attributes", BTree(attr_reader))

    af = AllocationFile(
        image_path=image_path, block_size=vh.block_size,
        extents=vh.allocation_file.active_extents(),
        total_blocks=vh.total_blocks, partition_offset=ov.partition_offset,
    )
    match = af.count_free() == vh.free_blocks
    items.append(ResultItem(
        label="Allocation File free-block cross-check",
        group="PASS" if match else "FAIL", status="pass" if match else "fail",
        sections=[("Check", [
            ("Name", "Allocation File free-block cross-check", FG_PRIMARY),
            ("Bitmap free count", str(af.count_free()), FG_DIM),
            ("Volume Header free_blocks", str(vh.free_blocks), FG_DIM),
        ])],
    ))

    n_pass = sum(1 for i in items if i.status == "pass")
    n_fail = sum(1 for i in items if i.status == "fail")
    summary = {"total": len(items), "pass": n_pass, "warn": 0, "fail": n_fail}
    return items, summary


def run_recover(image_path: str, outdir: str) -> Tuple[List[ResultItem], dict]:
    items: List[ResultItem] = []
    ov = OpenVolume(image_path)
    image_path = ov.image_path      # converted raw copy for non-raw containers
    vh = ov.vh
    outdir = outdir or os.path.expanduser("~")

    af = AllocationFile(
        image_path=image_path, block_size=vh.block_size,
        extents=vh.allocation_file.active_extents(),
        total_blocks=vh.total_blocks, partition_offset=ov.partition_offset,
    )

    scanner = DeletedFileScanner(ov.catalog_btree, allocation_file=af, image_path=image_path,
                                 volume_header=vh, partition_offset=ov.partition_offset)
    recovered = scanner.scan()

    for r in recovered:
        status = "unallocated" if r.fully_free else "allocated"
        # naming convention matches this project's terminology:
        # "unallocated" (free space) = content likely intact = the interesting/recoverable case
        # "allocated" = blocks reused by something else = content likely overwritten
        name = safe_display(r.key.node_name) or f"(file_id_{r.record.file_id})"

        extract_fn = None
        if r.fully_free:
            def _make_extract(record=r.record, decmpfs=r.decmpfs):
                return lambda: ov.read_deleted_file(record, decmpfs)
            extract_fn = _make_extract()

        items.append(ResultItem(
            label=f"{name}  ({human_size(r.record.data_fork.logical_size)})",
            group="Recovered Files",
            status=status,
            badge=" ★" if status == "unallocated" else "",
            sections=[("Recovered File", [
                ("Name", name, FG_PRIMARY),
                ("File ID", str(r.record.file_id), FG_DIM),
                ("Found via", r.source, FG_DIM),
                ("Size", human_size(r.record.data_fork.logical_size), FG_PRIMARY),
                ("Data blocks checked", str(r.blocks_checked), FG_DIM),
                ("Data blocks still free", str(r.blocks_free), FG_DIM),
                ("Content likely intact",
                 "YES - not yet overwritten" if r.fully_free else "NO - blocks reused by another file",
                 COL_GREEN if r.fully_free else COL_RED),
                ("Modified (before deletion)", format_hfs_time(r.record.content_mod_date), COL_CYAN),
            ])],
            extract_fn=extract_fn,
            rel_path=f"recovered/{name}",
        ))

    if not recovered:
        items.append(ResultItem(
            label="No recoverable deleted files found",
            group="Recovered Files", status="info",
            sections=[("Note", [
                ("Detail", "No deleted file records found in orphaned catalog nodes, catalog slack "
                           "or the journal. On non-journaled volumes macOS scrubs these records; "
                           "use the Carve operation to recover file contents (without names).", FG_DIM),
            ])],
        ))

    summary = {"total": len(recovered),
               "unallocated": sum(1 for r in recovered if r.fully_free)}
    return items, summary


def run_carve(image_path: str, outdir: str) -> Tuple[List[ResultItem], dict]:
    """Content-only recovery from unallocated space (no names/metadata)."""
    items: List[ResultItem] = []
    ov = OpenVolume(image_path)
    image_path = ov.image_path      # converted raw copy for non-raw containers
    vh = ov.vh
    af = AllocationFile(
        image_path=image_path, block_size=vh.block_size,
        extents=vh.allocation_file.active_extents(),
        total_blocks=vh.total_blocks, partition_offset=ov.partition_offset,
    )
    for i, c in enumerate(carve_free_space(image_path, vh.block_size, af, ov.partition_offset)):
        name = f"carved_{i}_blk{c.start_block}.{c.extension}"
        items.append(ResultItem(
            label=f"{name}  ({human_size(len(c.data))})",
            group="Carved Files",
            status="unallocated",
            badge=" ★",
            sections=[("Carved File", [
                ("Name", name, FG_PRIMARY),
                ("Type", c.kind, FG_DIM),
                ("Start block", str(c.start_block), FG_DIM),
                ("Size", human_size(len(c.data)), FG_PRIMARY),
                ("MD5", hashlib.md5(c.data).hexdigest(), FG_DIM),
                ("Size determined by footer", "yes" if c.exact_end else "no (trimmed zero padding)", FG_DIM),
            ])],
            extract_fn=(lambda d=c.data: d),
            rel_path=f"carved/{name}",
        ))
    if not items:
        items.append(ResultItem(
            label="Nothing carved", group="Carved Files", status="info",
            sections=[("Note", [("Detail", "No non-zero data found in unallocated space.", FG_DIM)])],
        ))
    return items, {"total": len(items)}


BACKEND: Dict[str, Callable] = {
    "info": run_info,
    "list": run_list,
    "extract": run_extract,
    "recover": run_recover,
    "carve": run_carve,
    "verify": run_verify,
}


def open_in_viewer(path: str):
    if not path or not os.path.exists(path):
        QMessageBox.warning(None, "File Not Found", f"Not found:\n{path}")
        return
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:  # noqa: BLE001
        QMessageBox.warning(None, "Cannot Open", str(e))


# ---------------------------------------------------------------------------
# Detail panel (right)
# ---------------------------------------------------------------------------

class DetailPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(380)
        self.setStyleSheet(f"background:{BG_PANEL};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        hdr = QLabel("  Details")
        hdr.setFixedHeight(44)
        hdr.setStyleSheet(
            f"background:{BG_PANEL}; color:{COL_CYAN}; font-family:'Segoe UI',Arial;"
            f"font-size:15px; font-weight:800; letter-spacing:2px;"
            f"border-bottom:1px solid {BORDER}; padding-left:10px;"
        )
        layout.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"background:{BG_PANEL}; border:none;")
        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setStyleSheet(
            f"background:{BG_PANEL}; color:{FG_PRIMARY}; border:none;"
            f"font-family:'Segoe UI',Arial,sans-serif; font-size:13px; font-weight:600;"
        )
        scroll.setWidget(self.report)
        layout.addWidget(scroll)
        self._show_placeholder()

    def _show_placeholder(self):
        self.report.setHtml(
            f'<html><body style="background:{BG_PANEL};color:{FG_DIM};'
            f'font-family:\'Segoe UI\',Arial;font-size:14px;margin:24px 20px;">'
            f'<p>Run an operation, then click any result to view its details.</p>'
            f'</body></html>'
        )

    def load(self, item: ResultItem):
        self.report.setHtml(item.to_html())

    def clear(self):
        self._show_placeholder()


# ---------------------------------------------------------------------------
# Result catalog (left)
# ---------------------------------------------------------------------------

class ResultTree(QWidget):
    item_selected = pyqtSignal(object)
    item_opened = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(360)
        self.setMaximumWidth(620)
        self.setStyleSheet(f"background:{BG_PANEL};")
        self._all: List[ResultItem] = []
        self._filter = "All"
        self._op_key = "info"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.hdr = QLabel("  Result Catalog")
        self.hdr.setFixedHeight(44)
        self.hdr.setStyleSheet(
            f"background:{BG_PANEL}; color:{COL_CYAN}; font-family:'Segoe UI',Arial;"
            f"font-size:15px; font-weight:800; letter-spacing:2px;"
            f"border-bottom:1px solid {BORDER}; padding-left:10px;"
        )
        layout.addWidget(self.hdr)

        self.fbar = QWidget()
        self.fbar.setFixedHeight(56)
        self.fbar.setStyleSheet(f"background:{BG_PANEL}; border-bottom:1px solid {BORDER};")
        self.fb = QHBoxLayout(self.fbar)
        self.fb.setContentsMargins(10, 10, 10, 10)
        self.fb.setSpacing(6)
        self._filter_btns: Dict[str, QPushButton] = {}
        layout.addWidget(self.fbar)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.itemClicked.connect(self._on_click)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree)

        self.set_operation("info")

    def set_operation(self, op_key: str):
        self._op_key = op_key
        self._filter = "All"
        for i in reversed(range(self.fb.count())):
            w = self.fb.itemAt(i).widget()
            if w:
                w.setParent(None)
        self._filter_btns = {}
        for lbl in FILTER_TABS.get(op_key, ["All"]):
            btn = QPushButton(lbl)
            btn.setCheckable(True)
            btn.setFixedHeight(34)
            btn.clicked.connect(lambda _, l=lbl: self._set_filter(l))
            self._filter_btns[lbl] = btn
            self.fb.addWidget(btn)
        self._filter_btns["All"].setChecked(True)
        self._restyle_filter_btns()
        self.fbar.setVisible(len(FILTER_TABS.get(op_key, ["All"])) > 1)

    def populate(self, items: List[ResultItem]):
        self._all = items
        self._rebuild()

    def clear(self):
        self._all = []
        self.tree.clear()

    def _matches_filter(self, item: ResultItem) -> bool:
        if self._filter == "All":
            return True
        f = self._filter.lower().rstrip("s")
        if f in ("director", "file", "fragmented"):
            return f in item.group.lower() or f in item.status.lower()
        return f in item.status.lower()

    def _rebuild(self):
        self.tree.clear()
        shown = [a for a in self._all if self._matches_filter(a)]

        groups: Dict[str, List[ResultItem]] = {}
        for a in shown:
            groups.setdefault(a.group, []).append(a)

        for grp_name, arts in groups.items():
            grp = QTreeWidgetItem([f"  {grp_name}   ({len(arts)})"])
            grp.setForeground(0, QColor(COL_BLUE))
            grp.setFont(0, QFont("Segoe UI", 13, QFont.Bold))
            self.tree.addTopLevelItem(grp)
            grp.setExpanded(len(groups) <= 6)

            for a in arts[:2000]:
                item = QTreeWidgetItem()
                item.setText(0, f"  ● {a.label}{a.badge}")
                item.setForeground(0, QColor(a.color))
                item.setData(0, Qt.UserRole, a)
                grp.addChild(item)
            if len(arts) > 2000:
                more = QTreeWidgetItem([f"  ... {len(arts) - 2000} more (refine filter)"])
                more.setForeground(0, QColor(FG_FAINT))
                grp.addChild(more)

    def _on_click(self, item: QTreeWidgetItem, _col: int):
        a = item.data(0, Qt.UserRole)
        if a:
            self.item_selected.emit(a)

    def _on_double_click(self, item: QTreeWidgetItem, _col: int):
        a = item.data(0, Qt.UserRole)
        if a:
            self.item_opened.emit(a)

    def _set_filter(self, label: str):
        self._filter = label
        for lbl, btn in self._filter_btns.items():
            btn.setChecked(lbl == label)
        self._restyle_filter_btns()
        self._rebuild()

    def _restyle_filter_btns(self):
        active = (FG_PRIMARY, "#094771", f"{COL_BLUE}55")
        for lbl, btn in self._filter_btns.items():
            if btn.isChecked():
                fg, bg, bd = active
                btn.setStyleSheet(
                    f"QPushButton {{ background:{bg}; color:{fg};"
                    f"border:1px solid {bd}; border-radius:4px;"
                    f"font-family:'Segoe UI',Arial; font-size:13px;"
                    f"font-weight:800; padding:0 10px; }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background:transparent; color:{FG_DIM};"
                    f"border:1px solid #3a3a3a; border-radius:4px;"
                    f"font-family:'Segoe UI',Arial; font-size:13px; font-weight:600;"
                    f"padding:0 10px; }}"
                    f"QPushButton:hover {{ background:#2a2a2a; color:{FG_PRIMARY}; }}"
                )


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class OperationWorker(QThread):
    finished_ok = pyqtSignal(list, dict)
    error = pyqtSignal(str)

    def __init__(self, op_key: str, image_path: str, outdir: str, parent=None):
        super().__init__(parent)
        self.op_key = op_key
        self.image_path = image_path
        self.outdir = outdir

    def run(self):
        try:
            fn = BACKEND[self.op_key]
            items, summary = fn(self.image_path, self.outdir)
            self.finished_ok.emit(items, summary)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class HFSPlusParserWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._op_key = "info"
        self._worker: Optional[OperationWorker] = None

        self.setWindowTitle("HFS+ Parser")
        self.setMinimumSize(1100, 720)
        self.resize(1600, 940)
        self.setStyleSheet(DARK_QSS)

        self._build_ui()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        vbox.addWidget(self._build_toolbar())

        self.prog = QProgressBar()
        self.prog.setFixedHeight(10)
        self.prog.setTextVisible(False)
        self.prog.hide()
        vbox.addWidget(self.prog)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)

        self.tree = ResultTree()
        self.tree.item_selected.connect(self._on_select)
        self.tree.item_opened.connect(self._on_open)
        splitter.addWidget(self.tree)

        self.detail = DetailPanel()
        splitter.addWidget(self.detail)

        splitter.setSizes([460, 1140])
        splitter.setStretchFactor(1, 1)
        vbox.addWidget(splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._set_operation("info")

    def _build_toolbar(self) -> QWidget:
        outer = QWidget()
        outer.setStyleSheet(f"background:{BG_PANEL}; border-bottom:1px solid {BORDER};")
        outer.setFixedHeight(126)
        vl = QVBoxLayout(outer)
        vl.setContentsMargins(16, 10, 16, 10)
        vl.setSpacing(10)

        def lbl(t):
            w = QLabel(t)
            w.setStyleSheet(
                f"color:{FG_PRIMARY}; font-family:'Segoe UI',Arial;"
                f"font-size:14px; font-weight:700;"
            )
            return w

        def choose_btn(slot):
            b = QPushButton("Choose…")
            b.setFixedHeight(40)
            b.setStyleSheet(
                f"QPushButton {{ background:#2d2d2d; color:{FG_PRIMARY};"
                f"border:1px solid {BORDER}; border-radius:4px;"
                f"font-family:'Segoe UI',Arial; font-size:14px; font-weight:600;"
                f"padding:0 16px; }}"
                f"QPushButton:hover {{ background:#3a3a3a; color:{FG_PRIMARY}; }}"
            )
            b.clicked.connect(slot)
            return b

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        row1.addWidget(lbl("Evidence Image"))
        self.image_edit = QLineEdit()
        self.image_edit.setPlaceholderText("Select image  (.dmg .img .raw .dd)…")
        self.image_edit.setFixedHeight(40)
        row1.addWidget(self.image_edit, 1)
        row1.addWidget(choose_btn(self._browse_image))

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedHeight(40)
        sep.setStyleSheet(f"color:{BORDER};")
        row1.addWidget(sep)

        row1.addWidget(lbl("Output Folder"))
        self.out_edit = QLineEdit()
        self.out_edit.setText(os.path.join(os.path.expanduser("~"), "hfsplus_output"))
        self.out_edit.setFixedHeight(40)
        row1.addWidget(self.out_edit, 1)
        row1.addWidget(choose_btn(self._browse_out))
        vl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        op_lbl = QLabel("Operation:")
        op_lbl.setStyleSheet(
            f"color:{FG_PRIMARY}; font-family:'Segoe UI',Arial;"
            f"font-size:14px; font-weight:700;"
        )
        row2.addWidget(op_lbl)

        self._op_btns: Dict[str, QPushButton] = {}
        for key, display in OPERATIONS:
            btn = QPushButton(display)
            btn.setCheckable(True)
            btn.setFixedHeight(38)
            btn.clicked.connect(lambda _, k=key: self._set_operation(k))
            self._op_btns[key] = btn
            row2.addWidget(btn)

        row2.addStretch()

        self.lbl_run_status = QLabel("")
        self.lbl_run_status.setStyleSheet(
            f"color:{FG_DIM}; font-family:'Segoe UI',Arial; font-size:14px; font-weight:700;"
        )
        row2.addWidget(self.lbl_run_status)

        self.btn_start = QPushButton("▶  Run")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.setFixedHeight(42)
        self.btn_start.setMinimumWidth(150)
        self.btn_start.clicked.connect(self._start)
        row2.addWidget(self.btn_start)

        vl.addLayout(row2)
        return outer

    def _set_operation(self, key: str):
        self._op_key = key
        active = (COL_GREEN, "#0b2e1e", f"{COL_GREEN}55")
        off_ss = (
            f"QPushButton {{ background:#2a2a2a; color:{FG_DIM};"
            f"border:1px solid #333; border-radius:4px;"
            f"font-family:'Segoe UI',Arial; font-size:14px; font-weight:600;"
            f"padding:0 14px; }}"
            f"QPushButton:hover {{ background:#343434; color:{FG_PRIMARY}; }}"
        )
        for k, btn in self._op_btns.items():
            btn.setChecked(k == key)
            if k == key:
                fg, bg, bd = active
                btn.setStyleSheet(
                    f"QPushButton {{ background:{bg}; color:{fg};"
                    f"border:1px solid {bd}; border-radius:4px;"
                    f"font-family:'Segoe UI',Arial; font-size:14px;"
                    f"font-weight:800; padding:0 14px; }}"
                )
            else:
                btn.setStyleSheet(off_ss)
        self.tree.set_operation(key)
        self.tree.clear()
        self.detail.clear()
        self._update_status({})

    def _browse_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Evidence Image", os.path.expanduser("~"),
            "Disk images (*.dmg *.img *.raw *.dd);;All files (*)",
        )
        if path:
            self.image_edit.setText(path)

    def _browse_out(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self.out_edit.text() or os.path.expanduser("~"),
        )
        if path:
            self.out_edit.setText(path)

    def _start(self):
        image = self.image_edit.text().strip()
        outdir = self.out_edit.text().strip()
        if not image:
            QMessageBox.warning(self, "HFS+ Parser", "Please select an evidence image.")
            return
        if not os.path.exists(image):
            QMessageBox.warning(self, "HFS+ Parser", f"Image not found:\n{image}")
            return
        if outdir:
            os.makedirs(outdir, exist_ok=True)

        self.btn_start.setEnabled(False)
        self.prog.setRange(0, 0)
        self.prog.show()
        op_display = dict(OPERATIONS)[self._op_key]
        self._set_run_status(f"Running {op_display}…", COL_AMBER)
        self.tree.clear()
        self.detail.clear()

        self._worker = OperationWorker(self._op_key, image, outdir, parent=self)
        self._worker.finished_ok.connect(self._done)
        self._worker.error.connect(self._op_error)
        self._worker.start()

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            reply = QMessageBox.question(
                self, "HFS+ Parser",
                "An operation is still running and cannot be safely cancelled.\n\n"
                "Force-quit the application now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            os._exit(1)
        event.accept()

    def _done(self, items: List[ResultItem], summary: dict):
        self.btn_start.setEnabled(True)
        self.prog.setRange(0, 100)
        self.prog.setValue(100)
        self._set_run_status(f"Complete — {len(items)} results", COL_GREEN)
        QTimer.singleShot(2500, self.prog.hide)
        self.tree.populate(items)
        self._update_status(summary)

    def _op_error(self, msg: str):
        self.btn_start.setEnabled(True)
        self.prog.hide()
        self._set_run_status(f"Error: {msg[:60]}", COL_RED)
        QMessageBox.critical(self, "HFS+ Parser — Error", msg)

    def _set_run_status(self, text: str, color: str):
        self.lbl_run_status.setText(text)
        self.lbl_run_status.setStyleSheet(
            f"color:{color}; font-family:'Segoe UI',Arial; font-size:14px; font-weight:700;"
        )

    def _on_select(self, a: ResultItem):
        self.detail.load(a)

    def _on_open(self, a: ResultItem):
        if a.full_path and os.path.exists(a.full_path):
            open_in_viewer(a.full_path)
            return
        if a.extract_fn is not None:
            try:
                data = a.extract_fn()
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Extract Failed", f"{type(e).__name__}: {e}")
                return
            outdir = self.out_edit.text().strip() or os.path.expanduser("~")
            dest = os.path.join(outdir, "_opened", a.rel_path or "extracted_file")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            a.full_path = dest
            open_in_viewer(dest)

    def _update_status(self, summary: dict):
        total = summary.get("total", 0)
        image = self.image_edit.text().strip() or "—"
        extra = "  │  ".join(f"{k}: {v}" for k, v in summary.items() if k != "total")
        msg = f"  {total} results"
        if extra:
            msg += f"  │  {extra}"
        msg += f"          Image: {image}"
        self.status_bar.showMessage(msg)


def launch():
    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication(sys.argv)
    win = HFSPlusParserWindow()
    win.show()
    if owns_app:
        sys.exit(app.exec_())
    else:
        app.exec_()


if __name__ == "__main__":
    launch()
