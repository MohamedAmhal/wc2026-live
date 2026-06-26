"""Orchestration du scraping : enchaîne les pages dans l'ordre des dépendances
et persiste tout dans SQLite via le Store."""
import logging
import time

import config
from db.store import Store
from scraper import native_stats, parsers
from scraper.driver import build_driver, build_live_driver
from scraper.fetcher import Fetcher

log = logging.getLogger(__name__)


class Pipeline:
    """mode 'full'   : collecte complète (groupes, matchs, toutes les stats).
       mode 'update' : re-scrape ce qui bouge en live (classements, scores,
                       stats standard) — plus rapide, idéal pour un cron."""

    def __init__(self, mode="full"):
        self.mode = mode
        self.store = Store()
        self.driver = None
        self.fetcher = None
        self.pages = 0

    # --- Cycle de vie --------------------------------------------------------
    def __enter__(self):
        self.store.init_schema()
        if self.mode == "live":
            # native-stats seul : driver léger (sans furtivité), idéal cloud/CI.
            self.driver = build_live_driver()
        else:
            self.driver = build_driver()
            self.fetcher = Fetcher(self.driver)
        return self

    def __exit__(self, *exc):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.store.close()

    # --- Étapes --------------------------------------------------------------
    def scrape_groups(self):
        log.info("== Groupes & classements ==")
        soup = self.fetcher.get_soup(config.HUB_URL)
        self.pages += 1
        groups, teams, standings = parsers.parse_groups_and_standings(soup)
        self.store.upsert_groups(groups)
        self.store.upsert_teams(teams)
        self.store.upsert_standings(standings)
        return teams

    def scrape_schedule(self):
        log.info("== Calendrier & matchs ==")
        soup = self.fetcher.get_soup(config.SCHEDULE_URL)
        self.pages += 1
        matches = parsers.parse_schedule(soup)
        self.store.upsert_matches(matches)
        self.store.dedupe_matches()
        self.store.rebuild_venues_and_referees()
        return matches

    def scrape_player_stats(self, categories):
        log.info("== Stats joueurs (%d catégories) ==", len(categories))
        for cat in categories:
            segment = config.PLAYER_STAT_CATEGORIES[cat]
            url = config.player_stats_url(segment)
            try:
                soup = self.fetcher.get_soup(url)
                self.pages += 1
            except RuntimeError as exc:
                log.error("Catégorie '%s' ignorée : %s", cat, exc)
                continue
            players, stats = parsers.parse_player_stats(soup, cat)
            self.store.upsert_players(list(players.values()))
            for player_id, team_name, s in stats:
                self.store.upsert_player_stats(player_id, cat, team_name, s)
            # Stats au niveau équipe (possession, tirs, totaux...).
            for ts in parsers.parse_team_stats(soup, cat):
                self.store.upsert_team_stats(ts["team_name"], cat, ts["team_id"], ts["stats"])

    def scrape_live_native(self):
        """Source live : corrige le retard de fbref (scores + classement)."""
        log.info("== Live native-stats (scores + classement) ==")
        try:
            updated, unmatched = native_stats.update_live(self.store, self.driver)
            self.pages += 1
            log.info("native-stats : %d matchs MAJ, %d non appariés", updated, unmatched)
        except Exception:
            log.exception("Étape native-stats échouée (données fbref conservées).")

    def _preprocess(self):
        try:
            import preprocess
            preprocess.preprocess(config.DB_PATH)
            log.info("Preprocessing terminé (tables stats_<categorie>).")
        except Exception:
            log.exception("Preprocessing échoué (JSON brut conservé).")

    def _export(self):
        try:
            import export_json
            export_json.build(config.DB_PATH)
            log.info("Export JSON terminé (webapp/data.json).")
        except Exception:
            log.exception("Export JSON échoué.")

    # --- Runs ----------------------------------------------------------------
    def run(self, run_ts):
        # Mode live (cloud/CI) : uniquement native-stats (scores + classement).
        if self.mode == "live":
            self.scrape_live_native()
            self._preprocess()
            self._export()
            self.store.log_run(run_ts, self.mode, self.pages, notes="native-only")
            log.info("Run 'live' terminé : %d pages.", self.pages)
            return self.pages

        if self.mode == "full":
            categories = list(config.PLAYER_STAT_CATEGORIES.keys())
        else:  # update : le sous-ensemble qui évolue match après match
            categories = ["standard", "shooting", "keepers"]

        self.scrape_groups()
        self.scrape_schedule()
        self.scrape_player_stats(categories)
        # native-stats en dernier : écrase scores/classement avec les données à jour.
        self.scrape_live_native()

        # Preprocessing : éclate stats_json en tables stats_<categorie> typées.
        self._preprocess()
        self._export()

        self.store.log_run(run_ts, self.mode, self.pages,
                           notes=f"categories={categories}")
        log.info("Run '%s' terminé : %d pages récupérées.", self.mode, self.pages)
        return self.pages
