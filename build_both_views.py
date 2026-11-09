"""
בונה שתי חלוקות מקבילות של אותם דוחות:
  לפי רבעון\<שנה> <תקופה>\...
  לפי חברה\<פילוח>\<מנפיק> - <חברה>\...
"""
import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maya_bulk_downloader as M

PAT = re.compile(r"^(\d+)_(.*)_(\d{4})_(Q1|Q2|Q3|Annual)_(\d+)\.pdf$")
LABEL = {"Q1": "רבעון 1", "Q2": "רבעון 2", "Q3": "רבעון 3", "Annual": "שנתי"}
BY_Q = "לפי רבעון"
BY_C = "לפי חברה"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--excel", required=True)
    args = ap.parse_args()
    root = args.root

    seg_of = {c["issuer"]: c["segment"] for c in M.load_excel(args.excel)}

    # כל קובצי ה-PDF, איפה שלא יהיו כרגע
    found = []
    for dirpath, _d, names in os.walk(root):
        for fn in names:
            m = PAT.match(fn)
            if m:
                found.append((os.path.join(dirpath, fn), m))
    print(f"נמצאו {len(found)} קבצים", flush=True)

    qroot = os.path.join(root, BY_Q)
    croot = os.path.join(root, BY_C)

    # שלב 1 - העברה למבנה 'לפי רבעון'
    moved = 0
    for src, m in found:
        dst_dir = os.path.join(qroot, f"{m.group(3)} {LABEL[m.group(4)]}")
        dst = os.path.join(dst_dir, os.path.basename(src))
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        os.makedirs(dst_dir, exist_ok=True)
        if os.path.exists(dst):
            os.remove(src)
            continue
        shutil.move(src, dst)
        moved += 1
        if moved % 500 == 0:
            print(f"   ...הועברו {moved}", flush=True)
    print(f"[1] לפי רבעון: הועברו {moved}", flush=True)

    # שלב 2 - העתקה למבנה 'לפי חברה'
    copied = existed = 0
    for dirpath, _d, names in os.walk(qroot):
        for fn in names:
            m = PAT.match(fn)
            if not m:
                continue
            iss = int(m.group(1))
            seg = M.sanitize(seg_of.get(iss, "לא ידוע"))
            comp = M.sanitize(m.group(2))
            dst_dir = os.path.join(croot, seg, f"{iss} - {comp}")
            dst = os.path.join(dst_dir, fn)
            src = os.path.join(dirpath, fn)
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                existed += 1
                continue
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
            if copied % 250 == 0:
                print(f"   ...הועתקו {copied}", flush=True)
    print(f"[2] לפי חברה: הועתקו {copied}, כבר היו {existed}", flush=True)

    # שלב 3 - ניקוי תיקיות ריקות שנשארו מהמבנה הישן
    removed = 0
    for dirpath, _d, _f in sorted(os.walk(root), key=lambda t: -len(t[0])):
        ap_ = os.path.abspath(dirpath)
        if ap_ in (os.path.abspath(root), os.path.abspath(qroot), os.path.abspath(croot)):
            continue
        if ap_.startswith(os.path.abspath(qroot)) or ap_.startswith(os.path.abspath(croot)):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed += 1
        except OSError:
            pass
    print(f"[3] נמחקו {removed} תיקיות ריקות")

    nq = sum(len([f for f in fs if f.lower().endswith(".pdf")])
             for _r, _d, fs in os.walk(qroot))
    nc = sum(len([f for f in fs if f.lower().endswith(".pdf")])
             for _r, _d, fs in os.walk(croot))
    print(f"\nלפי רבעון: {nq} קבצים ב-{len(os.listdir(qroot))} תיקיות")
    print(f"לפי חברה : {nc} קבצים ב-{len(os.listdir(croot))} פילוחים")


if __name__ == "__main__":
    main()
