"""Récupération des pages fbref via SeleniumBase UC : franchissement du défi
Cloudflare (clic Turnstile auto + fallback manuel), throttling, et extraction
du HTML (y compris les tableaux planqués dans des commentaires HTML)."""
import logging
import random
import time

from bs4 import BeautifulSoup, Comment

import config

log = logging.getLogger(__name__)


class Fetcher:
    """Gère un driver SeleniumBase UC + rate limit strict ≤ 10 req/min."""

    _CHALLENGE_MARKERS = (
        "just a moment", "un instant", "enable javascript and cookies",
        "verifying you are human", "checking your browser",
        "cf-browser-verification", "challenge-platform",
        "needs to review the security",
    )
    _BAN_MARKERS = (
        "rate limited request", "error 429", "too many requests", "access denied",
    )

    def __init__(self, driver):
        self.driver = driver
        self._last_request_ts = 0.0

    # --- Throttling ----------------------------------------------------------
    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_ts
        wait = config.RATE_LIMIT_SECONDS - elapsed + random.uniform(0, config.RATE_LIMIT_JITTER)
        if wait > 0:
            log.debug("Throttle : attente %.1fs", wait)
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    # --- Détection de blocage ------------------------------------------------
    @classmethod
    def _looks_blocked(cls, html: str) -> bool:
        if not html or len(html) < 800:
            return True
        low = html.lower()
        return any(m in low for m in cls._CHALLENGE_MARKERS + cls._BAN_MARKERS)

    def _is_banned(self, html: str) -> bool:
        low = (html or "").lower()
        return any(m in low for m in self._BAN_MARKERS)

    def _safe_page_source(self) -> str:
        try:
            return self.driver.get_page_source()
        except Exception as exc:  # état transitoire (rechargement du défi)
            log.debug("page_source indisponible (transitoire) : %s", exc)
            return ""

    # --- Franchissement du défi ----------------------------------------------
    def _try_click_captcha(self):
        """Tente le clic auto sur la case Turnstile (SeleniumBase / pyautogui).

        Nécessite une fenêtre visible et, sous macOS, l'autorisation
        « Accessibilité » pour le terminal. Jamais fatal en cas d'échec."""
        for method in ("uc_gui_click_captcha", "uc_gui_handle_captcha"):
            fn = getattr(self.driver, method, None)
            if fn is None:
                continue
            try:
                fn()
                log.info("Clic Turnstile tenté via %s().", method)
                return
            except Exception as exc:
                log.debug("%s() a échoué : %s", method, exc)

    def _poll_until_clear(self, deadline) -> str:
        html = self._safe_page_source()
        while self._looks_blocked(html) and time.monotonic() < deadline:
            if self._is_banned(html):
                return html  # ban de débit : inutile d'attendre
            time.sleep(config.CLOUDFLARE_POLL)
            html = self._safe_page_source()
        return html

    def _wait_for_challenge(self, url: str) -> str:
        # Phase 1 : clic auto + attente que le défi se lève.
        self._try_click_captcha()
        html = self._poll_until_clear(time.monotonic() + config.CLOUDFLARE_MAX_WAIT)
        if not self._looks_blocked(html) or self._is_banned(html):
            return html

        # Phase 2 : fenêtre visible -> on laisse l'humain cocher la case.
        if config.MANUAL_SOLVE and not config.HEADLESS:
            log.warning(
                "\n>>> Défi Cloudflare non franchi automatiquement.\n"
                ">>> Coche la case « Vérifiez que vous êtes humain » dans la "
                "fenêtre Chrome.\n>>> (le cookie sera mémorisé) — j'attends %ds...",
                config.MANUAL_SOLVE_WAIT)
            html = self._poll_until_clear(time.monotonic() + config.MANUAL_SOLVE_WAIT)
        return html

    # --- Requête -------------------------------------------------------------
    def get_html(self, url: str) -> str:
        for attempt in range(1, config.MAX_RETRIES + 1):
            self._throttle()
            log.info("GET %s (tentative %d)", url, attempt)
            try:
                # uc_open_with_reconnect déconnecte brièvement le webdriver pour
                # que Cloudflare ne détecte pas l'automatisation au chargement.
                self.driver.uc_open_with_reconnect(url, config.UC_RECONNECT)
            except Exception as exc:
                log.warning("uc_open a échoué (%s), fallback driver.get", exc)
                try:
                    self.driver.get(url)
                except Exception as exc2:
                    log.warning("driver.get a aussi échoué : %s", exc2)

            html = self._wait_for_challenge(url)
            if not self._looks_blocked(html):
                return html

            backoff = config.RETRY_BACKOFF_SECONDS * attempt
            log.warning("Page bloquée/incomplète (%s). Backoff %.0fs.", url, backoff)
            time.sleep(backoff)

        raise RuntimeError(f"Échec récupération après {config.MAX_RETRIES} tentatives : {url}")

    def get_soup(self, url: str) -> BeautifulSoup:
        """Soup avec les tables planquées dans des commentaires HTML ré-injectées."""
        soup = BeautifulSoup(self.get_html(url), "lxml")
        for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
            if "<table" in c:
                c.replace_with(BeautifulSoup(c, "lxml"))
        return soup
