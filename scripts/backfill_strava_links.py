import os
import sys
import re
import csv
import time
import urllib.request
import urllib.parse
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor, as_completed
from azure.data.tables import TableClient

def load_env():
    # Root dir is the parent of scripts/ folder
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root_dir, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")

# Load environment variables from .env file before importing db/sync
load_env()

# Add app directory to path to import db and sync
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import db
from db import make_activity_id, _get_table_client, load_activities
from sync import login_to_stravit, _opener, BASE_URL, parse_number, parse_time_to_seconds

def fetch_page_html(page):
    url = f"{BASE_URL}/challenge/rywalizacja-sportowa?page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with _opener.open(req, timeout=30) as resp:
            return page, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"Error fetching page {page}: {e}")
        return page, None

def parse_activities_from_html(html):
    if not html:
        return []
    
    table_match = re.search(r'<table[^>]*class=\"[^\"]*challange__table-activities[^\"]*\".*?</table>', html, re.DOTALL)
    if not table_match:
        return []
    
    table_html = table_match.group(0)
    rows = re.findall(r'<tr[^>]*>.*?</tr>', table_html, re.DOTALL)
    
    parsed = []
    # Skip header
    for row in rows[1:]:
        tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(tds) < 9:
            continue
        
        name = re.sub(r'<[^>]*>', '', tds[0]).strip()
        title = re.sub(r'<[^>]*>', '', tds[1]).strip()
        
        dist_text = re.sub(r'<[^>]*>', '', tds[2]).strip()
        dist = parse_number(dist_text)
        
        time_text = re.sub(r'<[^>]*>', '', tds[4]).strip()
        time_sec = parse_time_to_seconds(time_text)
        
        type_str = re.sub(r'<[^>]*>', '', tds[5]).strip()
        date_raw = re.sub(r'<[^>]*>', '', tds[6]).strip()
        
        link_td = tds[8]
        link_match = re.search(r'href=\"(https?://(?:www\.)?strava\.com/activities/\d+)\"', link_td)
        strava_url = link_match.group(1) if link_match else None
        
        parsed.append({
            "name": name,
            "title": title,
            "dist": dist,
            "timeSec": time_sec,
            "type": type_str,
            "dateRaw": date_raw,
            "stravaUrl": strava_url
        })
    return parsed

def main():
    # Force target if passed as CLI argument, otherwise fallback to AZURE_STORAGE_CONNECTION_STRING
    conn_str = None
    target = sys.argv[1] if len(sys.argv) > 1 else None
    
    if target in ("dev", "prod"):
        if target == "prod":
            conn_str = os.environ.get("PROD_STORAGE_CONNECTION_STRING")
            print("Using PROD database from PROD_STORAGE_CONNECTION_STRING.")
        else:
            conn_str = os.environ.get("DEV_STORAGE_CONNECTION_STRING")
            print("Using DEV database from DEV_STORAGE_CONNECTION_STRING.")
    else:
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str and "AccountName=..." not in conn_str:
            print("Using connection string from AZURE_STORAGE_CONNECTION_STRING.")
        else:
            conn_str = os.environ.get("DEV_STORAGE_CONNECTION_STRING")
            print("Fallback: Using DEV database from DEV_STORAGE_CONNECTION_STRING.")

    if not conn_str or "AccountName=..." in conn_str:
        print("Error: Azure connection string is not configured or contains placeholder '...' values.")
        print("Please configure AZURE_STORAGE_CONNECTION_STRING or DEV_STORAGE_CONNECTION_STRING/PROD_STORAGE_CONNECTION_STRING in your .env file.")
        sys.exit(1)
        
    os.environ["AZURE_STORAGE_CONNECTION_STRING"] = conn_str
    db.AZURE_STORAGE_CONN_STR = conn_str

    print("Step 1: Logging in to Stravit...")
    email = os.environ.get("STRAVIT_EMAIL")
    password = os.environ.get("STRAVIT_PASSWORD")
    
    if not email or not password:
        print("Stravit credentials not found in environment (STRAVIT_EMAIL/STRAVIT_PASSWORD).")
        try:
            if not email:
                email = input("Enter Stravit email: ").strip()
            if not password:
                import getpass
                password = getpass.getpass("Enter Stravit password: ").strip()
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(1)
            
    login_to_stravit(email, password)
    print("Logged in successfully!")

    print("Step 2: Detecting total pages from Stravit challenge page...")
    _, first_page_html = fetch_page_html(1)
    page_matches = re.findall(r'page=(\d+)', first_page_html)
    total_pages = max([int(p) for p in page_matches]) if page_matches else 1
    print(f"Detected total activities pages: {total_pages}")

    print(f"Step 3: Scraped activities pages 1 to {total_pages} in parallel...")
    scraped_activities = []
    
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = {executor.submit(fetch_page_html, page): page for page in range(1, total_pages + 1)}
        for future in as_completed(futures):
            page, html = future.result()
            acts = parse_activities_from_html(html)
            scraped_activities.extend(acts)
            if page % 25 == 0 or page == total_pages:
                print(f"  -> Processed page {page}/{total_pages}")
                
    elapsed = time.time() - start_time
    print(f"Scraped {len(scraped_activities)} activities from Stravit HTML in {elapsed:.2f} seconds.")

    print("Step 4: Mapping Strava links by RowKey...")
    strava_map = {}
    for act in scraped_activities:
        if act["stravaUrl"]:
            rk = make_activity_id(act["name"], act["dateRaw"], act["title"], act["dist"], act["timeSec"])
            strava_map[rk] = act["stravaUrl"]
            
    print(f"Mapped {len(strava_map)} unique activities to Strava URLs.")

    print("Step 5: Loading existing activities from database...")
    db_activities = load_activities("rywalizacja-sportowa") or []
    print(f"Loaded {len(db_activities)} activities from database.")

    print("Step 6: Backfilling Strava links in Azure Table Storage...")
    client = _get_table_client("activities")
    if not client:
        print("Error: Could not connect to Azure Table.")
        return

    updated_count = 0
    skipped_count = 0
    batch = []
    
    for act in db_activities:
        rk = make_activity_id(act["name"], act.get("dateRaw", act["dateStr"]), act["title"], act["dist"], act["timeSec"])
        
        # Check if we have a Strava URL for this activity
        strava_url = strava_map.get(rk)
        if strava_url:
            # Check if it is already set to avoid redundant updates
            if act.get("stravaUrl") == strava_url:
                skipped_count += 1
                continue
                
            # Update entity
            entity = {
                "PartitionKey": "rywalizacja-sportowa",
                "RowKey": rk,
                "name": act["name"],
                "title": act["title"],
                "dist": act["dist"],
                "pts": act["pts"],
                "elev": act["elev"],
                "timeSec": act["timeSec"],
                "type": act["type"],
                "dateStr": act["dateStr"],
                "dateRaw": act.get("dateRaw", act["dateStr"]),
                "stravaUrl": strava_url
            }
            batch.append(("upsert", entity))
            updated_count += 1
            
            # Execute batch update (max 100 per transaction)
            if len(batch) >= 100:
                client.submit_transaction(batch)
                print(f"  -> Submitted batch update: {updated_count} entities updated...")
                batch = []
        else:
            skipped_count += 1

    if batch:
        client.submit_transaction(batch)
        print(f"  -> Submitted final batch update: {updated_count} entities updated...")

    print(f"\nBackfill complete! Updated: {updated_count}, Skipped (no changes or no URL found): {skipped_count}")

if __name__ == "__main__":
    main()
