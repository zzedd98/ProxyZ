"""
🔌 Script de reset des clés 4G via Playwright
=============================================

Reset la connexion 4G en accédant à l'interface web du modem
via un proxy local sur le port spécifié.

Version synchrone : pas d'asyncio, donc pas de boucle d'événements ni socketpair
par processus → évite la saturation des sockets (WinError 10055) avec 6 resets en parallèle.
"""

import logging
import sys
import re
import time
import httpx
from playwright.sync_api import sync_playwright

# Configuration du logging - niveau ERROR pour réduire les logs
logging.basicConfig(
    level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# URL de l'interface web des modems Huawei
MODEM_WEB_URL = "http://192.168.8.1/#/mobileconnection"


def _get_modem_ip(proxy_port: int) -> str | None:
    """Récupère l'IP publique du modem via son proxy (synchrone)."""
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    services = ["https://api.ipify.org", "https://ifconfig.me", "https://icanhazip.com"]

    for service in services:
        try:
            with httpx.Client(proxy=proxy_url, timeout=10.0) as client:
                response = client.get(service)
                if response.status_code == 200:
                    ip = response.text.strip()
                    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        return ip
        except Exception:
            continue
    return None


def reset_modem_by_port(proxy_port: int) -> bool:
    """
    Reset un modem via son proxy local en utilisant Playwright (synchrone).

    Args:
        proxy_port: Port du proxy local (101, 102, etc.)

    Returns:
        True si le reset Playwright a réussi **et** que l'IP publique a changé,
        False sinon
    """
    playwright = None
    browser = None
    context = None

    try:
        print(f"[RESET] 🔄 Démarrage du reset Playwright pour le port {proxy_port}...")

        # 1) Récupérer l'IP publique AVANT le reset
        old_ip = _get_modem_ip(proxy_port)
        if old_ip:
            print(f"[RESET] 📡 IP actuelle avant reset (port {proxy_port}): {old_ip}")
        else:
            print(
                f"[RESET] ⚠️ Impossible de récupérer l'IP actuelle avant reset (port {proxy_port})"
            )

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        proxy_config = {"server": f"http://127.0.0.1:{proxy_port}"}
        context = browser.new_context(proxy=proxy_config)
        page = context.new_page()

        print(
            f"[RESET] 🌐 Accès à l'interface web du modem via proxy {proxy_port}..."
        )
        # Le modem peut renvoyer ERR_EMPTY_RESPONSE ou ne pas émettre "load" : on utilise domcontentloaded et des retries
        goto_ok = False
        for attempt in range(1, 4):
            try:
                page.goto(
                    MODEM_WEB_URL,
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                goto_ok = True
                break
            except Exception as goto_err:
                if attempt < 3 and ("ERR_EMPTY_RESPONSE" in str(goto_err) or "Timeout" in str(goto_err)):
                    print(f"[RESET] ⏳ Tentative {attempt}/3 échouée, nouvel essai dans 3s...")
                    page.wait_for_timeout(3000)
                else:
                    raise
        page.wait_for_timeout(2000)

        print(f"[RESET] 📋 Sélection du profil...")
        page.locator("tr.table-data:nth-child(3)").click()
        page.wait_for_timeout(1000)

        print(f"[RESET] ☑️ Activation du profil par défaut...")
        page.evaluate(
            """() => {
            document.querySelector('#defaultProfile input[type="checkbox"]').click();
        }"""
        )

        page.wait_for_timeout(500)

        print(f"[RESET] 💾 Enregistrement...")
        page.get_by_role("button", name="Save").click()
        page.wait_for_timeout(2000)

        print(
            f"[RESET] ✅ Séquence Playwright terminée, vérification du changement d'IP (port {proxy_port})..."
        )

        # 2) Fermeture explicite pour libérer ressources tout de suite
        page.close()
        context.close()
        context = None
        browser.close()
        browser = None
        playwright.stop()
        playwright = None

        # 3) Vérifier que l'IP a bien changé après le reset
        max_attempts = 12
        for attempt in range(1, max_attempts + 1):
            time.sleep(5)
            new_ip = _get_modem_ip(proxy_port)

            if not new_ip:
                print(
                    f"[RESET] 🔎 Tentative {attempt}/{max_attempts}: impossible de lire l'IP (port {proxy_port})"
                )
                continue

            if old_ip is None:
                print(
                    f"[RESET] ✅ IP obtenue après reset (port {proxy_port}): {new_ip}"
                )
                return True

            if new_ip != old_ip:
                print(
                    f"[RESET] ✅ IP changée pour le port {proxy_port}: {old_ip} → {new_ip}"
                )
                return True

            print(
                f"[RESET] 🔁 Tentative {attempt}/{max_attempts}: l'IP est toujours la même ({new_ip})"
            )

        print(
            f"[RESET] ❌ L'IP n'a pas changé après le reset (port {proxy_port}). "
            f"Ancienne IP: {old_ip or 'inconnue'}"
        )
        return False

    except Exception as e:
        print(f"[RESET] ❌ Erreur reset port {proxy_port}: {str(e)}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        # Libération garantie pour ne pas garder Chromium/sockets ouverts en cas d'exception
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python reset_modem.py <proxy_port>")
        print("Exemple: python reset_modem.py 101")
        sys.exit(1)

    port = int(sys.argv[1])
    success = reset_modem_by_port(port)
    sys.exit(0 if success else 1)
