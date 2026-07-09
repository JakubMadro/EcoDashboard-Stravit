# EcoDashboard — Rywalizacja Sportowa 🚴‍♂️🏃‍♂️

EcoDashboard to aplikacja webowa do śledzenia aktywności sportowych i rywalizacji w ramach platformy **Stravit**. Projekt składa się z backendu w Pythonie (WSGI/Gunicorn), frontendu w HTML/JS/CSS oraz zasobów infrastruktury w Terraform wdrażanych automatycznie na Azure App Service przez GitHub Actions.

---

## 📂 Struktura Projektu

```text
├── .github/workflows/
│   └── deploy.yml          # Konfiguracja CI/CD (GitHub Actions)
├── app/                    # Kod źródłowy aplikacji webowej
│   ├── main.py             # Główna logika serwera WSGI i API
│   ├── server.py           # Punkt wejściowy dla serwera WSGI/Gunicorn
│   ├── db.py               # Integracja z bazą Azure Table Storage (lub plikami lokalnymi)
│   ├── sync.py             # Daemon synchronizacji i integracja z portalem Stravit
│   └── static/             # Pliki statyczne (index.html, style.css, app.js itp.)
├── scripts/                # Skrypty pomocnicze (administracyjne i testowe)
│   ├── backfill_strava_links.py # Uzupełnianie linków do aktywności w Strava
│   ├── migrate_prod_to_dev.py   # Migracja danych między bazami Prod i Dev
│   └── smoke_stress_test.py     # Testy UI (Playwright) oraz obciążeniowe API
├── terraform/              # Pliki konfiguracji infrastruktury w Azure (Terraform)
│   ├── main.tf
│   ├── variables.tf
│   └── outputs.tf
├── .env.example            # Szablon pliku konfiguracyjnego środowiska
├── .gitignore              # Konfiguracja ignorowanych plików w repozytorium
└── requirements.txt        # Zależności projektu w Pythonie
```

---

## 🛠️ Konfiguracja i Uruchomienie Lokalne

### 1. Instalacja Zależności
Upewnij się, że masz zainstalowanego Pythona (zalecany `3.12`) oraz utwórz wirtualne środowisko:

```bash
# Tworzenie i aktywacja wirtualnego środowiska
python3 -m venv venv
source venv/bin/activate

# Instalacja pakietów
pip install -r requirements.txt
```

### 2. Zmienne Środowiskowe
Skopiuj szablon konfiguracji `.env.example` do `.env`:

```bash
cp .env.example .env
```

Następnie uzupełnij plik `.env` swoimi własnymi danymi uwierzytelniającymi (ten plik jest dodany do `.gitignore` i nigdy nie zostanie wypchnięty do repozytorium).

### 3. Uruchomienie Serwera Lokalne
Możesz uruchomić lokalny serwer aplikacji, wpisując:

```bash
python app/server.py
```
Serwer domyślnie uruchomi się na porcie `8000` i będzie dostępny pod adresem: [http://localhost:8000](http://localhost:8000).

---

## 🚀 Skrypty Pomocnicze (`scripts/`)

Skrypty automatycznie wczytują konfigurację z pliku `.env` znajdującego się w katalogu głównym projektu.

### A. Uzupełnianie linków Strava (`backfill_strava_links.py`)
Pobiera brakujące odnośniki do serwisu Strava i aktualizuje bazę danych:

```bash
python scripts/backfill_strava_links.py [dev/prod]
```

### B. Migracja danych Azure Table (`migrate_prod_to_dev.py`)
Umożliwia szybkie przeniesienie rekordów tabel `crews` (profili) oraz `activities` (treningów) z bazy produkcyjnej do bazy deweloperskiej:

```bash
python scripts/migrate_prod_to_dev.py [crews/activities/all]
```

### C. Testy dymne i obciążeniowe (`smoke_stress_test.py`)
Skrypt testuje responsywność aplikacji i poprawność API oraz interfejsu (wymaga `playwright`):

```bash
# Instalacja Playwright do testów UI
pip install playwright
playwright install

# Uruchomienie testów UI oraz API
python scripts/smoke_stress_test.py --url http://localhost:8000 --mode all
```

---

## 🌐 Infrastruktura (Terraform)

Pliki w folderze `terraform/` opisują architekturę w chmurze Azure dla środowiska deweloperskiego oraz produkcyjnego. 

Aby wdrożyć infrastrukturę lokalnie:
1. Zainstaluj Terraform CLI.
2. Zaloguj się do konta Azure (`az login`).
3. Przejdź do katalogu `terraform/` i uruchom:
   ```bash
   terraform init
   terraform apply
   ```
> **Uwaga:** Pliki stanu `.tfstate` oraz dane wrażliwe `*.tfvars` są automatycznie ignorowane przez system kontroli wersji git.
