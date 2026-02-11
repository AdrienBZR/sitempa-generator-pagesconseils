import cloudscraper
import gspread
from google.oauth2.service_account import Credentials
import xml.etree.ElementTree as ET
from datetime import datetime
import os

# Configuration
SERVICE_ACCOUNT_FILE = '../creds/solocal-poc-f9a485d4ac05.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
SHEET_ID = '1B93nJwvS591zZ-x7nGCwwPkOcbnH4ZifApO_QSQztzg'
OUTPUT_FILE = 'sitemap.xml'

# XML Namespaces
XMLNS = "http://www.sitemaps.org/schemas/sitemap/0.9"

# Initialize scraper
scraper = cloudscraper.create_scraper()

def get_sheet_data():
    """Authenticates and fetches records from ALL worksheets in the Google Sheet."""
    try:
        # Resolve absolute path for credentials
        base_dir = os.path.dirname(os.path.abspath(__file__))
        creds_path = os.path.join(base_dir, SERVICE_ACCOUNT_FILE)
        
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID)
        
        all_records = []
        worksheets = sheet.worksheets()
        print(f"Found {len(worksheets)} worksheets. Processing...")
        
        for ws in worksheets:
            print(f"Reading worksheet: {ws.title}")
            records = ws.get_all_records()
            all_records.extend(records)
            
        return all_records
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error fetching data from Google Sheet: {repr(e)}")
        # Re-raise to let the caller handle it or stop execution
        raise

def validate_url(url):
    """Checks if the URL returns 200 OK using cloudscraper."""
    try:
        response = scraper.get(url)
        if response.status_code == 200:
            return True
        print(f"Skipping {url} - Status: {response.status_code}")
        return False
    except Exception as e:
        print(f"Skipping {url} - Error: {e}")
        return False

def format_date(date_str):
    """Converts DD/MM/YYYY to YYYY-MM-DD."""
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return date_str # Return original if parse fails

def generate_sitemap(data):
    """Generates a sitemap.xml file from the provided data."""
    urlset = ET.Element("urlset", xmlns=XMLNS)

    if not data:
        print("No data found to generate sitemap.")
        return

    # Explicit column names
    url_key = 'URL article'
    lastmod_key = 'Date de MEP'
    statut_key = 'Statut'
    target_statut = 'Programmé'
    
    print(f"Using columns - URL: '{url_key}', LastMod: '{lastmod_key}', Filter: '{statut_key}' == '{target_statut}'")

    count = 0
    skipped = 0
    total = len(data)
    
    for i, row in enumerate(data):
        # 1. Filter by Statut
        if row.get(statut_key) != target_statut:
            skipped += 1
            continue

        url_val = row.get(url_key)
        if not url_val:
            continue
            
        print(f"Processing candidate ({i+1}/{total}): {url_val}")
        
        # 2. Validate URL (200 OK)
        if not validate_url(url_val):
            continue

        url_element = ET.SubElement(urlset, "url")
        loc_element = ET.SubElement(url_element, "loc")
        loc_element.text = str(url_val).strip()

        # 3. Handle Date
        if lastmod_key and row.get(lastmod_key):
            raw_date = str(row.get(lastmod_key)).strip()
            formatted_date = format_date(raw_date)
            
            lastmod_element = ET.SubElement(url_element, "lastmod")
            lastmod_element.text = formatted_date
            
        count += 1

    tree = ET.ElementTree(urlset)
    
    # Pretty printing (Python 3.9+ has indent)
    ET.indent(tree, space="  ", level=0)
    
    tree.write(OUTPUT_FILE, encoding='utf-8', xml_declaration=True)
    print(f"Sitemap generated at {os.path.abspath(OUTPUT_FILE)}")
    print(f"Stats: {count} URLs added, {skipped} skipped (wrong status), Total rows processed: {total}")

def main():
    try:
        data = get_sheet_data()
        generate_sitemap(data)
    except Exception:
        print("Failed to run pipeline.")

if __name__ == "__main__":
    main()
