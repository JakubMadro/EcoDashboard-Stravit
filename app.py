import os
import urllib.parse
import json
import time
import mimetypes
import re
import threading
import logging
from pathlib import Path

# Configure structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("eco-dashboard.app")

# Configure App Insights telemetry globally if connection string is provided
APPINSIGHTS_CONN_STR = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
if APPINSIGHTS_CONN_STR:
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(connection_string=APPINSIGHTS_CONN_STR)
        logger.info("Application Insights telemetry configured successfully in app.py.")
    except Exception as e:
        logger.warning(f"Failed to configure App Insights in app.py: {e}")

from db import save_crew_data, load_crew_data, list_crew_profiles, load_activities, _get_table_client
from sync import is_logged_in, login_to_stravit, sync_activities, start_background_sync, AuthRequired, STRAVIT_EMAIL, STRAVIT_PASSWORD, DEFAULT_SLUG

BASE_DIR = Path(__file__).resolve().parent

# Configurable sync rate limiting (default 30 mins)
SYNC_RATE_LIMIT_MINUTES = int(os.environ.get("SYNC_RATE_LIMIT_MINUTES", "30"))
SYNC_RATE_LIMIT_SECONDS = SYNC_RATE_LIMIT_MINUTES * 60

# Cache configurations
CACHE_TTL_SECONDS = 3600
_cache = {}
_cache_lock = threading.Lock()
_last_sync_time = 0
_sync_lock = threading.Lock()

# Start background sync thread on startup
start_background_sync(DEFAULT_SLUG)


def build_data_from_activities(activities, crew_filter=None):
    all_dates = sorted({activity["dateStr"] for activity in activities})
    if not all_dates:
        raise RuntimeError("Brak aktywnosci do przetworzenia.")

    # Convert crew_filter to a set of lowercase strings for fast matching
    crew_set = None
    if crew_filter:
        crew_set = {name.strip().lower() for name in crew_filter if name.strip()}

    # 1. Aggregate totals for all users first (needed for rank calculation)
    all_users = {}
    for activity in activities:
        name = activity["name"]
        user = all_users.setdefault(name, {
            "distance": 0.0,
            "points": 0.0,
            "elevation": 0.0,
            "time": 0,
            "count": 0,
        })
        user["distance"] += activity["dist"]
        user["points"] += activity["pts"]
        user["elevation"] += activity["elev"]
        user["time"] += activity["timeSec"]
        user["count"] += 1

    # 2. Sort and assign ranks
    sorted_users = sorted(all_users.items(), key=lambda item: item[1]["points"], reverse=True)
    for idx, (_, user) in enumerate(sorted_users, 1):
        user["rank"] = idx

    # 3. Build detailed data only for the filtered crew (or all if crew_set is None)
    users = {}
    for activity in activities:
        name = activity["name"]
        name_lower = name.lower()
        
        # If crew filter is active, skip details of non-crew members
        if crew_set is not None and name_lower not in crew_set:
            continue

        if name not in users:
            meta = all_users[name]
            users[name] = {
                "distance": round(meta["distance"], 2),
                "points": round(meta["points"], 3),
                "elevation": round(meta["elevation"], 1),
                "time": meta["time"],
                "count": meta["count"],
                "rank": meta["rank"],
                "daily": {date: {"points": 0.0, "distance": 0.0} for date in all_dates},
                "byType": {},
            }

        user = users[name]
        user["daily"][activity["dateStr"]]["points"] += activity["pts"]
        user["daily"][activity["dateStr"]]["distance"] += activity["dist"]

        by_type = user["byType"].setdefault(activity["type"], {
            "count": 0,
            "distance": 0.0,
            "points": 0.0,
            "time": 0,
        })
        by_type["count"] += 1
        by_type["distance"] += activity["dist"]
        by_type["points"] += activity["pts"]
        by_type["time"] += activity["timeSec"]

    # Round daily and sport details
    for user in users.values():
        for day in user["daily"].values():
            day["points"] = round(day["points"], 3)
            day["distance"] = round(day["distance"], 2)
        for by_type in user["byType"].values():
            by_type["distance"] = round(by_type["distance"], 2)
            by_type["points"] = round(by_type["points"], 3)

    totals = {
        "distance": round(sum(user["distance"] for user in all_users.values()), 1),
        "points": round(sum(user["points"] for user in all_users.values()), 1),
        "count": len(activities),
        "time": sum(user["time"] for user in all_users.values()),
    }

    names = sorted(all_users.keys(), key=lambda name: name.casefold())
    top = sorted(
        [{"name": name, "points": user["points"], "rank": user["rank"]} for name, user in all_users.items()],
        key=lambda item: item["rank"],
    )[:10]

    # Get recent activities of the filtered crew (or all if crew_set is None)
    crew_acts = []
    for act in activities:
        name_lower = act["name"].lower()
        if crew_set is None or name_lower in crew_set:
            crew_acts.append({
                "name": act["name"],
                "title": act["title"],
                "dist": float(act["dist"]),
                "pts": float(act["pts"]),
                "elev": float(act["elev"]),
                "timeSec": int(act["timeSec"]),
                "type": act["type"],
                "dateStr": act["dateStr"],
                "dateRaw": act.get("dateRaw", act["dateStr"]),
                "stravaUrl": act.get("stravaUrl"),
            })
    
    crew_acts.sort(key=lambda a: a.get("dateRaw", a["dateStr"]), reverse=True)
    recent_acts = crew_acts[:10]

    return {
        "source": "stravit-database-nosql",
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dateRange": [all_dates[0], all_dates[-1]],
        "allDates": all_dates,
        "totalUsers": len(all_users),
        "totals": totals,
        "topLeaders": top,
        "users": users,
        "recentActivities": recent_acts,
    }


def load_challenge_data(slug, force=False, full_import=False):
    global _last_sync_time
    now = time.time()
    
    if force:
        # Clear cache to force database rebuild
        with _cache_lock:
            _cache.pop(slug, None)

    # Rebuild from Table Storage
    with _cache_lock:
        cached = _cache.get(slug)
        if cached and now - cached["created"] < CACHE_TTL_SECONDS:
            return cached["data"]

    # Load from DB (or local JSON fallback)
    activities = load_activities(slug)
    # The statistics will be aggregated dynamically on request to support custom crew filters, 
    # but we cache the raw loaded database activities to speed up concurrent filtered requests!
    with _cache_lock:
        _cache[slug] = {"created": now, "data": activities}
    return activities


def _send_bytes(start_response, status, body, content_type="text/plain; charset=utf-8", headers=None):
    response_headers = [("Content-Type", content_type)]
    if headers:
        response_headers.extend(headers)
    response_headers.append(("Content-Length", str(len(body))))
    
    # Disable caching for API calls
    if "/api/" in content_type or "application/json" in content_type:
        response_headers.append(("Cache-Control", "no-store, no-cache, must-revalidate"))
        
    if isinstance(status, int):
        phrases = {
            200: "200 OK",
            201: "201 Created",
            202: "202 Accepted",
            400: "400 Bad Request",
            401: "401 Unauthorized",
            403: "403 Forbidden",
            404: "404 Not Found",
            410: "410 Gone",
            500: "500 Internal Server Error",
            502: "502 Bad Gateway"
        }
        status_str = phrases.get(status, f"{status} Unknown")
    else:
        status_str = status
        
    start_response(status_str, response_headers)
    return [body]


def _send_json(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _send_bytes(start_response, status, body, "application/json; charset=utf-8")


def app(environ, start_response):
    path = urllib.parse.urlparse(environ.get("PATH_INFO", "/")).path
    method = environ.get("REQUEST_METHOD", "GET").upper()
    query_string = environ.get("QUERY_STRING", "")
    params = urllib.parse.parse_qs(query_string)

    # Standard browser / Azure health probe files
    if method == "GET":
        if path == "/favicon.ico" or path.startswith("/apple-touch-icon"):
            start_response("204 No Content", [])
            return [b""]
        if path.startswith("/robots") and path.endswith(".txt"):
            body = b"User-agent: *\nDisallow: /\n"
            return _send_bytes(start_response, 200, body, "text/plain; charset=utf-8")

    # API v1 Versioned endpoints
    if path.startswith("/api/v1/"):
        api_path = path[7:] # Remove '/api/v1' prefix
        
        if method == "GET" and api_path == "/auth/status":
            return _send_json(start_response, 200, {
                "loggedIn": is_logged_in(),
                "hasMasterCredentials": bool(STRAVIT_EMAIL and STRAVIT_PASSWORD)
            })

        if method == "POST" and api_path == "/auth/login":
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

        if api_path == "/crew/profiles":
            if method == "GET":
                return _send_json(start_response, 200, list_crew_profiles())
            return _send_json(start_response, 405, {"error": "Method not allowed"})

        if api_path == "/crew":
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

        # Get aggregated challenge statistics
        match = re.fullmatch(r"/challenge/([^/]+)/data", api_path)
        if match and method == "GET":
            slug = urllib.parse.unquote(match.group(1))
            force = "true" in params.get("force", [])
            full_import = "true" in params.get("full_import", [])
            
            # Read crew filter list
            crew_param = params.get("crew", [None])[0]
            crew_filter = crew_param.split(",") if crew_param else None

            try:
                # Load activities from DB/cache
                activities = load_challenge_data(slug, force=force, full_import=full_import)
                # Aggregate on backend, filtering detailed history to selected crew members only
                payload = build_data_from_activities(activities, crew_filter=crew_filter)
                return _send_json(start_response, 200, payload)
            except AuthRequired as exc:
                return _send_json(start_response, 401, {"error": str(exc), "authRequired": True})
            except Exception as exc:
                return _send_json(start_response, 502, {"error": str(exc)})

        # Get raw activities list for admin validation
        match_acts = re.fullmatch(r"/challenge/([^/]+)/activities", api_path)
        if match_acts and method == "GET":
            slug = urllib.parse.unquote(match_acts.group(1))
            try:
                activities = load_activities(slug) or []
                return _send_json(start_response, 200, activities)
            except Exception as e:
                return _send_json(start_response, 502, {"error": str(e)})

        # Get sorted list of unique athlete names
        match_names = re.fullmatch(r"/challenge/([^/]+)/names", api_path)
        if match_names and method == "GET":
            slug = urllib.parse.unquote(match_names.group(1))
            try:
                activities = load_challenge_data(slug, force=False)
                names = sorted({act["name"] for act in activities}, key=lambda n: n.casefold())
                return _send_json(start_response, 200, names)
            except Exception as e:
                return _send_json(start_response, 502, {"error": str(e)})

        # Trigger asynchronous sync job in Table Storage
        match_sync = re.fullmatch(r"/challenge/([^/]+)/sync", api_path)
        if match_sync and method == "POST":
            slug = urllib.parse.unquote(match_sync.group(1))
            try:
                length = int(environ.get("CONTENT_LENGTH", "0"))
                raw = environ.get("wsgi.input", b"").read(length).decode("utf-8") if length else "{}"
                payload = json.loads(raw or "{}")
                full_imp = payload.get("full_import", False)
                
                from db import request_sync_job
                success = request_sync_job(slug, full_import=full_imp)
                return _send_json(start_response, 202, {"ok": success, "status": "requested"})
            except Exception as e:
                return _send_json(start_response, 400, {"error": str(e)})

        # Get status of sync job
        match_sync_status = re.fullmatch(r"/challenge/([^/]+)/sync/status", api_path)
        if match_sync_status and method == "GET":
            slug = urllib.parse.unquote(match_sync_status.group(1))
            try:
                from db import get_sync_job_status
                status_info = get_sync_job_status(slug)
                return _send_json(start_response, 200, status_info)
            except Exception as e:
                return _send_json(start_response, 502, {"error": str(e)})

        # Get log history of sync runs
        match_logs = re.fullmatch(r"/challenge/([^/]+)/synclogs", api_path)
        if match_logs and method == "GET":
            slug = urllib.parse.unquote(match_logs.group(1))
            try:
                table_client = _get_table_client("synclogs")
                if table_client:
                    entities = list(table_client.query_entities(query_filter=f"PartitionKey eq '{slug}'"))
                    entities.sort(key=lambda e: e.get("completed_at", ""), reverse=True)
                    logs = []
                    for ent in entities[:30]:
                        logs.append({
                            "started_at": ent.get("started_at", ""),
                            "completed_at": ent.get("completed_at", ""),
                            "status": ent.get("status", ""),
                            "records_pulled": ent.get("records_pulled", 0),
                            "error": ent.get("error", ""),
                            "trigger_type": ent.get("trigger_type", ""),
                        })
                    return _send_json(start_response, 200, logs)
                else:
                    # Local fallback
                    local_file = BASE_DIR / "data" / f"sync_logs_{slug}.jsonl"
                    logs = []
                    if local_file.exists():
                        with open(local_file, "r", encoding="utf-8") as f:
                            for line in f:
                                if line.strip():
                                    logs.append(json.loads(line.strip()))
                        logs.reverse()
                    return _send_json(start_response, 200, logs[:30])
            except Exception as e:
                return _send_json(start_response, 502, {"error": str(e)})

        # Get last 20 workouts imported to storage account
        match_recent = re.fullmatch(r"/challenge/([^/]+)/recent-imports", api_path)
        if match_recent and method == "GET":
            slug = urllib.parse.unquote(match_recent.group(1))
            try:
                table_client = _get_table_client("activities")
                if table_client:
                    entities = list(table_client.query_entities(query_filter=f"PartitionKey eq '{slug}'"))
                    entities.sort(key=lambda e: e.metadata.get("timestamp") or e.get("dateRaw", ""), reverse=True)
                    recent = []
                    for ent in entities[:20]:
                        recent.append({
                            "name": ent.get("name", ""),
                            "sport": ent.get("type", ent.get("sport", "")),
                            "date": ent.get("dateRaw", ent.get("dateStr", "")),
                            "dist": ent.get("dist", 0.0),
                            "timeSec": ent.get("timeSec", 0),
                            "pts": ent.get("pts", 0.0),
                            "title": ent.get("title", ""),
                            "stravaUrl": ent.get("stravaUrl", ""),
                            "imported_at": ent.metadata.get("timestamp").strftime("%Y-%m-%dT%H:%M:%SZ") if ent.metadata.get("timestamp") else ""
                        })
                    return _send_json(start_response, 200, recent)
                else:
                    # Local fallback
                    local_file = BASE_DIR / "data" / f"activities_{slug}.json"
                    if local_file.exists():
                        data = json.loads(local_file.read_text(encoding="utf-8"))
                        data.reverse()
                        recent = []
                        for ent in data[:20]:
                            recent.append({
                                "name": ent.get("name", ""),
                                "sport": ent.get("type", ent.get("sport", "")),
                                "date": ent.get("dateRaw", ent.get("dateStr", "")),
                                "dist": ent.get("dist", 0.0),
                                "timeSec": ent.get("timeSec", 0),
                                "pts": ent.get("pts", 0.0),
                                "title": ent.get("title", ""),
                                "stravaUrl": ent.get("stravaUrl", ""),
                                "imported_at": ""
                            })
                        return _send_json(start_response, 200, recent)
                    return _send_json(start_response, 200, [])
            except Exception as e:
                return _send_json(start_response, 502, {"error": str(e)})

        return _send_json(start_response, 404, {"error": "API Route Not Found"})

    # Reject deprecated old non-versioned /api/ endpoints to keep codebase clean
    if path.startswith("/api/"):
        return _send_json(start_response, 410, {"error": "API version 1.0 required. Use /api/v1/ prefix."})

    # Serve static assets (HTML, CSS, JS) from base directory
    if path == "/":
        path = "/index.html"

    candidate = (BASE_DIR / path.lstrip("/")).resolve()
    if not str(candidate).startswith(str(BASE_DIR.resolve())):
        return _send_bytes(start_response, 404, b"Not Found")

    if candidate.exists() and candidate.is_file():
        content = candidate.read_bytes()
        content_type, _ = mimetypes.guess_type(str(candidate))
        content_type = content_type or "application/octet-stream"
        return _send_bytes(start_response, 200, content, content_type)

    return _send_bytes(start_response, 404, b"Not Found")


# Wrap WSGI app with OpenTelemetry tracing if configured
if APPINSIGHTS_CONN_STR:
    try:
        from opentelemetry.instrumentation.wsgi import OpenTelemetryMiddleware
        app = OpenTelemetryMiddleware(app)
        logger.info("WSGI application wrapped with OpenTelemetry middleware in app.py.")
    except Exception as e:
        logger.warning(f"Failed to wrap WSGI app with OpenTelemetry: {e}")

application = app
