"""
Journal replay checks using hand-crafted pending transactions (block-list header layout
verified against genuine macOS journal data). Needs /tmp/h/dirty.dmg style input: pass a
clean journaled HFS+ image containing /f1.txt as argv[1].
"""
import shutil, struct, sys, tempfile, os
from volume import open_volume
from journal import read_journal_info


def craft(src, dst, start_pos, payload, corrupt=False):
    shutil.copy(src, dst)
    v = open_volume(dst)
    info = read_journal_info(v.image_path, v.vh, v.partition_offset)
    jstart = v.partition_offset + info.offset
    with open(dst, "r+b") as f:
        f.seek(jstart)
        hdr = bytearray(f.read(512))
        _m, _e, _s, _end, size, blhdr, _c, jhdr = struct.unpack("<IIQQQIII", hdr[:44])
        rec = v.catalog.lookup(2, "f1.txt")
        phys = v.partition_offset + rec.data_fork.active_extents()[0].start_block * v.vh.block_size
        txn = bytearray(blhdr)
        struct.pack_into("<HHIII", txn, 0, 511, 2, blhdr + 4096, 0, 0)
        struct.pack_into("<QII", txn, 16, 0, 0, 1)
        struct.pack_into("<QII", txn, 32, phys // jhdr, 4096, 0)
        raw = bytes(txn) + payload.ljust(4096, b"\0")
        if corrupt:
            raw = b"\xff" * len(raw)
        p, rem = start_pos, raw
        while rem:                                  # write with wrap-around
            n = min(size - p, len(rem))
            f.seek(jstart + p); f.write(rem[:n]); rem = rem[n:]
            p = jhdr if p + n >= size else p + n
        struct.pack_into("<QQ", hdr, 8, start_pos, p)
        f.seek(jstart); f.write(hdr)


def check(path, expect, pending, problem):
    v = open_volume(path)
    data = v.read_file(v.catalog.lookup(2, "f1.txt"))
    assert data == expect[:len(data)] and len(data) == 7, f"content {data!r}"
    assert v.journal_pending_blocks == pending, v.journal_pending_blocks
    assert (v.journal_problem is not None) == problem, v.journal_problem


if __name__ == "__main__":
    src = sys.argv[1]
    with tempfile.TemporaryDirectory() as tmp:
        d = lambda n: os.path.join(tmp, n)  # noqa: E731
        craft(src, d("a.dmg"), 17408, b"REPLAYED-NORMAL"); check(d("a.dmg"), b"REPLAYED-NORMAL", 1, False)
        craft(src, d("b.dmg"), 524288 - 6000, b"REPLAYED-WRAPPED"); check(d("b.dmg"), b"REPLAYED-WRAPPED", 1, False)
        craft(src, d("c.dmg"), 17408, b"x", corrupt=True); check(d("c.dmg"), b"file 1\n", 0, True)
        check(src, b"file 1\n", 0, False)
    print("PASS journal replay (normal, wrap-around, corrupt, clean)")
