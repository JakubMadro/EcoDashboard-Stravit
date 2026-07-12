import json
import os
import time
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger("eco-dashboard.db")

# Load .env file if it exists (for local development convenience)
def load_env():
    for path in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        env_path = path / ".env"
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        os.environ[key.strip()] = val.strip().strip('"').strip("'")
            break

load_env()

AZURE_STORAGE_CONN_STR = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
if not AZURE_STORAGE_CONN_STR:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING environment variable is required.")

try:
    from azure.data.tables import TableClient, UpdateMode
    from azure.core import MatchConditions
    HAS_AZURE = True
except ImportError:
    HAS_AZURE = False
    raise RuntimeError("Brak biblioteki 'azure-data-tables'. Upewnij się, że jest zainstalowana.")

_table_clients = {}
_table_initialized = {}


def _get_table_client(table_name):
    global _table_clients, _table_initialized
    if table_name not in _table_clients:
        try:
            client = TableClient.from_connection_string(conn_str=AZURE_STORAGE_CONN_STR, table_name=table_name)
            _table_clients[table_name] = client
        except Exception as e:
            print(f"Error creating TableClient for {table_name}: {e}", flush=True)
            return None

    client = _table_clients[table_name]
    if not _table_initialized.get(table_name):
        try:
            client.create_table()
        except Exception:
            pass
        _table_initialized[table_name] = True

    return client


def make_activity_id(athlete, date_raw, title, distance, time_sec):
    # Ensure stable RowKey by using SHA-256 of unique fields including full date_raw with time
    raw = f"{athlete}|{date_raw}|{title}|{distance}|{time_sec}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# =====================================================================
# CREWS & PROFILES LOGIC
# =====================================================================

def save_profile_index(index_payload):
    table_client = _get_table_client("crews")
    if not table_client:
        return False
    try:
        entity = {
            "PartitionKey": "index",
            "RowKey": "profiles-index",
            "payload": json.dumps(index_payload, ensure_ascii=False)
        }
        table_client.upsert_entity(entity)
        print("Saved profile index to Azure Table Storage.", flush=True)
        return True
    except Exception as e:
        print(f"Error saving profile index to Azure Table: {e}", flush=True)
        return False


def load_profile_summaries():
    payload = []
    table_client = _get_table_client("crews")
    if not table_client:
        return []
    try:
        entity = table_client.get_entity(partition_key="index", row_key="profiles-index")
        data_str = entity.get("payload")
        if data_str:
            loaded = json.loads(data_str)
            if isinstance(loaded, list):
                payload = loaded
    except Exception:
        pass
    return [p for p in payload if isinstance(p, dict) and p.get("me") and p.get("me").strip()]


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

    table_client = _get_table_client("crews")
    if not table_client:
        return False
    try:
        entity = {
            "PartitionKey": "profiles",
            "RowKey": profile_data["id"],
            "me": profile_data["me"],
            "members": json.dumps(profile_data["members"], ensure_ascii=False)
        }
        table_client.upsert_entity(entity)
        print(f"Saved profile {profile_data['id']} to Azure Table Storage.", flush=True)
    except Exception as e:
        print(f"Error saving profile to Azure Table: {e}", flush=True)
        return False

    if profile_data["me"]:
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
    table_client = _get_table_client("crews")
    if not table_client:
        return {}
    try:
        entity = table_client.get_entity(partition_key="profiles", row_key=crew_id)
        me = entity.get("me") or ""
        members_str = entity.get("members") or "[]"
        members = json.loads(members_str)
        return {
            "id": crew_id,
            "me": me,
            "members": members
        }
    except Exception:
        pass
    return {}


def list_crew_profiles():
    return load_profile_summaries()


# =====================================================================
# ACTIVITIES LOGIC
# =====================================================================

def save_activities_batch(slug, activities_list):
    table_client = _get_table_client("activities")
    if not table_client:
        return
    
    # De-duplicate by RowKey to prevent Azure Table transaction errors (same RowKey cannot be in a batch)
    seen_keys = set()
    unique_activities = []
    for act in activities_list:
        row_key = make_activity_id(
            act["name"], act.get("dateRaw", act["dateStr"]), act["title"], act["dist"], act["timeSec"]
        )
        if row_key not in seen_keys:
            seen_keys.add(row_key)
            unique_activities.append(act)

    logger.info(f"DB: Zapisywanie {len(unique_activities)} unikalnych aktywności do Azure Table Storage w paczkach po 100...")
    saved_count = 0
    for i in range(0, len(unique_activities), 100):
        chunk = unique_activities[i:i + 100]
        operations = []
        for act in chunk:
            row_key = make_activity_id(
                act["name"], act.get("dateRaw", act["dateStr"]), act["title"], act["dist"], act["timeSec"]
            )
            entity = {
                "PartitionKey": slug,
                "RowKey": row_key,
                "name": act["name"],
                "title": act["title"],
                "dist": float(act["dist"]),
                "pts": float(act["pts"]),
                "elev": float(act["elev"]),
                "timeSec": int(act["timeSec"]),
                "type": act["type"],
                "dateStr": act["dateStr"],
                "dateRaw": act.get("dateRaw", act["dateStr"]),
            }
            if "stravaUrl" in act and act["stravaUrl"]:
                entity["stravaUrl"] = act["stravaUrl"]
            operations.append(("upsert", entity))
        try:
            table_client.submit_transaction(operations)
            saved_count += len(chunk)
        except Exception as e:
            logger.error(f"DB: Błąd zapisu paczki {i}-{i+len(chunk)} do Azure Table: {e}")
    logger.info(f"DB: Zapisano pomyślnie {saved_count} z {len(unique_activities)} unikalnych aktywności w Azure Table Storage.")


def load_activities(slug):
    table_client = _get_table_client("activities")
    if not table_client:
        return []
    try:
        entities = table_client.query_entities(query_filter=f"PartitionKey eq '{slug}'")
        activities = []
        for ent in entities:
            activities.append({
                "name": ent.get("name"),
                "title": ent.get("title"),
                "dist": float(ent.get("dist") or 0.0),
                "pts": float(ent.get("pts") or 0.0),
                "elev": float(ent.get("elev") or 0.0),
                "timeSec": int(ent.get("timeSec") or 0),
                "type": ent.get("type"),
                "dateStr": ent.get("dateStr"),
                "dateRaw": ent.get("dateRaw", ent.get("dateStr")),
                "stravaUrl": ent.get("stravaUrl"),
            })
        return activities
    except Exception as e:
        print(f"Error loading activities from Azure Table: {e}", flush=True)
    return []


def has_activity(slug, activity_id):
    table_client = _get_table_client("activities")
    if not table_client:
        return False
    try:
        table_client.get_entity(partition_key=slug, row_key=activity_id)
        return True
    except Exception:
        pass
    return False


def get_sync_job_status(slug):
    table_client = _get_table_client("syncjobs")
    if not table_client:
        return {"status": "idle", "full_import": False, "error": ""}
    try:
        entity = table_client.get_entity(partition_key="sync_job", row_key=slug)
        return {
            "status": entity.get("status", "idle"),
            "full_import": entity.get("full_import", False),
            "error": entity.get("error", ""),
            "requested_at": entity.get("requested_at", ""),
            "started_at": entity.get("started_at", ""),
            "completed_at": entity.get("completed_at", ""),
        }
    except Exception:
        pass
    return {"status": "idle", "full_import": False, "error": ""}


def request_sync_job(slug, full_import=False):
    table_client = _get_table_client("syncjobs")
    if not table_client:
        return False
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        entity = {
            "PartitionKey": "sync_job",
            "RowKey": slug,
            "status": "requested",
            "full_import": full_import,
            "error": "",
            "requested_at": now_str,
        }
        table_client.upsert_entity(entity)
        return True
    except Exception as e:
        logger.error(f"DB: Blad zapisu sync job do Azure: {e}")
        return False


def start_sync_job(slug, is_periodic=False):
    table_client = _get_table_client("syncjobs")
    if not table_client:
        return False
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        entity = table_client.get_entity(partition_key="sync_job", row_key=slug)
        curr_status = entity.get("status", "idle")
        if is_periodic:
            if curr_status not in ("idle", "failed"):
                return False
        else:
            if curr_status != "requested":
                return False
        entity["status"] = "running"
        entity["started_at"] = now_str
        table_client.update_entity(
            entity, 
            mode=UpdateMode.REPLACE, 
            etag=entity.metadata.get("etag"), 
            match_condition=MatchConditions.IfNotModified
        )
        return True
    except Exception as e:
        logger.error(f"DB: Blad start_sync_job: {e}")
        return False


def complete_sync_job(slug, error=None):
    table_client = _get_table_client("syncjobs")
    if not table_client:
        return False
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        entity = table_client.get_entity(partition_key="sync_job", row_key=slug)
        entity["status"] = "failed" if error else "idle"
        entity["error"] = error or ""
        entity["completed_at"] = now_str
        table_client.update_entity(entity, mode=UpdateMode.REPLACE)
        return True
    except Exception as e:
        logger.error(f"DB: Blad zakonczenia sync job w Azure: {e}")
        return False


def write_sync_log(slug, started_at, completed_at, status, records_pulled, error_msg, trigger_type):
    table_client = _get_table_client("synclogs")
    if not table_client:
        return False
    row_key = completed_at.replace(":", "-")
    try:
        entity = {
            "PartitionKey": slug,
            "RowKey": row_key,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": status,
            "records_pulled": int(records_pulled),
            "error": error_msg or "",
            "trigger_type": trigger_type,
        }
        table_client.upsert_entity(entity)
        return True
    except Exception as e:
        logger.error(f"DB: Blad zapisu logu sync do Azure: {e}")
        return False
