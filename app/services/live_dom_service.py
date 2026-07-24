"""
live_dom_service.py
--------------------
QUAND l'utilisateur fournit une URL réelle (mode "capture d'écran + URL"),
on NE fait PLUS confiance a un modele de vision pour deviner les elements.

Pourquoi : un petit modele multimodal local (llava:7b, etc.) a ete
enorme entraine sur des screenshots de formulaires de login. Resultat :
il "reconnait" un login meme sur une page qui n'en a aucun (ex: Google
Traduction) -> il HALLUCINE email/password/submit qui n'existent pas.

La solution fiable : puisqu'on a une URL REELLE et executable, on ouvre
la vraie page dans un navigateur headless (Selenium), on laisse le JS
s'executer (essentiel pour les SPA React/Vue/Angular comme Google
Traduction), on recupere le DOM final, et on reutilise
html_parser_service.analyze_html_code() qui est deja fiable et teste
sur ce projet (base sur le vrai HTML, pas sur des pixels devines).

Le screenshot uploade par l'utilisateur ne sert plus qu'a l'affichage /
au contexte visuel — jamais a la detection d'elements.
"""
import asyncio
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from app.config import TEXT_TIMEOUT
from app.services.driver_service import get_driver_path


class LiveFetchError(Exception):
    """Levee quand l'URL n'est pas accessible ou ne charge pas correctement."""
    pass


def _fetch_sync(url: str, wait_seconds: int) -> tuple[str, bytes]:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,900")
    options.add_argument("--disable-gpu")

    driver = None
    try:
        driver = webdriver.Chrome(
            service=Service(get_driver_path()),
            options=options,
        )
        driver.set_page_load_timeout(30)
        driver.get(url)

        # Attend que le document soit chargé...
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        # ...puis laisse un temps supplémentaire pour les SPA qui rendent
        # leur contenu après coup (fetch API, hydration React/Vue, etc.)
        time.sleep(wait_seconds)

        html = driver.page_source
        screenshot = driver.get_screenshot_as_png()

        if not html or len(html) < 200:
            raise LiveFetchError("La page chargée est vide ou trop courte.")

        return html, screenshot

    except LiveFetchError:
        raise
    except Exception as e:
        raise LiveFetchError(f"Impossible de charger '{url}' : {type(e).__name__}: {e}")
    finally:
        if driver:
            driver.quit()


async def fetch_rendered_page(url: str, wait_seconds: int = 4) -> tuple[str, bytes]:
    """
    Ouvre `url` dans Chrome headless, attend le rendu JS, et retourne :
      - le HTML final (post-JS) exploitable par html_parser_service
      - un screenshot PNG (bytes) de la page telle qu'elle apparaît réellement
    Lève LiveFetchError si l'URL n'est pas joignable / ne charge pas.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_sync, url, wait_seconds)