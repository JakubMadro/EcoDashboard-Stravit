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

from db import save_crew_data, load_crew_data, list_crew_profiles, load_activities
from sync import is_logged_in, login_to_stravit, sync_activities, start_background_sync, AuthRequired, STRAVIT_EMAIL, STRAVIT_PASSWORD, DEFAULT_SLUG

BASE_DIR = Path(__file__).resolve().parent

# Configurable sync rate limiting (default 15 mins)
SYNC_RATE_LIMIT_MINUTES = int(os.environ.get("SYNC_RATE_LIMIT_MINUTES", "15"))
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

    return {
        "source": "stravit-database-nosql",
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dateRange": [all_dates[0], all_dates[-1]],
        "allDates": all_dates,
        "totalUsers": len(all_users),
        "totals": totals,
        "topLeaders": top,
        "users": users,
    }


def load_challenge_data(slug, force=False, full_import=False):
    global _last_sync_time
    now = time.time()
    
    if force:
        with _sync_lock:
            # Enforce rate limit (skip Stravit API hit if within limit)
            if now - _last_sync_time < SYNC_RATE_LIMIT_SECONDS:
                logger.info(f"Sync rate limit active. Bypassing Stravit API hit. Elapsed: {now - _last_sync_time:.0f}s")
            else:
                try:
                    logger.info(f"Manual Sync triggered. Full import: {full_import}.")
                    sync_activities(slug, full_import=full_import)
                    _last_sync_time = now
                    # Clear cache to force database rebuild
                    with _cache_lock:
                        _cache.pop(slug, None)
                except Exception as e:
                    logger.error(f"Error during manual sync trigger: {e}")
                    raise

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
        
    start_response(f"{status} OK" if isinstance(status, int) else status, response_headers)
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
