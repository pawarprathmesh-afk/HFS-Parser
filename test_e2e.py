"""
End-to-end check (macOS only): builds throwaway HFS+ images with hdiutil,
deletes files, and verifies that every deleted file is recovered with a
matching MD5 on both a journaled and a non-journaled volume, and that a
fragmented live file extracts correctly via the Extents Overflow tree.

Run: python3 test_e2e.py
"""
import hashlib, os, subprocess, tempfile

from partition import find_hfsplus_partition
from volume_header import read_volume_header
from fork_reader import ForkReader
from btree import BTree
from allocation_file import AllocationFile
from recovery import DeletedFileScanner
from carver import carve_free_space
from catalog import CatalogTree, CatalogFileRecord, ROOT_FOLDER_ID
from file_extract import extract_fork_data
from extents_overflow import ExtentsOverflowTree, DATA_FORK_TYPE


def sh(*a):
    return subprocess.run(a, check=True, capture_output=True, text=True).stdout


def make_volume(tmp, name, fs, size="20m"):
    img = os.path.join(tmp, name + ".dmg")
    sh("hdiutil", "create", "-size", size, "-fs", fs, "-volname", name, "-layout", "NONE", img)
    sh("hdiutil", "attach", img, "-nobrowse", "-noverify")
    return img, f"/Volumes/{name}"


def detach(mount):
    sh("sync")
    sh("hdiutil", "detach", mount)


def open_image(img):
    off = find_hfsplus_partition(img).start_offset
    vh = read_volume_header(img, off)
    bt = BTree(ForkReader(image_path=img, block_size=vh.block_size,
                          extents=vh.catalog_file.active_extents(), partition_offset=off))
    af = AllocationFile(image_path=img, block_size=vh.block_size,
                        extents=vh.allocation_file.active_extents(),
                        total_blocks=vh.total_blocks, partition_offset=off)
    eo = None
    if vh.extents_file.logical_size > 0:
        eo = ExtentsOverflowTree(BTree(ForkReader(image_path=img, block_size=vh.block_size,
                                                  extents=vh.extents_file.active_extents(),
                                                  partition_offset=off)))
    return off, vh, bt, af, eo


def md5(b): return hashlib.md5(b).hexdigest()


def populate_and_delete(mount):
    os.makedirs(f"{mount}/d")
    for i in range(3):
        open(f"{mount}/d/keep{i}.txt", "w").write(f"keep {i}\n" * 20)
    deleted = {}
    for name, size in [("a.txt", 40), ("b.bin", 150000), ("c.txt", 9000)]:
        data = os.urandom(size // 4 * 3).hex().encode()[:size] if size > 100 else b"SECRET %d\n" % size
        open(f"{mount}/d/{name}", "wb").write(data)
        deleted[name] = md5(data)
    subprocess.run(["sync"]); 
    import time; time.sleep(2)
    for name in deleted:
        os.remove(f"{mount}/d/{name}")
    time.sleep(2)
    return deleted


def test_deleted(tmp, journaled):
    fs = "Journaled HFS+" if journaled else "HFS+"
    img, mount = make_volume(tmp, "J" if journaled else "N", fs)
    deleted = populate_and_delete(mount)
    detach(mount)
    off, vh, bt, af, eo = open_image(img)
    assert vh.is_journaled == journaled
    got = {}
    for r in DeletedFileScanner(bt, af, img, vh, off).scan():
        if r.fully_free:
            data = extract_fork_data(img, vh.block_size, r.record.data_fork, off,
                                     r.record.file_id, DATA_FORK_TYPE, eo)
            got[r.key.node_name] = md5(data)
    if journaled:
        assert got == deleted, f"journal recovery mismatch: {got} vs {deleted}"
    else:  # names are scrubbed on non-journaled volumes -> content carving
        carved = {md5(c.data) for c in carve_free_space(img, vh.block_size, af, off)}
        missing = set(deleted.values()) - carved
        assert not missing, f"carving missed {missing}"
    print(f"PASS deleted-file recovery ({'journaled, by name' if journaled else 'non-journaled, carved'})")


def test_fragmented(tmp):
    img, mount = make_volume(tmp, "FR", "HFS+", "4m")
    i = 0
    while True:
        try:
            open(f"{mount}/s{i}", "wb").write(os.urandom(4096)); i += 1
        except OSError:
            break
    sh("sync")
    for j in range(0, i, 3):
        try: os.remove(f"{mount}/s{j}")
        except OSError: pass
    data = os.urandom(200000)
    open(f"{mount}/frag.bin", "wb").write(data)
    detach(mount)
    off, vh, bt, af, eo = open_image(img)
    for _, rec in CatalogTree(bt).walk(ROOT_FOLDER_ID):
        if isinstance(rec, CatalogFileRecord) and rec.data_fork.logical_size == 200000:
            assert sum(e.block_count for e in rec.data_fork.active_extents()) < rec.data_fork.total_blocks, \
                "file was not fragmented beyond 8 extents; test inconclusive"
            out = extract_fork_data(img, vh.block_size, rec.data_fork, off, rec.file_id, DATA_FORK_TYPE, eo)
            assert md5(out) == md5(data)
            print("PASS fragmented file via Extents Overflow")
            return
    raise AssertionError("frag.bin not found")


def test_deleted_compressed(tmp):
    """Delete compressed files (all codecs; inline and resource-fork) from a journaled volume
    and recover them exactly."""
    import time
    from volume import open_volume
    img, mount = make_volume(tmp, "JC", "Journaled HFS+", "40m")
    words = "alpha beta gamma delta forensic recover compressed".split()
    man = {}
    import random
    rnd = random.Random(5)
    for codec in ("ZLIB", "LZVN", "LZFSE"):
        for n in (100, 70000, 400000):
            p = f"{mount}/{codec}_{n}.txt"
            data = " ".join(rnd.choice(words) for _ in range(n // 4 + 10))[:n].encode()
            open(p, "wb").write(data)
            subprocess.run(["afsctool", "-c", "-T", codec, "-L", p], capture_output=True)
            man[f"{codec}_{n}.txt"] = md5(data)
    subprocess.run(["sync"]); time.sleep(2)
    for name in man:
        os.remove(f"{mount}/{name}")
    subprocess.run(["sync"]); time.sleep(3)
    detach(mount)
    v = open_volume(img)
    got = {}
    for r in DeletedFileScanner(v.catalog_btree, v.allocation_file(), v.image_path, v.vh, v.partition_offset).scan():
        got[r.key.node_name] = md5(v.read_deleted_file(r.record, r.decmpfs))
    assert got == man, f"recovered {len(got)}/{len(man)}; wrong: {[k for k in man if got.get(k) != man[k]]}"
    print("PASS deleted compressed files (9 files, 3 codecs)")


def test_codecs_against_libcompression():
    """Decode streams made by Apple's own encoder (libcompression) with the pure-Python decoders."""
    import ctypes, random
    import decmpfs
    lib = ctypes.CDLL("/usr/lib/libcompression.dylib")

    def enc(alg, data):
        cap = len(data) * 2 + 4096
        dst = ctypes.create_string_buffer(cap)
        n = lib.compression_encode_buffer(dst, cap, data, len(data), None, alg)   # fill first,
        return dst.raw[:n]                                                       # then snapshot
    rnd = random.Random(11)
    words = b"alpha beta gamma delta epsilon zeta eta theta iota kappa".split()

    def gen(kind, n):
        if kind == "text": return b" ".join(rnd.choice(words) for _ in range(n // 5 + 5))[:n]
        if kind == "random": return os.urandom(n)
        if kind == "zeros": return bytes(n)
        if kind == "runs": return b"".join(bytes([rnd.randrange(256)]) * rnd.randrange(1, 300) for _ in range(n // 50 + 2))[:n]
        return (gen("text", n // 2) + os.urandom(n // 4) + bytes(n // 4))[:n]
    checked = 0
    for kind in ("text", "random", "zeros", "runs", "mixed"):
        for n in (1, 7, 16, 100, 257, 4096, 30000, 65536, 300000):
            data = gen(kind, n)
            for alg, dec in ((0x900, lambda b, n: decmpfs.lzvn_decode(b, n)), (0x801, lambda b, n: decmpfs.lzfse_decode(b, n))):
                blob = enc(alg, data)
                if blob:
                    assert dec(blob, len(data)) == data, f"{kind} {n} alg {alg:#x}"
                    checked += 1
    print(f"PASS codecs vs libcompression ({checked} streams)")


def test_fuzz(image, trials=25):
    """Random corruption of metadata must never hang, recurse forever or exhaust memory."""
    import random, shutil, signal
    from volume import open_volume
    from catalog import CatalogFileRecord as FileRec
    rnd = random.Random(1)
    v0 = open_volume(image)
    regions = [v0.vh.catalog_file, v0.vh.extents_file, v0.vh.attributes_file, v0.vh.allocation_file, None]

    class Timeout(Exception):
        pass
    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(Timeout()))
    with tempfile.TemporaryDirectory() as d:
        dst = os.path.join(d, "fuzz.dmg")
        for t in range(trials):
            shutil.copy(image, dst)
            fork = rnd.choice(regions)
            with open(dst, "r+b") as f:
                for _ in range(rnd.choice([1, 5, 40, 400])):
                    if fork is None:
                        pos = 1024 + rnd.randrange(512)
                    else:
                        e = rnd.choice(fork.active_extents())
                        pos = e.start_block * v0.vh.block_size + rnd.randrange(e.block_count * v0.vh.block_size)
                    f.seek(pos); f.write(bytes([rnd.randrange(256)]))
            signal.alarm(30)
            try:
                v = open_volume(dst)
                for _p, r in v.walk():
                    if isinstance(r, FileRec):
                        try:
                            v.read_file(r)
                        except Exception:  # noqa: BLE001 - clean per-file failure is fine
                            pass
            except (Timeout, MemoryError, RecursionError) as e:
                raise AssertionError(f"trial {t}: {type(e).__name__}")
            except Exception:  # noqa: BLE001 - a clean error on a corrupt volume is the right outcome
                pass
            finally:
                signal.alarm(0)
    print(f"PASS fuzz ({trials} corrupted images)")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_deleted(tmp, True)
        test_deleted(tmp, False)
        test_fragmented(tmp)
