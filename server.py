#!/usr/bin/env python3
import csv
import html
import http.cookiejar
import json
import mimetypes
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
BASE_DIR = Path(__file__).resolve().parent
BASE_URL = "https://mtupolska.stravit.app"
DEFAULT_SLUG = "rywalizacja-sportowa"
CACHE_TTL_SECONDS = 120
STATUS_TEXT = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 502: "Bad Gateway", 403: "Forbidden"}

STRAVIT_EMAIL = os.environ.get("STRAVIT_EMAIL")
STRAVIT_PASSWORD = os.environ.get("STRAVIT_PASSWORD")
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
BLOB_CONTAINER_NAME = os.environ.get("BLOB_CONTAINER_NAME", "crews")
PROFILE_INDEX_BLOB = "profiles-index.json"

_cache = {}
_cache_lock = threading.Lock()
_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))

try:
    from azure.storage.blob import BlobServiceClient
    HAS_AZURE = True
except ImportError:
    HAS_AZURE = False


class AuthRequired(RuntimeError):
    pass


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


def request_headers(accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"):
    headers = {
        "Accept": accept,
        "Accept-Language": "pl,en;q=0.8",
        "User-Agent": "DashboardEco/1.0",
    }
    return headers


def fetch_text(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or request_headers())
    try:
        with _opener.open(req, timeout=30) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
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
    try:
        source = fetch_text(f"{BASE_URL}/challenge/{DEFAULT_SLUG}")
    except Exception:
        return False
    return "/logout" in source and "Logowanie do Stravit" not in source


def login_to_stravit(email, password, remember=True):
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

    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    headers = request_headers("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    fetch_text(f"{BASE_URL}/login", data=encoded, headers=headers)

    _cache.clear()
    if not is_logged_in():
        raise AuthRequired("Logowanie do Stravit nie powiodlo sie. Sprawdz email i haslo.")
    return {"ok": True, "loggedIn": True}


class ChallengeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = {"leaderboard": [], "activities": []}
        self._table_kind = None
        self._table_depth = 0
        self._in_row = False
        self._in_cell = False
        self._current_row = []
        self._current_cell = []
        self._last_page = 1

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")

        if tag == "table":
            if "challange__table-leaderboard" in classes:
                self._table_kind = "leaderboard"
                self._table_depth = 1
            elif "challange__table-activities" in classes:
                self._table_kind = "activities"
                self._table_depth = 1
            elif self._table_kind:
                self._table_depth += 1
            return

        if self._table_kind and tag == "tr":
            self._in_row = True
            self._current_row = []
            return

        if self._table_kind and self._in_row and tag in ("td", "th"):
            self._in_cell = True
            self._current_cell = []
            return

        if tag == "a":
            href = html.unescape(attrs.get("href", ""))
            match = re.search(r"[?&]page=(\d+)", href)
            if match:
                self._last_page = max(self._last_page, int(match.group(1)))

    def handle_endtag(self, tag):
        if self._table_kind and tag == "table":
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._table_kind = None
            return

        if self._table_kind and self._in_cell and tag in ("td", "th"):
            text = " ".join("".join(self._current_cell).split())
            self._current_row.append(text)
            self._in_cell = False
            return

        if self._table_kind and self._in_row and tag == "tr":
            row = [cell for cell in self._current_row if cell]
            if row and not any(cell in ("Miejsce", "Nazwa", "Punkty", "Zawodnik") for cell in row):
                self.tables[self._table_kind].append(row)
            self._in_row = False

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell.append(data)


def parse_challenge_html(source):
    parser = ChallengeParser()
    parser.feed(source)
    return parser.tables, parser._last_page


def parse_leaderboard(rows):
    leaders = []
    previous_rank = 0
    for row in rows:
        if len(row) < 3:
            continue
        rank = int(parse_number(row[0]))
        name = row[1].strip()
        points = parse_number(row[2])
        count = int(parse_number(row[3])) if len(row) > 3 else 0
        elevation = parse_number(row[5]) if len(row) > 5 else 0
        if previous_rank and rank <= previous_rank:
            break
        if rank and name:
            leaders.append({
                "rank": rank,
                "name": name,
                "points": points,
                "count": count,
                "elevation": elevation,
            })
            previous_rank = rank
    return leaders


def parse_activities(rows):
    activities = []
    for row in rows:
        if len(row) < 7:
            continue
        name = row[0].strip()
        title = row[1].strip()
        distance = parse_number(row[2])
        points = parse_number(row[3])
        time_sec = parse_time_to_seconds(row[4])
        activity_type = row[5].strip()
        date_raw = row[6].strip()
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", date_raw)
        if not name or not date_match:
            continue
        activities.append({
            "name": name,
            "title": title,
            "dist": distance,
            "pts": points,
            "elev": 0,
            "timeSec": time_sec,
            "type": activity_type,
            "dateStr": date_match.group(0),
        })
    return activities


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


def build_data_from_activities(activities):
    all_dates = sorted({activity["dateStr"] for activity in activities})
    if not all_dates:
        raise RuntimeError("Stravit nie zwrocil aktywnosci do przetworzenia.")

    users = {}
    for activity in activities:
        user = users.setdefault(activity["name"], {
            "distance": 0,
            "points": 0,
            "elevation": 0,
            "time": 0,
            "count": 0,
            "daily": {date: {"points": 0, "distance": 0} for date in all_dates},
            "byType": {},
        })
        user["distance"] += activity["dist"]
        user["points"] += activity["pts"]
        user["elevation"] += activity["elev"]
        user["time"] += activity["timeSec"]
        user["count"] += 1
        user["daily"][activity["dateStr"]]["points"] += activity["pts"]
        user["daily"][activity["dateStr"]]["distance"] += activity["dist"]

        by_type = user["byType"].setdefault(activity["type"], {
            "count": 0,
            "distance": 0,
            "points": 0,
            "time": 0,
        })
        by_type["count"] += 1
        by_type["distance"] += activity["dist"]
        by_type["points"] += activity["pts"]
        by_type["time"] += activity["timeSec"]

    sorted_users = sorted(users.items(), key=lambda item: item[1]["points"], reverse=True)
    for idx, (_, user) in enumerate(sorted_users, 1):
        user["rank"] = idx

    for user in users.values():
        user["distance"] = round(user["distance"], 2)
        user["points"] = round(user["points"], 3)
        user["elevation"] = round(user["elevation"], 1)
        for day in user["daily"].values():
            day["points"] = round(day["points"], 3)
            day["distance"] = round(day["distance"], 2)
        for by_type in user["byType"].values():
            by_type["distance"] = round(by_type["distance"], 2)
            by_type["points"] = round(by_type["points"], 3)

    totals = {
        "distance": round(sum(user["distance"] for user in users.values()), 1),
        "points": round(sum(user["points"] for user in users.values()), 1),
        "count": len(activities),
        "time": sum(user["time"] for user in users.values()),
    }

    names = sorted(users, key=lambda name: name.casefold())
    top = sorted(
        [{"name": name, "points": user["points"], "rank": user["rank"]} for name, user in users.items()],
        key=lambda item: item["rank"],
    )[:10]

    return {
        "source": "stravit-csv",
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dateRange": [all_dates[0], all_dates[-1]],
        "allDates": all_dates,
        "totalUsers": len(users),
        "totals": totals,
        "allNames": names,
        "topLeaders": top,
        "users": users,
    }

def save_profile_index(index_payload):
    if HAS_AZURE and AZURE_STORAGE_CONNECTION_STRING:
        try:
            blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
            container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)
            try:
                container_client.create_container()
            except Exception:
                pass
            blob_client = blob_service_client.get_blob_client(container=BLOB_CONTAINER_NAME, blob=PROFILE_INDEX_BLOB)
            blob_client.upload_blob(json.dumps(index_payload, ensure_ascii=False), overwrite=True)
            return True
        except Exception as e:
            print(f"Error saving profile index to Azure Blob: {e}")

    try:
        local_dir = BASE_DIR / "data" / "crews"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / PROFILE_INDEX_BLOB).write_text(json.dumps(index_payload, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        print(f"Error saving profile index locally: {e}")
        return False


def load_profile_summaries():
    if HAS_AZURE and AZURE_STORAGE_CONNECTION_STRING:
        try:
            blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
            blob_client = blob_service_client.get_blob_client(container=BLOB_CONTAINER_NAME, blob=PROFILE_INDEX_BLOB)
            if blob_client.exists():
                data_str = blob_client.download_blob().readall().decode("utf-8")
                payload = json.loads(data_str)
                if isinstance(payload, list):
                    return payload
        except Exception as e:
            print(f"Error loading profile index from Azure Blob: {e}")

    try:
        local_file = BASE_DIR / "data" / "crews" / PROFILE_INDEX_BLOB
        if local_file.exists():
            payload = json.loads(local_file.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return payload
    except Exception as e:
        print(f"Error loading profile index locally: {e}")

    return []


def save_crew_data(crew_id, payload):
    if isinstance(payload, dict):
        profile_data = dict(payload)
    else:
        profile_data = {
            "id": crew_id,
            "name": f"Profil {crew_id}",
            "me": "",
            "members": payload if isinstance(payload, list) else [],
        }

    profile_data["id"] = profile_data.get("id") or crew_id
    profile_data["me"] = (profile_data.get("me") or "").strip()
    profile_data.pop("name", None)
    members = profile_data.get("members")
    if not isinstance(members, list):
        members = []
    profile_data["members"] = [member for member in members if isinstance(member, str) and member.strip()]

    data_str = json.dumps(profile_data, ensure_ascii=False)
    if HAS_AZURE and AZURE_STORAGE_CONNECTION_STRING:
        try:
            blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
            container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)
            try:
                container_client.create_container()
            except Exception:
                pass
            blob_client = blob_service_client.get_blob_client(container=BLOB_CONTAINER_NAME, blob=f"profiles/{profile_data['id']}.json")
            blob_client.upload_blob(data_str, overwrite=True)
            print(f"Saved profile {profile_data['id']} to Azure Blob Storage.")
        except Exception as e:
            print(f"Error saving profile to Azure Blob: {e}")

    try:
        local_dir = BASE_DIR / "data" / "crews" / "profiles"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / f"{profile_data['id']}.json").write_text(data_str, encoding="utf-8")
        print(f"Saved profile {profile_data['id']} to local file system.")
    except Exception as e:
        print(f"Error saving profile locally: {e}")

    summaries = load_profile_summaries()
    summary = {
        "id": profile_data["id"],
        "me": profile_data["me"],
        "memberCount": len(profile_data["members"]),
    }
    existing = [item for item in summaries if item.get("id") == profile_data["id"]]
    if existing:
        for item in summaries:
            if item.get("id") == profile_data["id"]:
                item.update(summary)
                break
    else:
        summaries.append(summary)
    save_profile_index(summaries)
    return True


def load_crew_data(crew_id):
    if HAS_AZURE and AZURE_STORAGE_CONNECTION_STRING:
        try:
            blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
            blob_client = blob_service_client.get_blob_client(container=BLOB_CONTAINER_NAME, blob=f"profiles/{crew_id}.json")
            if blob_client.exists():
                stream = blob_client.download_blob()
                data_str = stream.readall().decode("utf-8")
                return json.loads(data_str)
        except Exception as e:
            print(f"Error loading profile from Azure Blob: {e}")

    try:
        local_file = BASE_DIR / "data" / "crews" / "profiles" / f"{crew_id}.json"
        if local_file.exists():
            data_str = local_file.read_text(encoding="utf-8")
            return json.loads(data_str)
    except Exception as e:
        print(f"Error loading profile locally: {e}")

    return {}


def list_crew_profiles():
    return load_profile_summaries()


def auto_login_if_configured():
    if STRAVIT_EMAIL and STRAVIT_PASSWORD:
        if not is_logged_in():
            print("Auto-logging in to Stravit...")
            try:
                login_to_stravit(STRAVIT_EMAIL, STRAVIT_PASSWORD)
                print("Auto-login successful!")
            except Exception as e:
                print(f"Auto-login failed: {e}")


def background_worker():
    while True:
        try:
            auto_login_if_configured()
            if is_logged_in():
                print("Background thread: fetching challenge data...")
                url = f"{BASE_URL}/challenge/{urllib.parse.quote(DEFAULT_SLUG)}/export/activities/csv"
                csv_text = fetch_text(url, headers=request_headers("text/csv,text/plain,*/*"))
                if "Logowanie do Stravit" not in csv_text and "<form" not in csv_text[:2000]:
                    data = build_data_from_activities(parse_csv_activities(csv_text))
                    now = time.time()
                    with _cache_lock:
                        _cache[DEFAULT_SLUG] = {"created": now, "data": data}
                    print("Background thread: Cache updated successfully.")
                else:
                    print("Background thread: Stravit session expired.")
            else:
                print("Background thread: Not logged in, skipping fetch.")
        except Exception as e:
            print(f"Background thread error: {e}")
        time.sleep(900)


def load_challenge_data(slug, force=False):
    auto_login_if_configured()
    
    now = time.time()
    with _cache_lock:
        cached = _cache.get(slug)
    
    if not force and cached and now - cached["created"] < CACHE_TTL_SECONDS:
        return cached["data"]

    url = f"{BASE_URL}/challenge/{urllib.parse.quote(slug)}/export/activities/csv"
    csv_text = fetch_text(url, headers=request_headers("text/csv,text/plain,*/*"))
    if "Logowanie do Stravit" in csv_text or "<form" in csv_text[:2000]:
        raise AuthRequired("Sesja Stravit wygasla. Zaloguj sie ponownie.")

    data = build_data_from_activities(parse_csv_activities(csv_text))
    with _cache_lock:
        _cache[slug] = {"created": now, "data": data}
    return data


def _send_bytes(start_response, status, body, content_type="text/plain; charset=utf-8", headers=None):
    response_headers = [("Content-Type", content_type)]
    if headers:
        response_headers.extend(headers)
    response_headers.append(("Content-Length", str(len(body))))
    response_headers.append(("Cache-Control", "no-store"))
    start_response(f"{status} {STATUS_TEXT.get(status, 'OK')}", response_headers)
    return [body]


def _send_json(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _send_bytes(start_response, status, body, "application/json; charset=utf-8")


def app(environ, start_response):
    path = urllib.parse.urlparse(environ.get("PATH_INFO", "/")).path
    method = environ.get("REQUEST_METHOD", "GET").upper()
    query_string = environ.get("QUERY_STRING", "")
    params = urllib.parse.parse_qs(query_string)

    if method == "GET" and path == "/api/auth/status":
        return _send_json(start_response, 200, {
            "loggedIn": is_logged_in(),
            "hasMasterCredentials": bool(STRAVIT_EMAIL and STRAVIT_PASSWORD)
        })

    if path == "/api/crew/profiles":
        if method == "GET":
            return _send_json(start_response, 200, list_crew_profiles())
        return _send_json(start_response, 405, {"error": "Method not allowed"})

    if path == "/api/crew":
        crew_id = params.get("id", [None])[0]
        if not crew_id:
            return _send_json(start_response, 400, {"error": "Missing crew id parameter."})

        if method == "GET":
            crew_list = load_crew_data(crew_id)
            return _send_json(start_response, 200, crew_list)

        if method == "POST":
            try:
                length = int(environ.get("CONTENT_LENGTH", "0"))
                raw = environ.get("wsgi.input", b"").read(length).decode("utf-8") if length else "{}"
                payload = json.loads(raw or "{}")
                success = save_crew_data(crew_id, payload)
                return _send_json(start_response, 200, {"ok": success})
            except Exception as e:
                return _send_json(start_response, 400, {"error": str(e)})

    if method == "GET":
        match = re.fullmatch(r"/api/challenge/([^/]+)/data", path)
        if match:
            slug = urllib.parse.unquote(match.group(1))
            force = "true" in params.get("force", [])
            try:
                payload = load_challenge_data(slug, force=force)
                return _send_json(start_response, 200, payload)
            except AuthRequired as exc:
                return _send_json(start_response, 401, {"error": str(exc), "authRequired": True})
            except Exception as exc:
                return _send_json(start_response, 502, {"error": str(exc)})

    if method == "POST" and path == "/api/auth/login":
        try:
            length = int(environ.get("CONTENT_LENGTH", "0"))
            raw = environ.get("wsgi.input", b"").read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw or "{}")
            result = login_to_stravit(
                (payload.get("email") or "").strip(),
                payload.get("password") or "",
                bool(payload.get("remember", True)),
            )
            return _send_json(start_response, 200, result)
        except AuthRequired as exc:
            return _send_json(start_response, 401, {"error": str(exc), "authRequired": True})
        except Exception as exc:
            return _send_json(start_response, 400, {"error": str(exc)})

    if path == "/":
        path = "/dashboard.html"

    candidate = (BASE_DIR / path.lstrip("/")).resolve()
    if not str(candidate).startswith(str(BASE_DIR.resolve())):
        return _send_bytes(start_response, 404, b"Not Found")

    if candidate.exists() and candidate.is_file():
        content = candidate.read_bytes()
        content_type, _ = mimetypes.guess_type(str(candidate))
        content_type = content_type or "application/octet-stream"
        return _send_bytes(start_response, 200, content, content_type)

    return _send_bytes(start_response, 404, b"Not Found")


application = app


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        
        if parsed.path == "/api/auth/status":
            self.send_json(200, {
                "loggedIn": is_logged_in(),
                "hasMasterCredentials": bool(STRAVIT_EMAIL and STRAVIT_PASSWORD)
            })
            return

        if parsed.path == "/api/crew/profiles":
            self.send_json(200, list_crew_profiles())
            return

        if parsed.path == "/api/crew":
            crew_id = params.get("id", [None])[0]
            if not crew_id:
                self.send_json(400, {"error": "Missing crew id parameter."})
                return
            crew_list = load_crew_data(crew_id)
            self.send_json(200, crew_list)
            return

        match = re.fullmatch(r"/api/challenge/([^/]+)/data", parsed.path)
        if match:
            slug = urllib.parse.unquote(match.group(1))
            force = "true" in params.get("force", [])
            try:
                payload = load_challenge_data(slug, force=force)
                self.send_json(200, payload)
            except AuthRequired as exc:
                self.send_json(401, {"error": str(exc), "authRequired": True})
            except Exception as exc:
                self.send_json(502, {"error": str(exc)})
            return

        if parsed.path == "/":
            self.path = "/dashboard.html"
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/crew":
            try:
                crew_id = params.get("id", [None])[0]
                if not crew_id:
                    self.send_json(400, {"error": "Missing crew id parameter."})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw or "{}")
                success = save_crew_data(crew_id, payload)
                self.send_json(200, {"ok": success})
            except Exception as e:
                self.send_json(400, {"error": str(e)})
            return

        if parsed.path != "/api/auth/login":
            self.send_json(404, {"error": "Nieznany endpoint."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw or "{}")
            result = login_to_stravit(
                (payload.get("email") or "").strip(),
                payload.get("password") or "",
                bool(payload.get("remember", True)),
            )
            self.send_json(200, result)
        except AuthRequired as exc:
            self.send_json(401, {"error": str(exc), "authRequired": True})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"Dashboard: http://{HOST}:{PORT}/dashboard.html")
    print("Login Stravit: /api/auth/login")
    print("Dane CSV Stravit: /api/challenge/rywalizacja-sportowa/data")
    
    # Auto-login at startup if credentials are set
    auto_login_if_configured()
    
    # Start background cache sync thread
    t = threading.Thread(target=background_worker, daemon=True)
    t.start()
    print("Background thread started for auto-refreshing Stravit cache.")
    
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
