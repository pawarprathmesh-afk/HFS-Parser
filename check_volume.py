"""usage: check_volume.py <image> <manifest.json>
Compares the parser's view of an image against ground truth recorded from the live mount
(see make_test_volume.py). Importable: check(image, manifest_path) -> (summary_line, problems)."""
import sys, json, hashlib, unicodedata, collections
from volume import open_volume   # single entry point (see volume.py)
from catalog import CatalogFileRecord, CatalogFolderRecord

def check(img, man_path):
    man = {unicodedata.normalize("NFC", k): v for k, v in json.load(open(man_path)).items()}
    vol = open_volume(img)
    info = (f"scheme={vol.part_info.scheme} offset={vol.partition_offset} block={vol.vh.block_size} "
            f"node={vol.catalog_btree.node_size} case_sensitive={vol.catalog_btree.header.is_case_sensitive} "
            f"journaled={vol.vh.is_journaled}")
    seen = {}
    for path, rec in vol.walk():
        seen[unicodedata.normalize("NFC", path.lstrip("/"))] = rec
    errors = collections.Counter(); samples = collections.defaultdict(list)

    def bad(kind, msg):
        errors[kind] += 1
        if len(samples[kind]) < 3:
            samples[kind].append(msg)

    for rel, e in man.items():
        rec = seen.get(rel)
        if rec is None:
            bad("missing", rel); continue
        if e["type"] == "file":
            if not isinstance(rec, CatalogFileRecord):
                bad("type", rel); continue
            try:
                data = vol.read_file(rec)
            except Exception as ex:
                bad("extract_error", f"{rel}: {type(ex).__name__}: {ex}"); continue
            if hashlib.md5(data).hexdigest() != e["md5"] or len(data) != e["size"]:
                bad("data_mismatch", f"{rel}: got {len(data)} bytes, want {e['size']}")
            if e["rsrc_size"]:
                try:
                    r = vol.read_file(rec, resource=True)
                except Exception as ex:
                    bad("rsrc_error", f"{rel}: {ex}"); continue
                if hashlib.md5(r).hexdigest() != e["rsrc_md5"]:
                    bad("rsrc_mismatch", rel)
        elif e["type"] == "dir" and not isinstance(rec, CatalogFolderRecord):
            bad("type", rel)
    problems = [f"{k}: {n} e.g. {samples[k]}" for k, n in errors.items()]
    return f"{info}; {len(man)} entries checked", problems


if __name__ == "__main__":
    summary, problems = check(sys.argv[1], sys.argv[2])
    print(summary)
    for p in problems:
        print("FAIL", p)
    print("ALL OK" if not problems else "FAILED")
    sys.exit(1 if problems else 0)
