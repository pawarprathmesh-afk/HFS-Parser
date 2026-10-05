# HFS+ Parser

A pure-Python (standard library only) parser and forensic toolkit for **HFS+ / HFSX** volumes, with a
desktop GUI. It reads disk images directly, without mounting them, and never modifies the evidence.

## What it does

- **Opens** raw images, GPT / APM / MBR partitioned images, old HFS-wrapped volumes, and (on macOS, via
  `hdiutil`) compressed `.dmg`, sparse images and sparsebundles. Falls back to the alternate volume header
  if the primary one is damaged.
- **Parses** the volume header, allocation bitmap, Catalog / Extents-Overflow / Attributes B-trees
  (any node size), case-sensitive (HFSX) volumes, any allocation block size.
- **Extracts** files, including fragmented files, hard links, resource forks, and transparently
  compressed files (decmpfs: zlib, LZVN, LZFSE; decoders are pure Python).
- **Replays the journal** in memory when a volume wasn't cleanly unmounted.
- **Recovers deleted files**
  - *Recover*: deleted Catalog records (with names, dates, extents) from orphaned nodes, node slack and
    the journal. This is the best method and works on journaled volumes.
  - *Carve*: file contents from free space, for volumes where the records were scrubbed (no names).
- **Verifies** B-tree and allocation-bitmap consistency.

## Usage

GUI (needs PyQt5):

```
pip install -r requirements-gui.txt
python3 gui_main.py
```

Choose an image, pick an operation (**Info, List, Extract, Recover, Carve, Verify**) and **Run**.
Double-click a result to extract and open it.

Command line:

```
python3 run_parser.py  image.dmg                 # volume info, file tree, sample extraction
python3 test_recover.py image.dmg --out ./out    # recover deleted files (+ carve), save to ./out
```

As a library:

```python
from volume import open_volume
vol = open_volume("image.dmg")
for path, record in vol.walk():
    if hasattr(record, "data_fork"):
        data = vol.read_file(record)       # decompressed, hard links resolved
```

## Tests

`test_all.py` builds real HFS+ volumes with macOS (`hdiutil`), fills them with awkward content, records the
OS's own view as ground truth and compares the parser byte for byte (MD5). It also covers journal replay,
deleted-file recovery, the compression decoders (cross-checked against Apple's `libcompression`) and a
corruption fuzz.

```
python3 test_all.py          # macOS only, ~4 minutes; `brew install afsctool` for full codec coverage
```

## Limitations

- Recovery is best-effort: overwritten blocks are gone, and on non-journaled volumes macOS erases deleted
  Catalog records, so only anonymous contents can be carved. Carving can't find the end of some zero-padded
  binaries (e.g. TIFF, Mach-O).
- Not supported: FileVault / CoreStorage encrypted volumes, APFS, directory hard links, ACLs.
- The whole Catalog is held in memory (fine at 40,000 files; millions of files untested).
- Journal replay is verified against genuine macOS transaction data and hand-built pending transactions;
  macOS never produced a naturally dirty journal for testing, and big-endian journals are untested.
- Converting non-raw image containers requires macOS (`hdiutil`).
