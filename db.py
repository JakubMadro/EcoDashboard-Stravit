import json
import os
import time
import hashlib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AZURE_STORAGE_CONN_STR = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
PROFILE_INDEX_BLOB = "profiles_index.json"

try:
    from azure.data.tables import TableClient
    HAS_AZURE = True
except ImportError:
    HAS_AZURE = False

_table_clients = {}
_table_initialized = {}
_local_activity_hashes_cache = {}


def _get_table_client(table_name):
    global _table_clients, _table_initialized
    if not HAS_AZURE or not AZURE_STORAGE_CONN_STR:
        return None

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


def make_activity_id(athlete, date_str, title, distance, time_sec):
    key_string = f"{athlete}|{date_str}|{title}|{distance}|{time_sec}"
    return hashlib.sha256(key_string.encode('utf-8')).hexdigest()


# =====================================================================
# CREWS & PROFILES LOGIC
# =====================================================================

def save_profile_index(index_payload):
    table_client = _get_table_client("crews")
    if table_client:
        try:
            entity = {
                "PartitionKey": "index",
                "RowKey": "profiles-index",
                "payload": json.dumps(index_payload, ensure_ascii=False)
            }
            table_client.upsert_entity(entity)
            print("Saved profile index to Azure Table Storage.")
            return True
        except Exception as e:
            print(f"Error saving profile index to Azure Table: {e}")

    try:
        local_dir = BASE_DIR / "data" / "crews"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / PROFILE_INDEX_BLOB).write_text(json.dumps(index_payload, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        print(f"Error saving profile index locally: {e}")
        return False


def load_profile_summaries():
    payload = []
    table_client = _get_table_client("crews")
    if table_client:
        try:
            try:
                entity = table_client.get_entity(partition_key="index", row_key="profiles-index")
                data_str = entity.get("payload")
                if data_str:
                    loaded = json.loads(data_str)
                    if isinstance(loaded, list):
                        payload = loaded
            except Exception:
                pass
        except Exception as e:
            print(f"Error loading profile index from Azure Table: {e}")

    if not payload:
        try:
            local_file = BASE_DIR / "data" / "crews" / PROFILE_INDEX_BLOB
            if local_file.exists():
                loaded = json.loads(local_file.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    payload = loaded
        except Exception as e:
            print(f"Error loading profile index locally: {e}")

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

    data_str = json.dumps(profile_data, ensure_ascii=False)
    table_client = _get_table_client("crews")
    if table_client:
        try:
            entity = {
                "PartitionKey": "profiles",
                "RowKey": profile_data["id"],
                "me": profile_data["me"],
                "members": json.dumps(profile_data["members"], ensure_ascii=False)
            }
            table_client.upsert_entity(entity)
            print(f"Saved profile {profile_data['id']} to Azure Table Storage.")
        except Exception as e:
            print(f"Error saving profile to Azure Table: {e}")

    try:
        local_dir = BASE_DIR / "data" / "crews" / "profiles"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / f"{profile_data['id']}.json").write_text(data_str, encoding="utf-8")
        print(f"Saved profile {profile_data['id']} to local file system.")
    except Exception as e:
        print(f"Error saving profile locally: {e}")

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
    if table_client:
        try:
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
        except Exception as e:
            print(f"Error loading profile from Azure Table: {e}")

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


# =====================================================================
# ACTIVITIES LOGIC
# =====================================================================

def save_activities_batch(slug, activities_list):
    table_client = _get_table_client("activities")
    if table_client:
        try:
            # Azure Table transaction allows up to 100 operations in a single batch.
            for i in range(0, len(activities_list), 100):
                chunk = activities_list[i:i + 100]
                operations = []
                for act in chunk:
                    row_key = make_activity_id(
                        act["name"], act["dateStr"], act["title"], act["dist"], act["timeSec"]
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
                    }
                    operations.append(("upsert", entity))
                table_client.submit_transaction(operations)
            print(f"Saved {len(activities_list)} activities to Azure Table Storage in batches.", flush=True)
        except Exception as e:
            print(f"Error saving activities batch to Azure Table: {e}", flush=True)

    # Backup locally (Merge new activities with existing ones)
    try:
        local_file = BASE_DIR / "data" / f"activities_{slug}.json"
        existing = []
        if local_file.exists():
            try:
                existing = json.loads(local_file.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []

        # Merge based on hash to avoid duplicates
        merged = {}
        for act in existing:
            h = make_activity_id(act["name"], act["dateStr"], act["title"], act["dist"], act["timeSec"])
            merged[h] = act
        for act in activities_list:
            h = make_activity_id(act["name"], act["dateStr"], act["title"], act["dist"], act["timeSec"])
            merged[h] = act

        local_file.parent.mkdir(parents=True, exist_ok=True)
        local_file.write_text(json.dumps(list(merged.values()), ensure_ascii=False), encoding="utf-8")
        
        # Clear local cache to force refresh on next check
        global _local_activity_hashes_cache
        _local_activity_hashes_cache.pop(slug, None)
        print(f"Saved {len(activities_list)} activities to local backup (total merged: {len(merged)}).", flush=True)
    except Exception as e:
        print(f"Error saving activities backup locally: {e}", flush=True)


def load_activities(slug):
    table_client = _get_table_client("activities")
    if table_client:
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
                })
            return activities
        except Exception as e:
            print(f"Error loading activities from Azure Table: {e}", flush=True)

    try:
        local_file = BASE_DIR / "data" / f"activities_{slug}.json"
        if local_file.exists():
            return json.loads(local_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error loading activities locally: {e}", flush=True)
    return []


def has_activity(slug, activity_id):
    table_client = _get_table_client("activities")
    if table_client:
        try:
            table_client.get_entity(partition_key=slug, row_key=activity_id)
            return True
        except Exception:
            pass

    try:
        local_file = BASE_DIR / "data" / f"activities_{slug}.json"
        if local_file.exists():
            local_data = json.loads(local_file.read_text(encoding="utf-8"))
            global _local_activity_hashes_cache
            if slug not in _local_activity_hashes_cache:
                hashes = set()
                for act in local_data:
                    h = make_activity_id(act["name"], act["dateStr"], act["title"], act["dist"], act["timeSec"])
                    hashes.add(h)
                _local_activity_hashes_cache[slug] = hashes
            return activity_id in _local_activity_hashes_cache[slug]
    except Exception:
        pass
    return False
