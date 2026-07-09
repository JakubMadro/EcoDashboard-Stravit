#!/usr/bin/env python3
"""
Skrypt do automatycznych testów dymnych (Smoke Tests) oraz obciążeniowych (Stress Tests)
dla aplikacji EcoDashboard.

Wymagania dla testu UI (Playwright):
    pip install playwright
    playwright install

Uruchomienie:
    python smoke_stress_test.py --url http://localhost:8000 --mode all
"""

import sys
import time
import argparse
import urllib.request
import urllib.error
import concurrent.futures
import json

# Spróbuj zaimportować Playwright do testów UI
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


def run_ui_smoke_test(url, email=None, password=None):
    """
    Test dymny UI - odpala przeglądarkę, klika po elementach,
    sprawdza responsywność i zapisuje zrzut ekranu.
    """
    print("\n" + "="*50)
    print("🚀 ROZPOCZYNAMY UI SMOKE TEST (PLAYWRIGHT)")
    print("="*50)

    if not HAS_PLAYWRIGHT:
        print("❌ Błąd: Biblioteka 'playwright' nie jest zainstalowana.")
        print("Aby zainstalować, uruchom w terminalu:")
        print("  pip install playwright")
        print("  playwright install")
        return False

    with sync_playwright() as p:
        print("-> Uruchamianie przeglądarki Chromium w tle (headless)...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        # Przechwytywanie błędów JS w konsoli
        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(err.message))

        try:
            print(f"-> Nawigacja do strony: {url}")
            page.goto(url, timeout=15000)
            page.wait_for_load_state("networkidle")

            # 1. Sprawdź tytuł strony
            title = page.title()
            print(f"✓ Strona załadowana. Tytuł: '{title}'")

            # 2. Sprawdź widoczność głównych kontenerów
            sidebar = page.locator("#dashboardSidebar")
            main_content = page.locator(".dashboard-main")
            
            if sidebar.is_visible():
                print("✓ Panel boczny (Sidebar) jest widoczny.")
            else:
                print("⚠️ Ostrzeżenie: Panel boczny nie jest widoczny.")

            if main_content.is_visible():
                print("✓ Główna sekcja z wykresami jest widoczna.")
            else:
                print("❌ Błąd: Główna sekcja z wykresami nie załadowała się!")
                return False

            # 3. Logowanie (jeśli podano dane)
            if email and password:
                print("-> Próba logowania do Stravit...")
                page.fill("#authEmail", email)
                page.fill("#authPassword", password)
                page.click("#authBtn")
                
                # Czekaj na schowanie panelu lub informację o sukcesie
                page.wait_for_timeout(2000)
                print("✓ Wysłano formularz logowania.")

            # 4. Sprawdzenie przycisku zwijania panelu (Burger)
            burger_btn = page.locator("#toggleSidebarBtnDesktop")
            if burger_btn.is_visible():
                print("-> Klikam przycisk zwijania panelu bocznego (hamburger)...")
                burger_btn.click()
                page.wait_for_timeout(500) # Czekaj na animację CSS
                
                # Sprawdź czy klasa sidebar-collapsed została nadana
                grid = page.locator("#dashboardGridLayout")
                is_collapsed = "sidebar-collapsed" in (grid.get_attribute("class") or "")
                print(f"✓ Panel boczny zwinięty: {is_collapsed}")

                # Przywróć panel
                burger_btn.click()
                page.wait_for_timeout(500)
                print("✓ Panel boczny rozłożony ponownie.")
            else:
                print("❌ Błąd: Przycisk zwijania panelu bocznego (#toggleSidebarBtnDesktop) jest niewidoczny!")

            # 5. Sprawdzenie zoomu wykresu (modal)
            expand_btn = page.locator(".expand-chart-btn").first
            if expand_btn.is_visible():
                print("-> Klikam przycisk powiększenia pierwszego wykresu...")
                expand_btn.click()
                page.wait_for_timeout(500)
                
                modal = page.locator("#chartModal")
                is_modal_visible = "show" in (modal.get_attribute("class") or "")
                print(f"✓ Okno powiększenia wykresu (modal) jest widoczne: {is_modal_visible}")

                # Zamknij klawiszem ESC
                print("-> Zamykam modal klawiszem ESC...")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                
                is_modal_closed = not "show" in (modal.get_attribute("class") or "")
                print(f"✓ Okno powiększenia wykresu zostało zamknięte: {is_modal_closed}")
            else:
                print("⚠️ Ostrzeżenie: Przycisk powiększenia wykresu (.expand-chart-btn) nie został znaleziony.")

            # 6. Zapisz zrzut ekranu do weryfikacji wizualnej
            screenshot_path = "screenshot_smoke_test.png"
            page.screenshot(path=screenshot_path)
            print(f"✓ Zapisano zrzut ekranu do: {screenshot_path}")

            # 7. Podsumowanie błędów JS
            if js_errors:
                print(f"❌ Wykryto {len(js_errors)} błędów JavaScript w konsoli:")
                for err in js_errors[:5]:
                    print(f"   - {err}")
                return False
            else:
                print("✓ Brak błędów JavaScript w konsoli przeglądarki.")

            print("\n🎉 WYNIK: UI SMOKE TEST ZAKOŃCZONY SUKCESEM!")
            return True

        except Exception as e:
            print(f"❌ Wystąpił błąd podczas testu UI: {e}")
            return False
        finally:
            browser.close()


def run_api_stress_test(base_url, requests_count=50, concurrency=5):
    """
    Test obciążeniowy API (Stress Test) - wysyła wiele równoległych zapytań
    do backendu bez używania zewnętrznych bibliotek.
    """
    print("\n" + "="*50)
    print(f"🔥 ROZPOCZYNAMY API STRESS TEST (Obciążenie: {requests_count} zapytań)")
    print(f"   Współbieżność: {concurrency} wątków równoległych")
    print("="*50)

    # Endpointy do testowania
    endpoints = [
        f"{base_url}/api/v1/challenge/rywalizacja-sportowa/data",
        f"{base_url}/api/v1/crew/profiles",
        f"{base_url}/api/v1/crew?id=test-smoke-id"
    ]

    results = []
    
    def fetch_endpoint(url):
        start_time = time.time()
        try:
            req = urllib.request.Request(
                url, 
                headers={"User-Agent": "StressTester/1.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                status_code = response.getcode()
                response.read() # Pobierz dane
                elapsed = time.time() - start_time
                return status_code, elapsed
        except urllib.error.HTTPError as e:
            return e.code, time.time() - start_time
        except Exception:
            return 500, time.time() - start_time

    # Rozdziel zapytania po endpointach
    target_urls = [endpoints[i % len(endpoints)] for i in range(requests_count)]

    start_suite = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        # Przekaż zadania do wykonania
        futures = {executor.submit(fetch_endpoint, url): url for url in target_urls}
        for future in concurrent.futures.as_completed(futures):
            status, duration = future.result()
            results.append({"status": status, "duration": duration})

    total_suite_time = time.time() - start_suite

    # Kalkulacja statystyk
    success_count = sum(1 for r in results if r["status"] in [200, 204])
    failed_count = requests_count - success_count
    durations = [r["duration"] for r in results]
    avg_duration = sum(durations) / len(durations) if durations else 0
    max_duration = max(durations) if durations else 0
    min_duration = min(durations) if durations else 0

    print("\n📊 WYNIKI TESTU OBCIĄŻENIOWEGO:")
    print(f"  - Czas trwania całego testu: {total_suite_time:.2f} s")
    print(f"  - Łączna liczba zapytań:    {requests_count}")
    print(f"  - Udane zapytania (200 OK): {success_count} ({success_count/requests_count*100:.1f}%)")
    print(f"  - Nieudane zapytania:       {failed_count} ({failed_count/requests_count*100:.1f}%)")
    print(f"  - Średni czas odpowiedzi:   {avg_duration*1000:.1f} ms")
    print(f"  - Najkrótsza odpowiedź:     {min_duration*1000:.1f} ms")
    print(f"  - Najdłuższa odpowiedź:      {max_duration*1000:.1f} ms")

    # Podział kodów statusów HTTP
    status_codes = {}
    for r in results:
        status_codes[r["status"]] = status_codes.get(r["status"], 0) + 1
    
    print("  - Kody odpowiedzi HTTP:")
    for code, count in status_codes.items():
        status_text = "OK" if code in [200, 204] else "Error/Redirect"
        print(f"    * HTTP {code} ({status_text}): {count} razy")

    if failed_count > 0:
        print("\n❌ WYNIK: API STRESS TEST WYKAZAŁ BŁĘDY!")
        return False
    else:
        print("\n🎉 WYNIK: API STRESS TEST ZAKOŃCZONY SUKCESEM (Brak błędów HTTP)!")
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke i Stress Testy dla EcoDashboard.")
    parser.add_argument("--url", default="http://localhost:8000", help="Bazowy adres URL aplikacji (default: http://localhost:8000)")
    parser.add_argument("--mode", default="all", choices=["ui", "api", "all"], help="Tryb testów: ui (tylko Playwright), api (tylko Stress test), all (oba)")
    parser.add_argument("--reqs", type=int, default=50, help="Liczba zapytań w teście obciążeniowym API (default: 50)")
    parser.add_argument("--concurrency", type=int, default=5, help="Współbieżność/liczba wątków w teście API (default: 5)")
    parser.add_argument("--email", default=None, help="Email logowania do Stravit w teście UI")
    parser.add_argument("--password", default=None, help="Hasło logowania do Stravit w teście UI")

    args = parser.parse_args()

    # Usuń ukośnik na końcu adresu URL
    base_url = args.url.rstrip("/")

    ui_success = True
    api_success = True

    if args.mode in ["ui", "all"]:
        ui_success = run_ui_smoke_test(base_url, args.email, args.password)

    if args.mode in ["api", "all"]:
        api_success = run_api_stress_test(base_url, requests_count=args.reqs, concurrency=args.concurrency)

    # Zwróć kod błędu jeśli któryś test zawiódł
    if not ui_success or not api_success:
        sys.exit(1)
    else:
        sys.exit(0)
