"""
Full verification suite (macOS: needs hdiutil; afsctool optional for codec coverage).

Builds real HFS+ volumes with macOS itself, fills them with tricky content, records the OS's own
view as ground truth and compares the parser against it byte for byte:

  * layouts      GPT, APM, none; journaled and not; case-sensitive (HFSX); 512 B and 16 KB allocation
                 blocks; 16 KB B-tree nodes
  * contents     1,500-file folders, hard links, resource forks, xattrs, unicode, deep trees,
                 compressed files (zlib / LZVN / LZFSE, inline and resource-fork)
  * containers   compressed UDZO, sparse image, sparsebundle, MBR, 4K-sector GPT, HFS wrapper,
                 damaged primary header (alternate header)
  * journal      pending-transaction replay (normal / wrap-around / corrupt)
  * recovery     deleted files by name (journal), carved from free space, deleted compressed files
  * decoders     LZVN / LZFSE vs Apple's libcompression encoder
  * robustness   random corruption never hangs or crashes

Run: python3 test_all.py
"""
import os, shutil, struct, subprocess, sys, tempfile, traceback

import check_volume
import make_test_volume
import test_e2e
import test_journal_replay

RESULTS = []


def run(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"PASS  {name}", flush=True)
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"FAIL  {name}: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc(limit=3)


def sh(*a):
    return subprocess.run(a, check=True, capture_output=True, text=True).stdout


def mount(img):
    out = sh("hdiutil", "attach", img, "-nobrowse", "-noverify")
    return [l for l in out.splitlines() if "/Volumes/" in l][-1].split("\t")[-1].strip()


def build_volume(tmp, name, fs, size, layout="NONE", fsargs=None, case=False):
    img = os.path.join(tmp, name + ".dmg")
    cmd = ["hdiutil", "create", "-size", size, "-fs", fs, "-volname", name.upper(), "-layout", layout]
    if fsargs:
        cmd += ["-fsargs", fsargs]
    sh(*cmd, img)
    mp = mount(img)
    manifest = os.path.join(tmp, name + ".json")
    try:
        make_test_volume.populate(mp, manifest, case=case)
    finally:
        sh("sync"); sh("hdiutil", "detach", mp)
    return img, manifest


def verify(img, manifest):
    summary, problems = check_volume.check(img, manifest)
    assert not problems, "; ".join(problems)


def main():
    if sys.platform != "darwin":
        sys.exit("test_all.py builds volumes with hdiutil and only runs on macOS")
    with tempfile.TemporaryDirectory() as tmp:
        built = {}
        layouts = [
            ("gpt_journaled", "Journaled HFS+", "60m", "GPTSPUD", None, False),
            ("apm_journaled", "Journaled HFS+", "60m", "SPUD", None, False),
            ("hfsx_case_sensitive", "Case-sensitive HFS+", "50m", "NONE", None, True),
            ("block_16k", "HFS+", "50m", "NONE", "-b 16384", False),
            ("block_512", "HFS+", "50m", "NONE", "-b 512", False),
            ("node_16k", "HFS+", "60m", "NONE", "-n c=16384,e=16384,a=16384", False),
        ]
        for name, fs, size, layout, fsargs, case in layouts:
            def t(name=name, fs=fs, size=size, layout=layout, fsargs=fsargs, case=case):
                built[name] = build_volume(tmp, name, fs, size, layout, fsargs, case)
                verify(*built[name])
            run(f"layout/content: {name}", t)

        # containers built from known-good volumes
        if "hfsx_case_sensitive" in built:
            raw, manifest = built["hfsx_case_sensitive"]
            for label, fmt, out in (("compressed UDZO", "UDZO", "z.dmg"), ("sparse image", "UDSP", "s"),
                                    ("sparsebundle", "UDSB", "b")):
                def t(fmt=fmt, out=out):
                    sh("hdiutil", "convert", raw, "-format", fmt, "-o", os.path.join(tmp, out))
                    produced = [p for p in (out, out + ".sparseimage", out + ".sparsebundle")
                                if os.path.exists(os.path.join(tmp, p))][0]
                    verify(os.path.join(tmp, produced), manifest)
                run(f"container: {label}", t)

            vol = open(raw, "rb").read()

            def wrap(name, data):
                p = os.path.join(tmp, name)
                open(p, "wb").write(data)
                verify(p, manifest)

            def mbr():
                m = bytearray(512); m[450] = 0xAF
                struct.pack_into("<II", m, 454, 2048, len(vol) // 512); m[510:512] = b"\x55\xaa"
                wrap("mbr.dmg", bytes(m) + b"\0" * (2048 * 512 - 512) + vol)

            def gpt4k():
                sec = 4096; img = bytearray(sec * 16)
                hdr = bytearray(92); hdr[0:8] = b"EFI PART"
                struct.pack_into("<Q", hdr, 72, 2); struct.pack_into("<II", hdr, 80, 4, 128)
                img[sec:sec + 92] = hdr
                ent = bytearray(128); ent[0:16] = bytes(range(1, 17))
                struct.pack_into("<QQ", ent, 32, 16, 16 + len(vol) // sec - 1)
                img[2 * sec:2 * sec + 128] = ent
                wrap("gpt4k.dmg", bytes(img) + vol)

            def hfs_wrapper():
                mdb = bytearray(512); mdb[0:2] = b"BD"
                struct.pack_into(">I", mdb, 20, 4096); struct.pack_into(">H", mdb, 28, 8)
                mdb[124:126] = b"HX"; struct.pack_into(">HH", mdb, 126, 4, len(vol) // 4096)
                wrap("wrapper.dmg", b"\0" * 1024 + bytes(mdb) + b"\0" * (20480 - 1536) + vol)

            def damaged_header():
                wrap("damaged.dmg", vol[:1024] + b"\0" * 512 + vol[1536:])

            run("container: MBR partition table", mbr)
            run("container: GPT with 4096-byte sectors", gpt4k)
            run("container: HFS wrapper", hfs_wrapper)
            run("damaged primary header -> alternate header", damaged_header)

        # journal replay
        def journal():
            img = os.path.join(tmp, "jr.dmg")
            sh("hdiutil", "create", "-size", "20m", "-fs", "Journaled HFS+", "-volname", "JR", "-layout", "NONE", img)
            mp = mount(img)
            for i in range(40):
                open(f"{mp}/f{i}.txt", "w").write(f"file {i}\n")
            sh("sync"); sh("hdiutil", "detach", mp)
            src = os.path.join(tmp, "jr_clean.dmg"); shutil.copy(img, src)
            d = lambda n: os.path.join(tmp, n)  # noqa: E731
            test_journal_replay.craft(src, d("a.dmg"), 17408, b"REPLAYED-NORMAL")
            test_journal_replay.check(d("a.dmg"), b"REPLAYED-NORMAL", 1, False)
            test_journal_replay.craft(src, d("b.dmg"), 524288 - 6000, b"REPLAYED-WRAPPED")
            test_journal_replay.check(d("b.dmg"), b"REPLAYED-WRAPPED", 1, False)
            test_journal_replay.craft(src, d("c.dmg"), 17408, b"x", corrupt=True)
            test_journal_replay.check(d("c.dmg"), b"file 1\n", 0, True)
        run("journal replay (normal / wrap-around / corrupt)", journal)

        # recovery
        run("recovery: journaled, by name", lambda: test_e2e.test_deleted(tmp, True))
        run("recovery: non-journaled, carved", lambda: test_e2e.test_deleted(tmp, False))
        run("fragmented file (extents overflow)", lambda: test_e2e.test_fragmented(tmp))
        if shutil.which("afsctool"):
            run("recovery: deleted compressed files (zlib/LZVN/LZFSE)", lambda: test_e2e.test_deleted_compressed(tmp))
        else:
            print("SKIP  recovery of deleted compressed files (brew install afsctool)")

        # decoders against Apple's encoder
        run("LZVN/LZFSE decoders vs libcompression", test_e2e.test_codecs_against_libcompression)

        # robustness
        if "hfsx_case_sensitive" in built:
            run("corruption fuzz (no hangs / crashes)", lambda: test_e2e.test_fuzz(built["hfsx_case_sensitive"][0], 25))

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
