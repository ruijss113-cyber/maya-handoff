"""
MAYA Reports Downloader (Fixed) - PAGE COUNT FILTER & AUTO-NAME SEARCH

תיקון עיקרי מול הגרסה הקודמת:
maya.tase.co.il מוגן ע"י Imperva WAF. בקשה ל-/api/... בלי User-Agent של דפדפן
ובלי עוגיות incap (שמתקבלות מביקור בדף הבית) מוחזרת עם 403.
לכן הסשן עושה "חימום" לדף הבית לפני כל קריאת API.
"""

import sys
import io
import os
import time
import re
import datetime
import requests
import urllib.parse

try:
    from pypdf import PdfReader          # pypdf = הגרסה המתוחזקת
except ImportError:
    from PyPDF2 import PdfReader

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "financial_reports")

SITE = "https://maya.tase.co.il"
FILES = "https://mayafiles.tase.co.il"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MIN_PAGES = 100
DEDUPE_BY_YEAR = True   # אותה שנה מופיעה לפעמים פעמיים עם אותו PDF בדיוק


def sanitize(name):
    return re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name).strip()[:150]


def build_session():
    """סשן עם User-Agent אמיתי + עוגיות Imperva מדף הבית."""
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
    try:
        r = session.get(f"{SITE}/he", timeout=30)
        ok = r.status_code == 200 and "incap_ses" in "".join(session.cookies.keys())
        print(f"[warm-up] {r.status_code} | cookies: {len(session.cookies)}"
              f"{'' if ok else '  (!) לא התקבלו עוגיות Imperva'}")
        return ok
    except Exception as e:
        print(f"[warm-up] נכשל: {e}")
        return False


def api(session, method, path, **kw):
    """קריאת API עם ניסיון חוזר וריענון עוגיות במקרה של 403."""
    url = f"{SITE}{path}"
    headers = {"Referer": f"{SITE}/he/reports/finance", "Origin": SITE,
               "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        res = session.request(method, url, headers=headers, timeout=30, **kw)
        if res.status_code == 200:
            return res
        if res.status_code in (403, 429):
            print(f"    [!] HTTP {res.status_code} - מרענן עוגיות ומנסה שוב ({attempt + 1}/3)")
            time.sleep(2 + attempt * 3)
            session.cookies.clear()
            warm_up(session)
            continue
        raise RuntimeError(f"{method} {path} -> HTTP {res.status_code}: {res.text[:300]}")
    raise RuntimeError(f"{method} {path} נחסם אחרי 3 ניסיונות (HTTP {res.status_code})")


def get_company_id(session, company_name):
    print(f"\nSearching for company ID: '{company_name}'...")
    q = urllib.parse.quote(company_name)
    try:
        data = api(session, "GET", f"/api/v1/companies/autocomplete?search={q}&take=8").json()
    except Exception as e:
        print(f"  [-] Failed to search: {e}")
        return None, None

    companies = [d for d in data if d.get("type") == "COMPANY"] or data
    if not companies:
        print(f"  [-] No match found for '{company_name}'")
        return None, None

    best = companies[0]
    cid, name = best.get("key"), best.get("value")
    print(f"  [+] Found Match: {name} (ID: {cid})")
    return cid, sanitize(name)


def pdf_page_count(filepath):
    """מספר עמודים, או None אם לא ניתן לקרוא."""
    try:
        with open(filepath, 'rb') as f:
            return len(PdfReader(f, strict=False).pages)
    except Exception as e:
        print(f"      [?] ERROR reading PDF {os.path.basename(filepath)}: {e}")
        return None


def keep_pdf(filepath):
    """True = לשמור. קובץ שלא ניתן לקרוא נשמר (ליתר ביטחון)."""
    n = pdf_page_count(filepath)
    if n is None:
        return True
    if n < MIN_PAGES:
        print(f"      [!] DELETE {os.path.basename(filepath)} - only {n} pages (< {MIN_PAGES}).")
        return False
    print(f"      [V] VERIFIED {os.path.basename(filepath)} - {n} pages.")
    return True


def pdf_url_for(rid):
    start = (int(rid) // 1000) * 1000 + 1
    return f"{FILES}/rpdf/{start}-{start + 999}/P{rid}-00.pdf"


PAGE_SIZE = 30  # המקסימום שה-API מאפשר
MIN_YEAR = 2000  # ה-API דוחה fromYear קטן מ-2000


def fetch_reports(session, cid, from_year, to_year):
    """שולף את כל העמודים (ה-API מגביל ל-30 רשומות לעמוד)."""
    from_year = max(int(from_year), MIN_YEAR)
    out, seen, page = [], set(), 1
    while True:
        payload = {"pageSize": PAGE_SIZE, "pageNumber": page, "by": "company",
                   "companyId": cid,
                   "period": 4,  # 4 = שנתי
                   "fromYear": from_year, "toYear": int(to_year)}
        data = api(session, "POST", "/api/v1/reports/finance", json=payload).json()
        if not isinstance(data, list) or not data:
            break
        for row in data:
            if row.get("id") not in seen:
                seen.add(row.get("id"))
                out.append(row)
        if len(data) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.4)
    return out


def download_reports_for_company(session, cid, company_name,
                                 from_year=2000, to_year=datetime.date.today().year,
                                 limit=None):
    """limit = כמה דוחות להוריד לכל היותר (החדשים ביותר). None = הכל."""
    print(f"\n[1] Fetching Reports for {company_name} ({from_year}-{to_year})...")
    try:
        data = fetch_reports(session, cid, from_year, to_year)
    except Exception as e:
        print(f"  [-] FAILED to fetch API: {e}")
        return

    if not data:
        print("  [-] No reports found matching criteria.")
        return
    print(f"  [+] Found {len(data)} items!")

    if DEDUPE_BY_YEAR:
        best = {}
        for r in sorted(data, key=lambda r: r.get("publishDate") or "", reverse=True):
            best.setdefault(r.get("year"), r)
        if len(best) < len(data):
            print(f"  [i] {len(data) - len(best)} כפילויות שנה הוסרו -> {len(best)} דוחות")
        data = sorted(best.values(), key=lambda r: r.get("year") or 0, reverse=True)

    data = sorted(data, key=lambda r: r.get("year") or 0, reverse=True)
    if limit:
        data = data[:limit]
        print(f"  [i] מוריד רק {len(data)} דוחות אחרונים")

    company_dir = os.path.join(BASE_OUTPUT_DIR, company_name)
    os.makedirs(company_dir, exist_ok=True)

    print(f"[2] Downloading {len(data)} PDFs into '{company_dir}' and checking pages...")
    downloaded = failed = removed_short = 0

    for r in data:
        rid = r.get("id")
        if rid is None:
            continue
        rid, year = str(rid), str(r.get("year", ""))
        filepath = os.path.join(company_dir, sanitize(f"{company_name}_Annual_Report_{year}_{rid}") + ".pdf")

        if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
            print(f"  [{rid}] {year} EXISTS")
            if keep_pdf(filepath):
                downloaded += 1
            else:
                os.remove(filepath)
                removed_short += 1
            continue

        print(f"  [{rid}] Downloading {year}...", end=" ", flush=True)
        tmp = filepath + ".part"
        try:
            resp = session.get(pdf_url_for(rid), timeout=60, stream=True,
                               headers={"Accept": "application/pdf,*/*", "Referer": f"{SITE}/"})
            if resp.status_code != 200:
                print(f"HTTP {resp.status_code}")
                failed += 1
                continue
            if "pdf" not in (resp.headers.get("content-type") or "").lower():
                print(f"NOT A PDF ({resp.headers.get('content-type')})")
                failed += 1
                continue

            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(65536):
                    if chunk:
                        f.write(chunk)

            if os.path.getsize(tmp) <= 1000:
                os.remove(tmp)
                print("TOO SMALL")
                failed += 1
                continue

            os.replace(tmp, filepath)
            print(f"OK ({os.path.getsize(filepath) / 1048576:.1f} MB)")
            if keep_pdf(filepath):
                downloaded += 1
            else:
                os.remove(filepath)
                removed_short += 1
        except Exception as e:
            print(f"ERROR: {e}")
            failed += 1
            if os.path.exists(tmp):
                os.remove(tmp)
        finally:
            time.sleep(0.5)

    print(f"COMPLETE for {company_name}: {downloaded} kept, "
          f"{removed_short} removed (<{MIN_PAGES} pages), {failed} failed")


def main():
    print("=" * 60)
    print("MAYA Multi-Company Reports Downloader (+PAGE LIMIT & AUTO-SEARCH)")
    print("=" * 60)

    os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)

    # ---------------------------------------------------------
    # כאן אתה מכניס את שמות החברות! אין צורך במספרים.
    # ---------------------------------------------------------
    companies_to_download = [
        "מליסרון",
        "קבוצת עזריאלי",
        "אלוני חץ",
    ]

    session = build_session()

    for cname in companies_to_download:
        cid, official_name = get_company_id(session, cname)
        if cid:
            download_reports_for_company(session, cid, official_name)
        time.sleep(2)


if __name__ == "__main__":
    main()
