#!/usr/bin/env python3
"""Point d'entrée du scraper Coupe du Monde 2026 (fbref → SQLite).

Exemples :
    python run.py --full      # collecte initiale complète
    python run.py --update    # mise à jour live (classements, scores, stats clés)
    python run.py --full --no-headless   # debug avec fenêtre Chrome visible
"""
import argparse
import datetime as dt
import logging
import sys

import config
from scraper.pipeline import Pipeline


def setup_logging():
    logfile = config.LOG_DIR / "scrape.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile, encoding="utf-8"),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Scraper Coupe du Monde 2026 (fbref)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--full", action="store_true",
                       help="collecte complète fbref + native (toutes les stats)")
    group.add_argument("--update", action="store_true",
                       help="fbref (sous-ensemble) + native-stats live")
    group.add_argument("--live", action="store_true",
                       help="native-stats SEUL (scores + classement) — sûr pour le cloud/CI")
    group.add_argument("--lineups", action="store_true",
                       help="compositions/tactiques fbref seulement (depuis le Mac)")
    parser.add_argument("--headless", action="store_true",
                        help="force le mode invisible (moins fiable vs Cloudflare)")
    args = parser.parse_args()

    if args.headless:
        config.HEADLESS = True

    setup_logging()
    log = logging.getLogger("run")

    mode = ("full" if args.full else "live" if args.live
            else "lineups" if args.lineups else "update")
    run_ts = dt.datetime.now().isoformat(timespec="seconds")
    log.info("Démarrage scraper — mode=%s, ts=%s", mode, run_ts)

    try:
        with Pipeline(mode=mode) as pipe:
            pages = pipe.run(run_ts)
    except Exception:
        log.exception("Échec du run.")
        return 1

    log.info("Terminé. Base : %s", config.DB_PATH)
    print(f"\n✅ {pages} pages récupérées → {config.DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
