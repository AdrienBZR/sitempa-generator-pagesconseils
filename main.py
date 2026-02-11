import ast
import base64
import os
import json
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
            decoded_str = decoded_bytes.decode('utf-8')
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
    """Authenticates and fetches records from ALL worksheets."""
    try:
        creds = get_credentials()
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID)
        
        all_records = []
        worksheets = sheet.worksheets()
        print(f"Found {len(worksheets)} worksheets. Processing...")
        
        for ws in worksheets:
            # print(f"Reading worksheet: {ws.title}")
            records = ws.get_all_records()
            all_records.extend(records)
            
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
        return response.status_code == 200
    except Exception as e:
        print(f"Skipping {url} - Error: {e}")
        return False

def format_date(date_str):
    """Converts DD/MM/YYYY to YYYY-MM-DD."""
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return date_str

@app.get("/sitemap.xml")
async def generate_sitemap():
    """Generates and returns sitemap.xml."""
    try:
        data = get_sheet_data()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    urlset = ET.Element("urlset", xmlns=XMLNS)
    
    # Configuration
    url_key = 'URL article'
    lastmod_key = 'Date de MEP'
    statut_key = 'Statut'
    target_statut = 'Programmé'

    count = 0
    if data:
        for row in data:
            if row.get(statut_key) != target_statut:
                continue

            url_val = row.get(url_key)
            if not url_val:
                continue
            
            # Note: synchronous validation might slow down request. 
            # Ideally this should be cached or async.
            if not validate_url(url_val):
               continue

            url_element = ET.SubElement(urlset, "url")
            loc_element = ET.SubElement(url_element, "loc")
            loc_element.text = str(url_val).strip()

            if lastmod_key and row.get(lastmod_key):
                raw_date = str(row.get(lastmod_key)).strip()
                formatted_date = format_date(raw_date)
                
                lastmod_element = ET.SubElement(url_element, "lastmod")
                lastmod_element.text = formatted_date
            
            count += 1
            
    # Generate String
    tree = ET.ElementTree(urlset)
    ET.indent(tree, space="  ", level=0)
    
    # Write to memory (for response)
    from io import BytesIO
    f = BytesIO()
    tree.write(f, encoding='utf-8', xml_declaration=True)
    xml_content = f.getvalue()
    
    return Response(content=xml_content, media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

