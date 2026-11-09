"""
MAYA Bulk Reports Downloader
============================
מוריד דוחות כספיים (רבעוניים + שנתיים) מאתר מאיה לפי רשימת חברות באקסל.

מבנה הפלט:
    OUT / פילוח / "<מספר מנפיק> - <שם חברה>" / "<מנפיק>_<שם>_<שנה>_<תקופה>_<מזהה>.pdf"

הערה טכנית:
maya.tase.co.il מוגן ע"י Imperva WAF. קריאה ל-/api/... בלי User-Agent של דפדפן
ובלי עוגיות incap (שמתקבלות מביקור בדף הבית) מוחזרת עם 403 - לכן warm_up().

הרצה:
    python maya_bulk_downloader.py --excel "רשימה.xlsx" --out "G:/My Drive/דוחות"
    python maya_bulk_downloader.py --excel "רשימה.xlsx" --out "..." --dry-run
    python maya_bulk_downloader.py --excel "רשימה.xlsx" --out "..." --only 226,323
"""

import sys
import io
import os
import re
import csv
import time
import sqlite3
import argparse
import urllib.parse

import requests
import pandas as pd

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ============================== הגדרות ==============================
SITE = "https://maya.tase.co.il"
FILES = "https://mayafiles.tase.co.il"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PAGE_SIZE = 30      # מקסימום שהשרת מאפשר (50/100 -> HTTP 400)
MIN_YEAR = 2000     # השרת דוחה fromYear קטן יותר

PERIODS = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Annual"}   # 4 = דוח שנתי (מכיל את Q4)

SLEEP_PDF = 0.4     # השהייה בין הורדות
SLEEP_API = 0.3
MIN_BYTES = 2000    # קטן מזה = לא דוח אמיתי
# ====================================================================


def sanitize(name):
    return re.sub(r'[<>:"/\\|?*\n\r\t]', '_', str(name)).strip().rstrip('. ')[:110]


# ---------------------------------------------------------------- HTTP
def build_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "application/json, text/plain, */*",
        "X-Maya-With": "B",
    })
    warm_up(s)
    return s


def warm_up(session):
    """מביא עוגיות Imperva. בלי זה כל קריאת API מוחזרת 403."""
    try:
        r = session.get(f"{SITE}/he", timeout=30)
        ok = r.status_code == 200 and any(k.startswith("incap_ses") for k in session.cookies.keys())
        if not ok:
            print(f"    [warm-up] {r.status_code} - לא התקבלו עוגיות Imperva")
        return ok
    except Exception as e:
        print(f"    [warm-up] נכשל: {e}")
        return False


def api(session, method, path, **kw):
    headers = {"Referer": f"{SITE}/he/reports/finance", "Origin": SITE,
               "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
               "Sec-Fetch-Site": "same-origin"}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    res = None
    for attempt in range(4):
        try:
            res = session.request(method, f"{SITE}{path}", headers=headers, timeout=40, **kw)
        except requests.RequestException as e:
            print(f"    [!] רשת: {e} - ניסיון {attempt + 1}/4")
            time.sleep(3 + attempt * 4)
            continue
        if res.status_code == 200:
            return res
        if res.status_code in (403, 429, 502, 503):
            print(f"    [!] HTTP {res.status_code} - מרענן עוגיות ({attempt + 1}/4)")
            time.sleep(3 + attempt * 5)
            session.cookies.clear()
            warm_up(session)
            continue
        raise RuntimeError(f"{method} {path} -> HTTP {res.status_code}: {res.text[:200]}")
    code = res.status_code if res is not None else "network"
    raise RuntimeError(f"{method} {path} נכשל אחרי 4 ניסיונות ({code})")


# ---------------------------------------------------------- Maya lookups
def company_details(session, issuer_no):
    try:
        return api(session, "GET", f"/api/v1/companies/{issuer_no}/details").json()
    except Exception:
        return {}


def fetch_period(session, issuer_no, period, from_year, to_year):
    """כל הדוחות של תקופה אחת, על פני כל העמודים."""
    from_year = max(int(from_year), MIN_YEAR)
    out, seen, page = [], set(), 1
    while True:
        data = api(session, "POST", "/api/v1/reports/finance", json={
            "pageSize": PAGE_SIZE, "pageNumber": page, "by": "company",
            "companyId": int(issuer_no), "period": period,
            "fromYear": from_year, "toYear": int(to_year)}).json()
        if not isinstance(data, list) or not data:
            break
        for row in data:
            if row.get("id") not in seen:
                seen.add(row["id"])
                row["_p"] = period
                out.append(row)
        if len(data) < PAGE_SIZE:
            break
        page += 1
        time.sleep(SLEEP_API)
    return out


def pdf_url_for(rid):
    # התיקיות בשרת הן טווחים סגורים 1..1000, 1001..2000 וכו'.
    # לכן מזהה שמתחלק ב-1000 שייך לטווח הקודם, לא לזה שמתחיל אחריו.
    start = ((int(rid) - 1) // 1000) * 1000 + 1
    return f"{FILES}/rpdf/{start}-{start + 999}/P{rid}-00.pdf"


# ------------------------------------------------------------- Excel
def parse_period_cell(v):
    """'Q1 2010' -> (2010, 1). 'Q4 2017' -> (2017, 4) = דוח שנתי."""
    m = re.match(r"\s*Q?\s*([1-4])\s+(\d{4})\s*$", str(v).strip())
    if not m:
        return None
    return int(m.group(2)), int(m.group(1))


def seq(year, period):
    """אינדקס רציף כדי להשוות טווחים."""
    return year * 4 + (period - 1)


def load_excel(path):
    df = pd.read_excel(path, sheet_name=0, header=0)
    df = df.iloc[:, :5]
    df.columns = ["segment", "issuer", "name", "first", "last"]
    df = df.dropna(subset=["issuer", "first", "last"]).copy()
    df["issuer"] = df["issuer"].astype(int)

    rows, bad = [], []
    for r in df.itertuples():
        f, l = parse_period_cell(r.first), parse_period_cell(r.last)
        if not f or not l or seq(*f) > seq(*l):
            bad.append((r.issuer, r.name, r.first, r.last))
            continue
        wanted = []
        for n in range(seq(*f), seq(*l) + 1):
            wanted.append((n // 4, n % 4 + 1))
        rows.append({"segment": str(r.segment).strip(), "issuer": r.issuer,
                     "name": str(r.name).strip(), "wanted": wanted,
                     "from_year": f[0], "to_year": l[0]})
    if bad:
        print(f"[!] {len(bad)} שורות לא נפרסו:")
        for b in bad[:10]:
            print("   ", b)
    return rows


# ------------------------------------------------------------ manifest
def open_db(out_dir):
    db = sqlite3.connect(os.path.join(out_dir, "_maya_manifest.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS files (
        report_id INTEGER PRIMARY KEY, issuer INTEGER, company TEXT, segment TEXT,
        year INTEGER, period INTEGER, period_tag TEXT,
        path TEXT, bytes INTEGER, status TEXT, ts TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS missing (
        issuer INTEGER, company TEXT, year INTEGER, period_tag TEXT,
        PRIMARY KEY (issuer, year, period_tag))""")
    db.commit()
    return db


def record(db, **kw):
    db.execute("""INSERT OR REPLACE INTO files
        (report_id, issuer, company, segment, year, period, period_tag, path, bytes, status, ts)
        VALUES (:report_id,:issuer,:company,:segment,:year,:period,:period_tag,:path,:bytes,:status,
                datetime('now'))""", kw)
    db.commit()


# ------------------------------------------------------------ download
def download_pdf(session, rid, filepath):
    """מחזיר (status, bytes). כתיבה אטומית דרך .part"""
    tmp = filepath + ".part"
    try:
        resp = session.get(pdf_url_for(rid), timeout=180, stream=True,
                           headers={"Accept": "application/pdf,*/*", "Referer": f"{SITE}/"})
        if resp.status_code != 200:
            return f"http_{resp.status_code}", 0
        if "pdf" not in (resp.headers.get("content-type") or "").lower():
            return "not_pdf", 0

        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(131072):
                if chunk:
                    f.write(chunk)

        size = os.path.getsize(tmp)
        if size < MIN_BYTES:
            os.remove(tmp)
            return "too_small", size
        with open(tmp, "rb") as f:
            if f.read(5) != b"%PDF-":
                os.remove(tmp)
                return "corrupt", size

        os.replace(tmp, filepath)
        return "ok", size
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return f"error:{type(e).__name__}", 0


# ------------------------------------------------------------ per company
def process_company(session, db, spec, out_dir, dry_run=False):
    iss, name, seg = spec["issuer"], spec["name"], spec["segment"]
    wanted = set(spec["wanted"])

    det = company_details(session, iss)
    official = det.get("longName") or det.get("name") or name
    folder = os.path.join(out_dir, sanitize(seg), f"{iss} - {sanitize(official)}")

    print(f"\n{'-' * 72}\n[{iss}] {official}  ({seg})")
    print(f"   מבוקש: {len(wanted)} דוחות | {spec['from_year']}-{spec['to_year']}")

    rows = []
    for p in (1, 2, 3, 4):
        try:
            rows += fetch_period(session, iss, p, spec["from_year"], spec["to_year"])
        except Exception as e:
            print(f"   [-] {PERIODS[p]}: {e}")
        time.sleep(SLEEP_API)

    # לכל משבצת (שנה,תקופה) שומרים את *כל* המועמדים לפי תאריך פרסום יורד.
    # חלק מהרשומות הן הודעות טקסט בלי PDF (חידוש מסחר, תיקון מצבת התחייבויות),
    # ולכן צריך לנסות מועמד אחר כשהראשון מחזיר 404.
    cand = {}
    for r in sorted(rows, key=lambda r: r.get("publishDate") or "", reverse=True):
        k = (r.get("year"), r["_p"])
        if k in wanted:
            cand.setdefault(k, []).append(r)

    gap = sorted(wanted - set(cand))
    if gap:
        print(f"   [i] {len(gap)} לא קיימים במאיה"
              f"{': ' + ', '.join(f'{y} {PERIODS[p]}' for y, p in gap[:6]) if len(gap) <= 6 else ''}")
        if not dry_run:
            db.executemany("INSERT OR REPLACE INTO missing VALUES (?,?,?,?)",
                           [(iss, official, y, PERIODS[p]) for y, p in gap])
            db.commit()

    todo = sorted(cand.items(), key=lambda kv: (-kv[0][0], kv[0][1]))
    print(f"   [+] נמצאו {len(todo)} דוחות")
    if dry_run:
        return {"ok": 0, "skip": len(todo), "fail": 0, "bytes": 0}

    os.makedirs(folder, exist_ok=True)
    st = {"ok": 0, "skip": 0, "fail": 0, "bytes": 0}

    for (year, p), variants in todo:
        tag = PERIODS[p]

        # קובץ שכבר ירד (כל אחד מהמועמדים) - מדלגים
        existing = None
        for v in variants:
            fp_v = os.path.join(folder, sanitize(f"{iss}_{official}_{year}_{tag}_{v['id']}") + ".pdf")
            if os.path.exists(fp_v) and os.path.getsize(fp_v) >= MIN_BYTES:
                existing = fp_v
                break
        if existing:
            st["skip"] += 1
            st["bytes"] += os.path.getsize(existing)
            continue

        # מנסים מועמדים לפי סדר (החדש ביותר קודם) עד שאחד מצליח
        status, size, rid = None, 0, None
        for idx, v in enumerate(variants):
            rid = v["id"]
            fp = os.path.join(folder, sanitize(f"{iss}_{official}_{year}_{tag}_{rid}") + ".pdf")
            status, size = download_pdf(session, rid, fp)
            if status == "ok":
                if idx:
                    print(f"   [OK]   {year} {tag:6} {size / 1048576:6.1f} MB  (מועמד {idx + 1})")
                else:
                    print(f"   [OK]   {year} {tag:6} {size / 1048576:6.1f} MB")
                break
            time.sleep(SLEEP_PDF)

        if status == "ok":
            st["ok"] += 1
            st["bytes"] += size
        else:
            st["fail"] += 1
            print(f"   [FAIL] {year} {tag:6} {status} ({len(variants)} מועמדים)")

        record(db, report_id=rid, issuer=iss, company=official, segment=seg,
               year=year, period=p, period_tag=tag,
               path=fp, bytes=size, status=status)
        time.sleep(SLEEP_PDF)

    print(f"   >> ירדו {st['ok']}, קיימים {st['skip']}, נכשלו {st['fail']}"
          f"  ({st['bytes'] / 1048576:.0f} MB)")
    return st


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true", help="בודק זמינות בלי להוריד")
    ap.add_argument("--only", help="מספרי מנפיק מופרדים בפסיק")
    ap.add_argument("--limit", type=int, help="עצור אחרי N חברות")
    args = ap.parse_args()

    specs = load_excel(args.excel)
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        specs = [s for s in specs if s["issuer"] in keep]
    if args.limit:
        specs = specs[:args.limit]

    total_wanted = sum(len(s["wanted"]) for s in specs)
    print("=" * 72)
    print(f"MAYA Bulk Downloader{'  [DRY RUN]' if args.dry_run else ''}")
    print(f"{len(specs)} חברות | {total_wanted} דוחות מבוקשים")
    print(f"יעד: {args.out}")
    print("=" * 72)

    os.makedirs(args.out, exist_ok=True)
    db = open_db(args.out)
    session = build_session()

    grand = {"ok": 0, "skip": 0, "fail": 0, "bytes": 0}
    t0 = time.time()
    for i, spec in enumerate(specs, 1):
        print(f"\n### {i}/{len(specs)}", end="")
        try:
            for k, v in process_company(session, db, spec, args.out, args.dry_run).items():
                grand[k] += v
        except Exception as e:
            print(f"\n   [!!] חברה {spec['issuer']} נכשלה: {e}")
        done = grand["ok"] + grand["skip"] + grand["fail"]
        el = time.time() - t0
        if done and i < len(specs):
            eta = el / i * (len(specs) - i)
            print(f"   [~] {grand['bytes'] / 1073741824:.1f} GB | "
                  f"חלף {el / 60:.0f} דק' | נותר ~{eta / 60:.0f} דק'")

    print("\n" + "=" * 72)
    print(f"TOTAL: {grand['ok']} ירדו, {grand['skip']} כבר היו, {grand['fail']} נכשלו")
    print(f"נפח: {grand['bytes'] / 1073741824:.2f} GB | זמן: {(time.time() - t0) / 60:.0f} דק'")

    csv_path = os.path.join(args.out, "_missing_reports.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["issuer", "company", "year", "period"])
        w.writerows(db.execute("SELECT * FROM missing ORDER BY issuer, year, period_tag"))
    print(f"דוח חוסרים: {csv_path}")
    db.close()


if __name__ == "__main__":
    main()
