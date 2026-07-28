import ast
import base64
import os
import json
import unicodedata
import concurrent.futures
import cloudscraper
import gspread
from google.oauth2.service_account import Credentials
import xml.etree.ElementTree as ET
from datetime import datetime
from fastapi import FastAPI, Response, HTTPException

app = FastAPI()

# Configuration
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
SHEET_ID = '1B93nJwvS591zZ-x7nGCwwPkOcbnH4ZifApO_QSQztzg'
XMLNS = "http://www.sitemaps.org/schemas/sitemap/0.9"
NEWS_NS = "http://www.google.com/schemas/sitemap-news/0.9"

# Live production Google-News sitemaps (source of truth for recently published
# URLs). Fetched through Cloudflare. Entries from every source are merged and
# de-duplicated by URL.
PROD_SITEMAP_URLS = [
    "https://www.pagesjaunes.fr/pagesconseils/sitemaps/sitemap-news.xml",
    "https://www.pagesjaunes.fr/que-faire/sitemaps/sitemap-news.xml",
]

# The crawl runs off-request (triggered by a scheduler hitting /refresh) and
# writes the result here; /sitemap.xml only serves this file, so the endpoint
# never blocks on the slow Cloudflare crawl.
SITEMAP_CACHE_PATH = os.environ.get("SITEMAP_CACHE_PATH", "sitemap-cache.xml")

# --- Resilient parsing configuration -------------------------------------
# The sheet is edited by humans who sometimes delete the header row, rename
# columns, or change casing/accents. Instead of relying on exact column names
# we map each logical field to a list of aliases, matched on a *normalized*
# form (lowercase, no accents, no punctuation), and we auto-detect which row
# is the header instead of assuming it is row 1.

# logical field -> list of accepted header aliases (normalized form)
FIELD_ALIASES = {
    'url':    ['url article', 'url', 'lien', 'lien article', 'adresse article', 'adresse', 'article'],
    'date':   ['date de mep', 'date mep', 'date de mise en ligne', 'date de publication', 'date', 'mep'],
    'statut': ['statut', 'status', 'etat'],
    'plage':  ['plage horaire', 'plage', 'horaire', 'creneau', 'moment de publication', 'moment'],
}

# Fields that MUST be present for a row to be usable as an article.
REQUIRED_FIELDS = ['url', 'statut']

# Fixed column positions (0-based) used ONLY as a last resort when a worksheet
# has NO recognizable header row (e.g. a user deleted it). Reflects the current
# sheet template. If the template layout changes, update these indexes.
# Observed layout: date=col 0, plage=col 1, statut=col 2, url=col 7.
FALLBACK_COLUMNS = {'date': 0, 'plage': 1, 'statut': 2, 'url': 7}

# Accepted statuses, stored in normalized form so 'Programmé ', 'publie',
# 'PUBLIÉ' etc. all match.
TARGET_STATUSES = {'programme', 'publie'}


def normalize(value):
    """Lowercase, strip accents and punctuation, collapse whitespace.

    Used both for header matching and for status comparison so that human
    typos in casing/accents/spacing do not break the parsing."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = ''.join(c if (c.isalnum() or c.isspace()) else ' ' for c in text)
    return ' '.join(text.split())


def match_field(header_cell):
    """Return the logical field a header cell maps to, or None."""
    norm = normalize(header_cell)
    if not norm:
        return None
    # 1) exact alias match takes precedence (most specific)
    for field, aliases in FIELD_ALIASES.items():
        if norm in aliases:
            return field
    # 2) substring match either way (e.g. 'url de l article' contains 'url')
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in norm or norm in alias:
                return field
    return None


def find_header_row(rows, max_scan=15):
    """Scan the first rows and return (header_index, colmap).

    colmap maps each detected logical field to its column index. Returns
    (None, None) if no row looks like a header (all required fields present)."""
    best = None  # (score, index, colmap)
    for idx, row in enumerate(rows[:max_scan]):
        colmap = {}
        for col_idx, cell in enumerate(row):
            field = match_field(cell)
            if field and field not in colmap:  # first occurrence wins
                colmap[field] = col_idx
        if all(f in colmap for f in REQUIRED_FIELDS):
            score = len(colmap)
            if best is None or score > best[0]:
                best = (score, idx, colmap)
    if best:
        return best[1], best[2]
    return None, None

# Initialize scraper
scraper = cloudscraper.create_scraper()

def get_credentials():
    """Retrieves credentials from environment variable (JSON or Python dict string)."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        # Fallback for local development if file exists
        local_creds = '../creds/solocal-poc-f9a485d4ac05.json'
        if os.path.exists(local_creds):
             return Credentials.from_service_account_file(local_creds, scopes=SCOPES)
        raise ValueError("Environment variable GOOGLE_CREDENTIALS_JSON is not set.")
    
    # Try decoding base64 first (Most robust method)
    base64_error = None
    try:
        # Check if it looks like base64 (no curly braces at start)
        cleaned_val = creds_json.strip().replace('\n', '').replace('\r', '').replace(' ', '')
        if not cleaned_val.startswith("{"):
            decoded_bytes = base64.b64decode(cleaned_val)
            try:
                decoded_str = decoded_bytes.decode('utf-8')
            except UnicodeDecodeError:
                # Fallback for weird encoding artifacts
                decoded_str = decoded_bytes.decode('latin-1')
            return Credentials.from_service_account_info(json.loads(decoded_str), scopes=SCOPES)
    except Exception as e:
        base64_error = str(e)
        print(f"Base64 decoding failed: {e}") # Log for debugging

    try:
        # Try standard JSON parsing
        creds_info = json.loads(creds_json)
    except json.JSONDecodeError:
        # Fallback: Try parsing as a Python dictionary (single quotes)
        try:
            # Handle newlines in private key which might cause literal_eval to fail
            clean_json = creds_json.replace('\n', '\\n') 
            creds_info = ast.literal_eval(clean_json)
        except (ValueError, SyntaxError):
            # Last resort: simplistic manual replacement for common issues
            try:
                # Replace single quotes with double quotes (imperfect but helps)
                # and ensure control characters are escaped
                clean_json = creds_json.replace("'", '"').replace('\n', '\\n')
                creds_info = json.loads(clean_json)
            except Exception as e:
                import traceback
                traceback.print_exc()
                
                # construct detailed error message
                msg = f"Failed to parse credentials. \nJSON Error: {e}. \nBase64 Error: {base64_error}. \nFirst 20 chars: {creds_json[:20]!r}"
                raise ValueError(msg)
            
    return Credentials.from_service_account_info(creds_info, scopes=SCOPES)

def get_sheet_data():
    """Authenticates and fetches normalized records from ALL worksheets.

    Reads the raw value grid (not get_all_records) so we can auto-detect the
    header row and map columns by alias. Each returned record uses stable
    logical keys ('url', 'date', 'statut', 'plage') regardless of how the
    human-facing columns are named. Worksheets with no recognizable header
    are skipped and logged rather than corrupting the output."""
    try:
        creds = get_credentials()
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID)

        all_records = []
        worksheets = sheet.worksheets()
        stats = {'worksheets': len(worksheets), 'header_ok': 0,
                 'header_fallback': 0, 'rows': 0}
        print(f"Found {len(worksheets)} worksheets. Processing...")

        for ws in worksheets:
            values = ws.get_all_values()
            if not values:
                continue

            header_idx, colmap = find_header_row(values)
            if colmap is None:
                # No header found (likely deleted): fall back to fixed column
                # positions and read from the very first row (no header to skip).
                colmap = FALLBACK_COLUMNS
                data_start = 0
                stats['header_fallback'] += 1
                print(f"WARN: worksheet '{ws.title}' - no header detected, "
                      f"using fixed column fallback {colmap}")
            else:
                data_start = header_idx + 1
                stats['header_ok'] += 1
                print(f"Worksheet '{ws.title}': header at row {header_idx + 1}, "
                      f"columns {colmap}")

            for row in values[data_start:]:
                def cell(field):
                    ci = colmap.get(field)
                    if ci is None or ci >= len(row):
                        return ''
                    return str(row[ci]).strip()

                record = {field: cell(field) for field in FIELD_ALIASES}
                # ignore fully empty trailing rows
                if not any(record.values()):
                    continue
                all_records.append(record)
                stats['rows'] += 1

        print(f"Sheet parse summary: {stats}")
        return all_records
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error fetching data: {repr(e)}")
        raise

def validate_url(url):
    """Checks if the URL returns 200 OK using cloudscraper."""
    try:
        response = scraper.get(url)
        print(f"Validation: {url} -> {response.status_code}") # DEBUG
        return response.status_code == 200
    except Exception as e:
        print(f"Skipping {url} - Error: {e}")
        return False

def parse_date(date_str):
    """Converts DD/MM/YYYY or French format (jeudi 12 février 2026) to YYYY-MM-DD."""
    if not date_str:
        return None
        
    try:
        # Try numeric first (legacy format)
        return datetime.strptime(str(date_str).strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
        
    try:
        # Try French text format: "jeudi 12 février 2026"
        # We need a custom mapping because locales might not be installed in the slim docker image
        lower_str = str(date_str).strip().lower()
        
        # Remove day name if present (e.g. "jeudi ")
        parts = lower_str.split()
        
        # Handle "12 février 2026" or "jeudi 12 février 2026"
        if len(parts) == 4: # day_name day_num month_name year
             parts = parts[1:] # Drop day name
             
        if len(parts) != 3:
             return date_str # Return raw if format not recognized
             
        day, month_name, year = parts
        
        month_map = {
            'janvier': '01', 'février': '02', 'mars': '03', 'avril': '04',
            'mai': '05', 'juin': '06', 'juillet': '07', 'août': '08',
            'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12'
        }
        
        month_num = month_map.get(month_name)
        if not month_num:
             return date_str
             
        # Normalize day (e.g. 5 -> 05)
        day = day.zfill(2)
        
        return f"{year}-{month_num}-{day}"
        
    except Exception:
        return date_str

def build_sitemap_xml(data):
    """Builds the sitemap XML bytes from normalized records.

    Shared by the HTTP endpoint and the local CLI dump so both produce
    identical output."""
    urlset = ET.Element("urlset", xmlns=XMLNS)
    urlset.set("xmlns:news", "http://www.google.com/schemas/sitemap-news/0.9")

    count = 0
    skipped = 0
    if data:
        for row in data:
            url_val = row.get('url')
            status = row.get('statut')

            if normalize(status) not in TARGET_STATUSES:
                skipped += 1
                continue

            if not url_val:
                skipped += 1
                print("Skipping: status matched but no URL in row")
                continue

            # Note: synchronous validation might slow down request.
            # Ideally this should be cached or async.
            # if not validate_url(url_val):
            #    print(f"Skipping: Validation failed for {url_val}")
            #    continue

            url_element = ET.SubElement(urlset, "url")
            loc_element = ET.SubElement(url_element, "loc")
            loc_element.text = str(url_val).strip()

            if row.get('date'):
                raw_date = str(row.get('date')).strip()
                formatted_date = parse_date(raw_date)

                lastmod_element = ET.SubElement(url_element, "lastmod")
                lastmod_element.text = formatted_date

                plage_horaire = str(row.get('plage', "")).strip().lower()

                if formatted_date and plage_horaire:
                    time_str = None
                    if "matin" in plage_horaire:
                        time_str = "05:00+01:00"
                    elif "midi" in plage_horaire:
                        time_str = "11:00+01:00"
                    elif "soir" in plage_horaire:
                        time_str = "17:00+01:00"
                        
                    if time_str:
                        news_element = ET.SubElement(url_element, "news:news")
                        news_pub_date = ET.SubElement(news_element, "news:publication_date")
                        news_pub_date.text = f"{formatted_date}T{time_str}"
            
            count += 1

    print(f"Sitemap generated: {count} URLs, {skipped} rows skipped")

    # Generate String
    tree = ET.ElementTree(urlset)
    ET.indent(tree, space="  ", level=0)

    from io import BytesIO
    f = BytesIO()
    tree.write(f, encoding='utf-8', xml_declaration=True)
    return f.getvalue()


BRIGHTDATA_API_URL = "https://api.brightdata.com/request"


def _fetch_attempt(api_key, proxy_url, target_url):
    """One fetch attempt for the current egress mode. Returns bytes or raises."""
    timeout = int(os.environ.get("CRAWL_TIMEOUT", "30"))
    if api_key:
        zone = os.environ.get("BRIGHTDATA_ZONE", "sitemap_unlocker")
        country = os.environ.get("BRIGHTDATA_COUNTRY", "fr")  # egress from France
        payload = {"zone": zone, "url": target_url, "format": "raw"}
        if country:
            payload["country"] = country
        response = scraper.post(
            BRIGHTDATA_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        # Web Unlocker reports target-side failure as HTTP 200 + empty body +
        # x-brd-error header (and only bills successful unlocks).
        brd_err = response.headers.get("x-brd-error")
        if brd_err:
            raise RuntimeError(f"brd-error: {brd_err}")
        if response.status_code != 200 or not response.content:
            raise RuntimeError(f"API HTTP {response.status_code}, {len(response.content)} bytes")
        return response.content

    if proxy_url:
        request_kwargs = {"timeout": timeout, "proxies": {"http": proxy_url, "https": proxy_url}}
        # Some unlockers terminate TLS with their own CA; allow opting out.
        if os.environ.get("CRAWL_PROXY_INSECURE", "").lower() in ("1", "true", "yes"):
            request_kwargs["verify"] = False
        response = scraper.get(target_url, **request_kwargs)
    else:
        response = scraper.get(target_url, timeout=timeout)

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    return response.content


def _fetch_one_sitemap_raw(target_url):
    """Fetch one sitemap's raw bytes, choosing the egress mode by env.

    Priority:
      1. Bright Data Web Unlocker API   -> BRIGHTDATA_API_KEY (+ BRIGHTDATA_ZONE)
      2. Generic proxy / ISP proxy      -> CRAWL_PROXY_URL (+ CRAWL_PROXY_INSECURE)
      3. Direct request                 -> no env set (default)

    pagesjaunes sits behind Cloudflare, which rejects a fraction of exit IPs
    with a 403 even from residential/ISP pools, so the fetch is retried up to
    CRAWL_MAX_ATTEMPTS until a good IP answers."""
    api_key = os.environ.get("BRIGHTDATA_API_KEY")
    proxy_url = os.environ.get("CRAWL_PROXY_URL")
    # Web Unlocker (with Premium domains) solves Cloudflare server-side, so a
    # low retry count is enough. This runs off-request (scheduled), so it never
    # impacts /sitemap.xml latency.
    max_attempts = int(os.environ.get("CRAWL_MAX_ATTEMPTS", "3"))

    mode = "Bright Data API" if api_key else ("proxy " + proxy_url.rsplit('@', 1)[-1] if proxy_url else "direct")
    print(f"Crawling {target_url} via {mode} (max {max_attempts} attempts)")

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            content = _fetch_attempt(api_key, proxy_url, target_url)
            if attempt > 1:
                print(f"  {target_url}: succeeded on attempt {attempt}")
            return content
        except Exception as e:
            last_err = str(e)
            print(f"  {target_url}: attempt {attempt}/{max_attempts} failed: {last_err}")
    raise RuntimeError(f"crawl of {target_url} failed after {max_attempts} attempts: {last_err}")


def _parse_sitemap_entries(raw):
    """Parse sitemap bytes into a list of {url, publication_date, name, title}."""
    ns = {'s': XMLNS, 'news': NEWS_NS}
    root = ET.fromstring(raw)  # parse bytes for correct utf-8

    entries = []
    for url_el in root.findall('s:url', ns):
        loc = (url_el.findtext('s:loc', default='', namespaces=ns) or '').strip()
        if not loc:
            continue
        entries.append({
            'url': loc,
            'publication_date': (url_el.findtext('.//news:publication_date', default='', namespaces=ns) or '').strip(),
            'name': (url_el.findtext('.//news:name', default='', namespaces=ns) or '').strip(),
            'title': (url_el.findtext('.//news:title', default='', namespaces=ns) or '').strip(),
        })
    return entries


def fetch_prod_sitemap_entries():
    """Fetch and parse all prod news sitemaps in parallel, merged + de-duped.

    Sources are fetched concurrently so total time is the slowest source, not
    the sum. A source that fails all retries is skipped (the others still
    contribute); only if EVERY source fails does this raise."""
    raw_by_url = {}
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROD_SITEMAP_URLS)) as pool:
        futures = {pool.submit(_fetch_one_sitemap_raw, url): url for url in PROD_SITEMAP_URLS}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                raw_by_url[url] = future.result()
            except Exception as e:
                errors.append(f"{url}: {e}")
                print(f"Source failed, skipping: {url} ({e})")

    # Merge in configured order for deterministic output.
    all_entries, seen = [], set()
    for url in PROD_SITEMAP_URLS:
        raw = raw_by_url.get(url)
        if raw is None:
            continue
        entries = _parse_sitemap_entries(raw)
        added = 0
        for entry in entries:
            key = entry['url'].rstrip('/')
            if key in seen:
                continue
            seen.add(key)
            all_entries.append(entry)
            added += 1
        print(f"  {url}: {len(entries)} URLs ({added} new)")

    if not all_entries:
        raise RuntimeError(f"all prod sitemaps failed: {'; '.join(errors)}")
    print(f"Crawled {len(all_entries)} URLs from {len(PROD_SITEMAP_URLS)} sitemap(s)")
    return all_entries


def cross_check_with_sheet(entries):
    """Match crawled URLs against the sheet (correspondence only, no filtering).

    For now this just reports how many crawled URLs are known to the sheet, so
    the mapping is observable. Filtering on status can be layered on later."""
    try:
        sheet_rows = get_sheet_data()
    except Exception as e:
        print(f"Cross-check skipped (sheet unavailable): {e}")
        return

    def key(u):
        return str(u).strip().rstrip('/')

    sheet_index = {key(r['url']): r for r in sheet_rows if r.get('url')}
    matched = 0
    for entry in entries:
        row = sheet_index.get(key(entry['url']))
        if row:
            matched += 1
            entry['sheet_statut'] = row.get('statut', '')
        else:
            entry['sheet_statut'] = None
    print(f"Cross-check: {matched}/{len(entries)} crawled URLs found in sheet")


def build_news_sitemap_from_entries(entries):
    """Build sitemap XML bytes from crawled prod entries."""
    urlset = ET.Element("urlset", xmlns=XMLNS)
    urlset.set("xmlns:news", NEWS_NS)

    for entry in entries:
        url_element = ET.SubElement(urlset, "url")
        ET.SubElement(url_element, "loc").text = entry['url']
        pub_date = entry.get('publication_date')
        if pub_date:
            ET.SubElement(url_element, "lastmod").text = pub_date[:10]
            news_element = ET.SubElement(url_element, "news:news")
            ET.SubElement(news_element, "news:publication_date").text = pub_date

    tree = ET.ElementTree(urlset)
    ET.indent(tree, space="  ", level=0)
    from io import BytesIO
    f = BytesIO()
    tree.write(f, encoding='utf-8', xml_declaration=True)
    return f.getvalue()


def _write_cache(xml_bytes):
    """Atomically write the sitemap cache file (write temp, then rename)."""
    tmp = SITEMAP_CACHE_PATH + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(xml_bytes)
    os.replace(tmp, SITEMAP_CACHE_PATH)


def refresh_sitemap_cache():
    """Crawl the prod sitemaps, build the news sitemap and update the cache.

    Runs off-request (triggered by /refresh or the CLI). On crawl failure the
    existing cache is kept untouched; only if there is no cache at all do we
    seed it from the sheet so /sitemap.xml has something to serve."""
    try:
        entries = fetch_prod_sitemap_entries()
    except Exception as e:
        if not os.path.exists(SITEMAP_CACHE_PATH):
            print(f"Crawl failed ({e}); no cache yet, seeding from sheet")
            _write_cache(build_sitemap_xml(get_sheet_data()))
            return {"status": "crawl_failed_seeded_from_sheet", "error": str(e)}
        print(f"Crawl failed ({e}); keeping existing cache")
        return {"status": "crawl_failed_kept_cache", "error": str(e)}

    cross_check_with_sheet(entries)
    _write_cache(build_news_sitemap_from_entries(entries))
    print(f"Cache updated: {len(entries)} URLs")
    return {"status": "ok", "urls": len(entries)}


@app.get("/sitemap.xml")
def generate_sitemap():
    """Serve the cached sitemap (instant, never crawls).

    The cache is refreshed off-request by the scheduler hitting /refresh. On a
    cold start with no cache yet, we serve the sheet-based sitemap as a
    stopgap until the first refresh runs."""
    if os.path.exists(SITEMAP_CACHE_PATH):
        with open(SITEMAP_CACHE_PATH, "rb") as fh:
            return Response(content=fh.read(), media_type="application/xml")

    print("No sitemap cache yet; serving sheet fallback")
    try:
        return Response(content=build_sitemap_xml(get_sheet_data()), media_type="application/xml")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"no cache and sheet failed: {e}")


@app.get("/refresh")
def refresh(token: str = ""):
    """Trigger a crawl + cache refresh. Called by the scheduler every ~4h.

    Protected by REFRESH_TOKEN when that env var is set: the caller must pass
    ?token=<value>. Runs synchronously (in FastAPI's threadpool) so it never
    blocks the event loop / other requests."""
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=403, detail="invalid or missing token")
    return refresh_sitemap_cache()


if __name__ == "__main__":
    import sys

    # Local dev helper: `python main.py --dump [output.xml]` fetches the sheet,
    # builds the sitemap and writes it to a file (default: sitemap.xml) so you
    # can inspect the result without running the web server. Needs credentials
    # (GOOGLE_CREDENTIALS_JSON env var or ../creds/*.json).
    if "--crawl" in sys.argv:
        # Run the same crawl + cache refresh the scheduler triggers, and write
        # the cache file (SITEMAP_CACHE_PATH) so you can inspect the result.
        result = refresh_sitemap_cache()
        print(f"Refresh result: {result}")
        print(f"Cache written to {SITEMAP_CACHE_PATH}")
    elif "--dump" in sys.argv:
        out_path = "sitemap.xml"
        for arg in sys.argv[1:]:
            if arg != "--dump" and not arg.startswith("-"):
                out_path = arg
        xml_content = build_sitemap_xml(get_sheet_data())
        with open(out_path, "wb") as fh:
            fh.write(xml_content)
        print(f"Wrote {out_path}")
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)

