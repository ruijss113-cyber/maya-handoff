"""
מסדר מחדש את מאגר הדוחות: תיקייה לכל רבעון, וכל החברות בתוכה.

לפני:  פילוח / מנפיק - חברה / מנפיק_חברה_שנה_תקופה_מזהה.pdf
אחרי:  2010 רבעון 1 / מנפיק_חברה_2010_Q1_מזהה.pdf
"""
import argparse
import os
import re
import shutil

PAT = re.compile(r"^(\d+)_(.*)_(\d{4})_(Q1|Q2|Q3|Annual)_(\d+)\.pdf$")
LABEL = {"Q1": "רבעון 1", "Q2": "רבעון 2", "Q3": "רבעון 3", "Annual": "שנתי"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = args.root

    files = []
    for dirpath, _dirs, names in os.walk(root):
        for fn in names:
            m = PAT.match(fn)
            if m:
                files.append((os.path.join(dirpath, fn), m))
    print(f"נמצאו {len(files)} קבצים")

    moved = same = clash = 0
    buckets = {}
    for src, m in files:
        year, tag = m.group(3), m.group(4)
        folder = os.path.join(root, f"{year} {LABEL[tag]}")
        dst = os.path.join(folder, os.path.basename(src))
        buckets[folder] = buckets.get(folder, 0) + 1
        if os.path.abspath(src) == os.path.abspath(dst):
            same += 1
            continue
        if args.dry_run:
            moved += 1
            continue
        os.makedirs(folder, exist_ok=True)
        if os.path.exists(dst):
            if os.path.getsize(dst) == os.path.getsize(src):
                os.remove(src)
                clash += 1
                continue
            base, ext = os.path.splitext(dst)
            n = 2
            while os.path.exists(f"{base}__{n}{ext}"):
                n += 1
            dst = f"{base}__{n}{ext}"
        shutil.move(src, dst)
        moved += 1

    if not args.dry_run:
        # מוחקים תיקיות שנשארו ריקות
        for dirpath, _d, _f in sorted(
                (t for t in os.walk(root)), key=lambda t: -len(t[0])):
            if os.path.abspath(dirpath) == os.path.abspath(root):
                continue
            if os.path.basename(dirpath) in buckets:
                continue
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass

    print(f"הועברו {moved} | היו במקום {same} | כפולים שנמחקו {clash}")
    print(f"\n{len(buckets)} תיקיות רבעון:")
    for k in sorted(buckets):
        print(f"   {os.path.basename(k):<16} {buckets[k]:>4} דוחות")


if __name__ == "__main__":
    main()
