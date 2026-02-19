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
    target_statuses = ['Programmé', 'Publié']

    count = 0
    if data:
        for row in data:
            url_val = row.get(url_key) # Get URL early for debug log
            
            status = row.get(statut_key)
            # print(f"Checking row: {url_val} | Status: {status}") # DEBUG - Clean up logs slightly
            
            if status not in target_statuses:
                # Check for encoding/whitespace issues
                if str(status).strip() in target_statuses:
                     print(f"WARN: Status match check failed due to whitespace? '{status}'")
                continue

            if not url_val:
                print("Skipping: No URL found")
                continue
            
            # Note: synchronous validation might slow down request. 
            # Ideally this should be cached or async.
            # if not validate_url(url_val):
            #    print(f"Skipping: Validation failed for {url_val}")
            #    continue

            url_element = ET.SubElement(urlset, "url")
            loc_element = ET.SubElement(url_element, "loc")
            loc_element.text = str(url_val).strip()

            if lastmod_key and row.get(lastmod_key):
                raw_date = str(row.get(lastmod_key)).strip()
                formatted_date = parse_date(raw_date)
                
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

