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
import logging
from db import make_activity_id, save_activities_batch, has_activity, load_activities

# Initialize structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("eco-dashboard.sync")

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
            "dateRaw": date_raw,
        })
    return activities


def auto_login_if_configured():
    if STRAVIT_EMAIL and STRAVIT_PASSWORD:
        if not is_logged_in():
            logger.info("Rozpoczynanie automatycznego logowania do Stravit (Master)...")
            try:
                login_to_stravit(STRAVIT_EMAIL, STRAVIT_PASSWORD)
                logger.info("Automatyczne logowanie do Stravit zakończone sukcesem!")
            except Exception as e:
                logger.error(f"Automatyczne logowanie do Stravit nie powiodło się: {e}")


def scrape_strava_urls(slug, num_pages=2):
    strava_map = {}
    for page in range(1, num_pages + 1):
        try:
            url = f"{BASE_URL}/challenge/{urllib.parse.quote(slug)}?page={page}"
            html = fetch_text(url, headers=request_headers("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"))
            
            table_match = re.search(r'<table[^>]*class=\"[^\"]*challange__table-activities[^\"]*\".*?</table>', html, re.DOTALL)
            if not table_match:
                continue
            
            table_html = table_match.group(0)
            rows = re.findall(r'<tr[^>]*>.*?</tr>', table_html, re.DOTALL)
            
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
                
                date_raw = re.sub(r'<[^>]*>', '', tds[6]).strip()
                
                link_td = tds[8]
                link_match = re.search(r'href=\"(https?://(?:www\.)?strava\.com/activities/\d+)\"', link_td)
                strava_url = link_match.group(1) if link_match else None
                
                if strava_url:
                    rk = make_activity_id(name, date_raw, title, dist, time_sec)
                    strava_map[rk] = strava_url
        except Exception as e:
            logger.error(f"Sync: Blad podczas zeskrobywania linkow Strava ze strony {page}: {e}")
    return strava_map


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
    
    # Dynamic page detection to scrape Strava URLs
    num_new_approx = 15 # default page size detection buffer
    if not full_import:
        try:
            existing = load_activities(slug) or []
            num_new_approx = max(15, len(activities) - len(existing) + 5)
        except Exception:
            pass
    else:
        num_new_approx = len(activities)
        
    pages_to_scrape = min(350, (num_new_approx // 10) + 2)
    logger.info(f"Sync: Pobieranie linkow Strava z pierwszych {pages_to_scrape} stron portalu...")
    
    try:
        strava_map = scrape_strava_urls(slug, num_pages=pages_to_scrape)
        for act in activities:
            rk = make_activity_id(act["name"], act.get("dateRaw", act["dateStr"]), act["title"], act["dist"], act["timeSec"])
            if rk in strava_map:
                act["stravaUrl"] = strava_map[rk]
    except Exception as e:
        logger.error(f"Sync: Nie udalo sie powiazac linkow Strava: {e}")

    if full_import:
        logger.info(f"Sync: Uruchomiono pelny import (Full Import) dla wyzwania: {slug}. Liczba treningow: {len(activities)}")
        save_activities_batch(slug, activities)
        return len(activities)
    
    # Szybka synchronizacja przyrostowa na podstawie zbioru kluczy w pamięci
    logger.info(f"Sync: Uruchomiono przyrostową synchronizację (In-Memory Key Check) dla wyzwania: {slug}")
    try:
        existing = load_activities(slug) or []
    except Exception as e:
        logger.error(f"Sync: Nie udało się pobrać istniejących aktywności do porównania: {e}")
        existing = []

    existing_keys = set()
    for act in existing:
        act_id = make_activity_id(act["name"], act.get("dateRaw", act["dateStr"]), act["title"], act["dist"], act["timeSec"])
        existing_keys.add(act_id)

    new_activities = []
    for act in activities:
        act_id = make_activity_id(act["name"], act.get("dateRaw", act["dateStr"]), act["title"], act["dist"], act["timeSec"])
        if act_id not in existing_keys:
            new_activities.append(act)
        
    if new_activities:
        logger.info(f"Sync: Znaleziono {len(new_activities)} nowych treningów do zaimportowania.")
        # Odwróć listę, aby zapisywać od najstarszych do najnowszych
        reversed_new = list(reversed(new_activities))
        for act in reversed_new:
            logger.info(f"  -> IMPORT: {act['name']} - \"{act['title']}\" ({act['dist']} km, {act['pts']} pkt, {act['type']}, {act['dateStr']})")
        save_activities_batch(slug, reversed_new)
    else:
        logger.info("Sync: Baza danych jest aktualna. Brak nowych treningów do zaimportowania.")
        
    return len(new_activities)


def background_worker(slug):
    # Auto cold-start helper
    try:
        logger.info(f"Wątek tła (Sync Daemon): Sprawdzanie stanu bazy dla {slug}...")
        existing = load_activities(slug)
        if not existing:
            logger.info(f"Baza aktywności dla {slug} jest pusta. Inicjowanie pełnego importu początkowego...")
            sync_activities(slug, full_import=True)
        else:
            logger.info(f"Baza danych zawiera {len(existing)} aktywności. Uruchamianie szybkiej synchronizacji startowej...")
            sync_activities(slug, full_import=False)
    except Exception as e:
        logger.error(f"Błąd podczas synchronizacji początkowej wątku tła: {e}")

    while True:
        # Sleep first to avoid running immediately after cold start
        time.sleep(900)
        try:
            logger.info(f"Wątek tła (Sync Daemon): Rozpoczynanie cyklicznej synchronizacji dla {slug}...")
            new_count = sync_activities(slug, full_import=False)
            logger.info(f"Wątek tła (Sync Daemon): Synchronizacja zakończona. Dodano {new_count} nowych treningów.")
        except Exception as e:
            logger.error(f"Wątek tła (Sync Daemon): Błąd podczas synchronizacji cyklicznej: {e}")


def start_background_sync(slug):
    t = threading.Thread(target=background_worker, args=(slug,), daemon=True)
    t.start()
    logger.info(f"Uruchomiono wątek tła synchronizacji dla wyzwania: {slug}")
