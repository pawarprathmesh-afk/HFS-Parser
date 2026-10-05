"""Fills a mounted HFS+ volume with tricky files (compressed, hard-linked, resource forks, xattrs,
unicode, deep trees, 1,500 files in one folder...) and records the OS's own view of them as ground
truth, for check_volume.py to compare the parser against.

    python3 make_test_volume.py <mountpoint> <manifest.json> [--case]
"""
import os, sys, json, hashlib, subprocess, random, shutil, tempfile


def populate(mp, out, case=False):
    random.seed(7)
    _populate(mp, out, case)


def _populate(mp, out, case):
    def w(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(data)
    def text(n): return (("lorem ipsum dolor sit amet %d " % random.random().__hash__()) * (n // 20 + 1))[:n].encode()
    # plain files of varied size
    for i in range(40): w(f"{mp}/plain/f{i}.txt", text(random.choice([0,1,100,4095,4096,4097,20000,200000])))
    # many files in one dir -> multi-level catalog B-tree
    for i in range(1500): w(f"{mp}/many/file_{i:05d}.dat", text(random.randint(10, 300)))
    # case-differing names (only valid on HFSX)
    if case:
        w(f"{mp}/case/Readme.txt", b"upper R"); w(f"{mp}/case/readme.txt", b"lower r"); w(f"{mp}/case/README.TXT", b"all caps")
    # compressed (transparent decmpfs) files
    os.makedirs(f"{mp}/compressed", exist_ok=True)
    for i, n in enumerate([50, 3000, 70000, 1_500_000]):
        src = os.path.join(tempfile.gettempdir(), f"_hfs_src{i}.txt"); open(src, "wb").write(text(n))
        subprocess.run(["ditto", "--hfsCompression", src, f"{mp}/compressed/c{i}.txt"], check=True)
        os.remove(src)
    # each decmpfs codec, inline (small) and in the resource fork (large); needs `brew install afsctool`
    if shutil.which("afsctool"):
        for codec in ("ZLIB", "LZVN", "LZFSE"):
            for n in (100, 70000, 700000):
                p = f"{mp}/codecs/{codec}_{n}.txt"
                w(p, text(n))
                subprocess.run(["afsctool", "-c", "-T", codec, "-L", p], capture_output=True)
    # hardlinks
    w(f"{mp}/links/original.txt", text(5000))
    os.link(f"{mp}/links/original.txt", f"{mp}/links/hard1.txt")
    os.link(f"{mp}/links/original.txt", f"{mp}/links/hard2.txt")
    os.makedirs(f"{mp}/links/sub", exist_ok=True)
    os.link(f"{mp}/links/original.txt", f"{mp}/links/sub/hard3.txt")
    # resource forks + xattrs
    w(f"{mp}/forks/with_rsrc.txt", b"data fork content")
    open(f"{mp}/forks/with_rsrc.txt/..namedfork/rsrc", "wb").write(text(9000))
    w(f"{mp}/forks/rsrc_only.txt", b"")
    open(f"{mp}/forks/rsrc_only.txt/..namedfork/rsrc", "wb").write(text(300))
    subprocess.run(["xattr", "-w", "user.test", "value123", f"{mp}/forks/with_rsrc.txt"], check=True)
    big = bytes(random.getrandbits(8) for _ in range(100000))
    subprocess.run(["xattr", "-w", "user.big", big.hex(), f"{mp}/forks/with_rsrc.txt"], check=True)
    # misc
    w(f"{mp}/misc/Ünï_文件.txt", b"unicode"); os.symlink("../plain/f1.txt", f"{mp}/misc/sym")
    d = f"{mp}/deep"
    for i in range(15): d += f"/d{i}"
    w(d + "/x.txt", b"deep")
    subprocess.run(["sync"])
    # ground truth
    def md5(b): return hashlib.md5(b).hexdigest()
    man = {}
    for root, dirs, files in os.walk(mp):
        for n in files + dirs:
            p = os.path.join(root, n); rel = os.path.relpath(p, mp)
            if rel.startswith((".fseventsd", ".Spotlight", ".Trashes", ".TemporaryItems", ".DocumentRevisions", ".HFS+")): continue
            st = os.lstat(p)
            e = {"nlink": st.st_nlink, "type": "dir" if os.path.isdir(p) and not os.path.islink(p) else "link" if os.path.islink(p) else "file"}
            if e["type"] == "file":
                data = open(p, "rb").read(); e["size"] = len(data); e["md5"] = md5(data)
                try: r = open(p + "/..namedfork/rsrc", "rb").read()
                except OSError: r = b""
                if "compressed" in rel: r = b""   # rsrc of compressed files is the compressed stream, not user data
                e["rsrc_md5"] = md5(r) if r else None; e["rsrc_size"] = len(r)
            man[rel] = e
    json.dump(man, open(out, "w"), indent=1)
    print("files in manifest:", len(man))


if __name__ == "__main__":
    populate(sys.argv[1], sys.argv[2], "--case" in sys.argv)
