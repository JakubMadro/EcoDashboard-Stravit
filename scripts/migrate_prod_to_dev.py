#!/usr/bin/env python3
import sys
import os
import json

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

# Load environment variables from .env file before execution
load_env()

PROD_CONN_STR = os.environ.get("PROD_STORAGE_CONNECTION_STRING", "")
DEV_CONN_STR = os.environ.get("DEV_STORAGE_CONNECTION_STRING", "")

try:
    from azure.data.tables import TableClient
except ImportError:
    print("❌ Błąd: Brak biblioteki 'azure-data-tables'.")
    print("Zainstaluj ją za pomocą: pip install azure-data-tables")
    sys.exit(1)

def migrate_table(prod_conn, dev_conn, table_name):
    print(f"\n--- Migracja tabeli: {table_name} ---")
    try:
        # Klienci do bazy źródłowej (PROD) i docelowej (DEV)
        prod_client = TableClient.from_connection_string(conn_str=prod_conn, table_name=table_name)
        dev_client = TableClient.from_connection_string(conn_str=dev_conn, table_name=table_name)
        
        # Utwórz tabelę w DEV jeśli nie istnieje
        try:
            dev_client.create_table()
            print(f"✓ Utworzono tabelę '{table_name}' w bazie docelowej (DEV).")
        except Exception as e:
            if "TableAlreadyExists" in str(e) or "ResourceExistsError" in str(type(e)):
                print(f"i Tabela '{table_name}' już istnieje w bazie docelowej (DEV).")
            else:
                print(f"⚠️ Uwaga: Błąd podczas próby utworzenia tabeli '{table_name}':")
                print(f"  {e}\n")

        # Pobieranie danych z bazy źródłowej (PROD)
        print("Pobieranie danych z bazy źródłowej (PROD)...")
        entities = list(prod_client.list_entities())
        total = len(entities)
        print(f"Znaleziono {total} rekordów.")

        if total == 0:
            print("Brak danych do przeniesienia.")
            return

        # Przenoszenie encji
        success_count = 0
        for idx, entity in enumerate(entities, 1):
            # Oczyszczanie encji z metadanych Azure
            clean_entity = {k: v for k, v in entity.items() if not k.startswith("Timestamp") and k != "etag"}
            
            try:
                dev_client.upsert_entity(clean_entity)
                success_count += 1
                if idx % 50 == 0 or idx == total:
                    print(f"Postęp: {idx}/{total}...")
            except Exception as e:
                print(f"❌ Błąd zapisu rekordu PK={entity.get('PartitionKey')}, RK={entity.get('RowKey')}: {e}")

        print(f"✓ Zakończono migrację tabeli '{table_name}': przeniesiono {success_count}/{total} rekordów.")

    except Exception as e:
        print(f"❌ Błąd podczas migracji tabeli '{table_name}': {e}")

def main():
    global PROD_CONN_STR, DEV_CONN_STR

    # 1. Odczyt connection stringów ze zmiennych w skrypcie lub środowiskowych
    prod_conn = PROD_CONN_STR.strip() or os.environ.get("PROD_STORAGE_CONNECTION_STRING") or ""
    dev_conn = DEV_CONN_STR.strip() or os.environ.get("DEV_STORAGE_CONNECTION_STRING") or ""

    # 2. Interaktywny prompt o connection stringi jeśli brakuje
    if not prod_conn or not dev_conn:
        print("=== Skrypt migracji danych Azure Table (PROD -> DEV) ===")
        if not prod_conn:
            try:
                prod_conn = input("Podaj connection string do bazy źródłowej (PROD): ").strip()
            except KeyboardInterrupt:
                print("\nAnulowano.")
                sys.exit(0)
        if not dev_conn:
            try:
                dev_conn = input("Podaj connection string do bazy docelowej (DEV): ").strip()
            except KeyboardInterrupt:
                print("\nAnulowano.")
                sys.exit(0)

    if not prod_conn or not dev_conn:
        print("❌ Błąd: Wymagane są oba Connection Stringi (PROD i DEV).")
        sys.exit(1)

    # 3. Wybór zakresu migracji (argument lub interaktywny prompt)
    mode = "crews"
    if len(sys.argv) >= 2:
        arg_mode = sys.argv[1].lower().strip()
        if arg_mode in ["crews", "activities", "all"]:
            mode = arg_mode
    else:
        print("\nZakresy migracji:")
        print("  [crews]      - migracja wyłącznie profili/ekip (domyślnie)")
        print("  [activities] - migracja wyłącznie treningów/aktywności")
        print("  [all]        - migracja profili i treningów")
        try:
            user_choice = input("Wybierz zakres (crews / activities / all) [crews]: ").strip().lower()
            if user_choice:
                if user_choice in ["crews", "activities", "all"]:
                    mode = user_choice
                else:
                    print(f"Niepoprawny wybór. Używam domyślnego: 'crews'.")
            else:
                mode = "crews"
        except KeyboardInterrupt:
            print("\nAnulowano.")
            sys.exit(0)

    print(f"\nRozpoczynanie migracji (Tryb: {mode.upper()})...")
    
    if mode in ["all", "crews"]:
        migrate_table(prod_conn, dev_conn, "crews")
    
    if mode in ["all", "activities"]:
        migrate_table(prod_conn, dev_conn, "activities")
        
    print("\n🎉 Sukces! Migracja ukończona.")

if __name__ == "__main__":
    main()
