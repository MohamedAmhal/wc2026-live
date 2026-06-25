"""Factory du driver SeleniumBase en mode UC (anti-Cloudflare).

SeleniumBase pilote Chrome de façon beaucoup plus furtive qu'undetected-
chromedriver et expose `uc_gui_click_captcha()` pour cliquer automatiquement la
case Turnstile. Il gère aussi tout seul la version du chromedriver (fini le
mismatch 149/150)."""
import logging

from seleniumbase import Driver

import config

log = logging.getLogger(__name__)


def build_live_driver():
    """Driver Selenium STANDARD (sans furtivité) pour native-stats.

    native-stats n'a pas de Cloudflare → pas besoin d'undetected/SeleniumBase.
    Ce driver léger tourne aussi très bien sur GitHub Actions (mode --live)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    if config.HEADLESS:
        opts.add_argument("--headless=new")
    for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--window-size=1400,1200", "--lang=en-US"):
        opts.add_argument(arg)
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    log.info("Driver Selenium standard démarré (mode live, headless=%s)", config.HEADLESS)
    return driver


def build_driver():
    """Crée un driver SeleniumBase UC, avec profil Chrome persistant.

    Le profil persistant conserve le cookie cf_clearance posé après le défi
    Cloudflare : les runs suivants passent alors sans interaction.
    """
    driver = Driver(
        uc=True,
        headless=config.HEADLESS,          # Cloudflare + clic GUI exigent une fenêtre visible
        user_data_dir=str(config.CHROME_PROFILE_DIR),
        locale_code="en",
        page_load_strategy="eager",
    )
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    log.info("Driver SeleniumBase UC démarré (headless=%s)", config.HEADLESS)
    return driver
