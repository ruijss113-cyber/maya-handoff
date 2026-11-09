"""
בדיקת איכות + תיקון בחירת דוחות.

הבעיה: מאיה מפרסמת באותו (שנה, רבעון) גם מצגות משקיעים, תיקונים
והודעות קצרות. הורדה לפי "הפרסום החדש ביותר" תופסת לפעמים מצגת
של 11 עמודים במקום דוח כספי של 300 עמודים.

אין שדה כותרת ב-API, ולכן ההבחנה היחידה האמינה היא מספר עמודים
בפועל. הסקריפט מוריד את כל המועמדים לאותו סלוט, סופר עמודים,
ומחליף אם נמצא מועמד טוב יותר.

הרצה:
    python maya_quality_check.py --root "D:\\maya_reports"
    python maya_quality_check.py --root "D:\\maya_reports" --mode all
    python maya_quality_check.py --root "D:\\maya_reports" --dry-run
"""
import argparse
import csv
import io
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maya_bulk_downloader as M
from pypdf import PdfReader

TAG2PER = {"Q1": 1, "Q2": 2, "Q3": 3, "Annual": 4}
FNAME = re.compile(r"^(\d+)_(.*)_(\d{4})_(Q1|Q2|Q3|Annual)_(\d+)\.pdf$")
MIN_GAIN = 1.5     # מחליפים רק אם המועמד גדול משמעותית
ABS_FLOOR = 25     # פחות מזה תמיד חשוד


def pages_of(path_or_bytes):
    try:
        src = (io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes)
               else path_or_bytes)
        return len(PdfReader(src).pages)
    except Exception:
        return -1


def scan(root):
    """מחזיר רשימת רשומות לכל PDF שנמצא בעץ."""
    out = []
    for dirpath, _d, files in os.walk(root):
        for fn in files:
            m = FNAME.match(fn)
            if not m:
                continue
            out.append({
                "issuer": int(m.group(1)),
                "company": m.group(2),
                "year": int(m.group(3)),
                "tag": m.group(4),
                "report_id": int(m.group(5)),
                "path": os.path.join(dirpath, fn),
            })
    # אם קיימות שתי תצוגות מקבילות, מספיק לבדוק כל סלוט פעם אחת
    uniq = {}
    for r in out:
        uniq.setdefault((r["issuer"], r["year"], r["tag"]), []).append(r)
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="תיקיית הפלט של ההורדה")
    ap.add_argument("--mode", choices=["suspicious", "all"], default="suspicious",
                    help="suspicious = רק חריגים (מהיר) | all = כל סלוט (איטי)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default=None, help="נתיב CSV לדוח (ברירת מחדל: בתוך root)")
    args = ap.parse_args()

    slots = scan(args.root)
    print(f"נסרקו {len(slots)} סלוטים ייחודיים תחת {args.root}")
    if not slots:
        print("[-] לא נמצאו קבצים בתבנית <מנפיק>_<שם>_<שנה>_<תקופה>_<מזהה>.pdf")
        return

    print("סופר עמודים...")
    for i, (key, recs) in enumerate(slots.items(), 1):
        recs[0]["pages"] = pages_of(recs[0]["path"])
        if i % 250 == 0:
            print(f"   ...{i}/{len(slots)}", flush=True)

    # חציון לכל (מנפיק, סוג דוח) כדי לזהות חריגים
    med = {}
    for (iss, _y, tag), recs in slots.items():
        p = recs[0]["pages"]
        if p > 0:
            med.setdefault((iss, tag), []).append(p)
    med = {k: statistics.median(v) for k, v in med.items()}

    targets = []
    for key, recs in slots.items():
        iss, year, tag = key
        p = recs[0]["pages"]
        m = med.get((iss, tag), 0)
        if args.mode == "all" or p < 0 or p < ABS_FLOOR or (m and p < 0.5 * m):
            targets.append(key)

    print(f"\nלבדיקה מול מאיה: {len(targets)} סלוטים "
          f"(מצב={args.mode})")
    if not targets:
        print("[V] לא נמצאו חריגים.")
        return

    s = M.build_session()
    rows, replaced = [], 0

    for n, key in enumerate(sorted(targets), 1):
        iss, year, tag = key
        recs = slots[key]
        cur = recs[0]
        print(f"\n[{n}/{len(targets)}] {iss} {cur['company'][:28]} {year} {tag}"
              f"  (כעת {cur['pages']} עמ')", flush=True)
        try:
            items = [x for x in M.fetch_period(s, iss, TAG2PER[tag], year, year)
                     if str(x.get("year")) == str(year)]
        except Exception as e:
            print(f"   [-] שגיאת API: {e}")
            continue

        best = (cur["pages"], None, cur["report_id"])
        for it in items:
            rid = int(it.get("id") or 0)
            if not rid or rid == cur["report_id"]:
                continue
            try:
                r = s.get(M.pdf_url_for(str(rid)), timeout=60)
            except Exception:
                continue
            if r.status_code != 200 or r.content[:5] != b"%PDF-":
                continue
            p = pages_of(r.content)
            print(f"   מועמד {rid}: {p} עמ'")
            if p > best[0]:
                best = (p, r.content, rid)

        if best[1] is not None and best[0] >= max(cur["pages"] * MIN_GAIN, ABS_FLOOR):
            rows.append({"issuer": iss, "company": cur["company"], "year": year,
                         "tag": tag, "old_id": cur["report_id"],
                         "old_pages": cur["pages"], "new_id": best[2],
                         "new_pages": best[0]})
            if args.dry_run:
                print(f"   [DRY] היה מוחלף: {cur['pages']} -> {best[0]} עמ'")
            else:
                # כותבים לכל העותקים של הסלוט (שתי התצוגות)
                for rec in recs:
                    newp = os.path.join(
                        os.path.dirname(rec["path"]),
                        M.sanitize(f"{iss}_{cur['company']}_{year}_{tag}_{best[2]}") + ".pdf")
                    tmp = newp + ".part"
                    with open(tmp, "wb") as f:
                        f.write(best[1])
                    os.replace(tmp, newp)
                    if os.path.abspath(newp) != os.path.abspath(rec["path"]):
                        os.remove(rec["path"])
                replaced += 1
                print(f"   [V] הוחלף: {cur['pages']} -> {best[0]} עמ'")
        else:
            print("   [=] אין מועמד טוב יותר")

    path = args.report or os.path.join(args.root, "_quality_report.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["issuer", "company", "year", "tag",
                                          "old_id", "old_pages", "new_id", "new_pages"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'=' * 60}")
    print(f"הוחלפו {replaced} דוחות | דוח: {path}")


if __name__ == "__main__":
    main()
