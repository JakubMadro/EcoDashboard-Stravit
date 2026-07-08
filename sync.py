import os
import re
import csv
import time
import gzip
import zlib
import urllib.request
import urllib.parse
import http.cookiejar
import html
import threading
from db import make_activity_id, save_activities_batch, has_activity, load_activities

BASE_URL = "https://mtupolska.stravit.app"
DEFAULT_SLUG = "rywalizacja-sportowa"

STRAVIT_EMAIL = os.environ.get("STRAVIT_EMAIL")
STRAVIT_PASSWORD = os.environ.get("STRAVIT_PASSWORD")

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))

_session_valid = False
_last_status_check = 0


class AuthRequired(RuntimeError):
    pass


def request_headers(accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"):
    headers = {
        "Accept": accept,
        "Accept-Language": "pl,en;q=0.8",
        "User-Agent": "DashboardEco/1.0",
    }
    return headers


def fetch_text(url, data=None, headers=None):
    req_headers = dict(headers or request_headers())
    req_headers["Accept-Encoding"] = "gzip, deflate"
    req = urllib.request.Request(url, data=data, headers=req_headers)
    try:
        with _opener.open(req, timeout=30) as resp:
            content_encoding = resp.headers.get("Content-Encoding", "").lower()
            raw_data = resp.read()
            if "gzip" in content_encoding:
                raw_data = gzip.decompress(raw_data)
            elif "deflate" in content_encoding:
                try:
                    raw_data = zlib.decompress(raw_data)
                except zlib.error:
                    raw_data = zlib.decompress(raw_data, -zlib.MAX_WBITS)

            charset = resp.headers.get_content_charset() or "utf-8"
            return raw_data.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthRequired("Stravit odrzucil sesje. Zaloguj sie ponownie.") from exc
        raise


def csrf_from_login_page(source):
    match = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', source)
    if not match:
        match = re.search(r"value=\"([^\"]+)\"[^>]*name=\"_csrf_token\"", source)
    if not match:
        raise RuntimeError("Nie znalazlem tokenu CSRF na stronie logowania.")
    return html.unescape(match.group(1))


def is_logged_in():
    global _session_valid, _last_status_check
    
    # Quick check: if we have no session cookie, we are definitely logged out
    if not any(cookie.name == "PHPSESSID" for cookie in _cookie_jar):
        _session_valid = False
        return False
        
    # If session is active and was verified within 5 minutes, assume valid
    now = time.time()
    if _session_valid and (now - _last_status_check < 300):
        return True

    # Otherwise do a lightweight check by requesting root "/" which is tiny
    try:
        source = fetch_text(BASE_URL)
        logged_in = "/logout" in source and "Logowanie do Stravit" not in source
        _session_valid = logged_in
        _last_status_check = now
        return logged_in
    except Exception:
        return _session_valid


def login_to_stravit(email, password, remember=True):
    global _session_valid, _last_status_check
    _session_valid = False
    _last_status_check = 0

    if not email or not password:
        raise RuntimeError("Podaj email i haslo.")

    login_page = fetch_text(f"{BASE_URL}/login")
    csrf_token = csrf_from_login_page(login_page)
    payload = {
        "email": email,
        "password": password,
        "_csrf_token": csrf_token,
    }
    if remember:
        payload["_remember_me"] = "on"

    data = urllib.parse.urlencode(payload).encode("utf-8")
    headers = request_headers("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(f"{BASE_URL}/login", data=data, headers=headers)
    try:
        with _opener.open(req, timeout=30) as resp:
            source = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthRequired("Bledne dane logowania.") from exc
        raise

    if not is_logged_in():
        raise RuntimeError("Logowanie nie powiodlo sie. Sprawdz dane logowania.")

    _session_valid = True
    _last_status_check = time.time()
    return {"success": True}


def parse_number(value):
    cleaned = re.sub(r"[^0-9,.\-]", "", value or "").replace(",", ".")
    try:
        return float(cleaned) if cleaned else 0
    except ValueError:
        return 0


def parse_time_to_seconds(value):
    parts = [int(p) for p in re.findall(r"\d+", value or "")]
    if len(parts) >= 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0


def parse_csv_activities(source):
    lines = source.splitlines()
    header_idx = -1
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if "nazwa uzytkownika" in lowered or "przyznane_punkty" in lowered:
            header_idx = idx
            break
    if header_idx == -1:
        raise AuthRequired("Eksport CSV nie jest dostepny. Zaloguj sie ponownie do Stravit.")

    activities = []
    reader = csv.DictReader(lines[header_idx:], delimiter=";")
    for row in reader:
        name = (row.get("nazwa uzytkownika") or "").strip()
        date_raw = (row.get("data treningu") or "").strip()
        if not name or not date_raw:
            continue
        activities.append({
            "name": name,
            "title": (row.get("nazwa") or "").strip(),
            "dist": parse_number(row.get("dystans") or ""),
            "pts": parse_number(row.get("przyznane_punkty") or ""),
            "elev": parse_number(row.get("przewyzszenia") or ""),
            "timeSec": parse_time_to_seconds(row.get("czas") or ""),
            "type": (row.get("typ treningu") or "").strip(),
            "dateStr": date_raw[:10],
        })
    return activities


def auto_login_if_configured():
    if STRAVIT_EMAIL and STRAVIT_PASSWORD:
        if not is_logged_in():
            print("Auto-logging in to Stravit...", flush=True)
            try:
                login_to_stravit(STRAVIT_EMAIL, STRAVIT_PASSWORD)
                print("Auto-login successful!", flush=True)
            except Exception as e:
                print(f"Auto-login failed: {e}", flush=True)


def sync_activities(slug, full_import=False):
    global _session_valid, _last_status_check
    auto_login_if_configured()

    url = f"{BASE_URL}/challenge/{urllib.parse.quote(slug)}/export/activities/csv"
    csv_text = fetch_text(url, headers=request_headers("text/csv,text/plain,*/*"))
    
    if "Logowanie do Stravit" in csv_text or "<form" in csv_text[:2000]:
        _session_valid = False
        raise AuthRequired("Sesja Stravit wygasla. Zaloguj sie ponownie.")

    # Reset valid status checks
    _session_valid = True
    _last_status_check = time.time()

    activities = parse_csv_activities(csv_text)
    
    if full_import:
        print(f"Sync: Performing full import of {len(activities)} activities...", flush=True)
        save_activities_batch(slug, activities)
        return len(activities)
    
    # Fast Incremental Sync (Stop on exist)
    new_activities = []
    print("Sync: Performing fast incremental sync...", flush=True)
    for act in activities:
        act_id = make_activity_id(act["name"], act["dateStr"], act["title"], act["dist"], act["timeSec"])
        if has_activity(slug, act_id):
            # Already exists in DB - stop parsing older ones!
            break
        new_activities.append(act)
        
    if new_activities:
        print(f"Sync: Found {len(new_activities)} new activities. Saving to DB...", flush=True)
        # Reverse to save oldest to newest (or batch handles it)
        save_activities_batch(slug, list(reversed(new_activities)))
        
    return len(new_activities)


def background_worker(slug):
    # Auto cold-start helper
    try:
        print(f"Background worker cold-start verification for {slug}...", flush=True)
        existing = load_activities(slug)
        if not existing:
            print(f"Database table is empty for {slug}. Starting initial full import...", flush=True)
            sync_activities(slug, full_import=True)
        else:
            print(f"Database contains {len(existing)} activities. Performing quick startup sync...", flush=True)
            sync_activities(slug, full_import=False)
    except Exception as e:
        print(f"Background worker cold-start sync failed: {e}", flush=True)

    while True:
        # Sleep first to avoid running immediately after cold start
        time.sleep(900)
        try:
            print(f"Background worker: Starting sync for {slug}...", flush=True)
            new_count = sync_activities(slug, full_import=False)
            print(f"Background worker: Sync completed. Added {new_count} activities.", flush=True)
        except Exception as e:
            print(f"Background worker error during sync: {e}", flush=True)


def start_background_sync(slug):
    t = threading.Thread(target=background_worker, args=(slug,), daemon=True)
    t.start()
    print(f"Background sync thread started for slug: {slug}", flush=True)
