"""Couche de persistance SQLite : init du schéma + upserts idempotents."""
import json
import logging
import sqlite3
from typing import Iterable, Mapping

import config

log = logging.getLogger(__name__)


class Store:
    def __init__(self, db_path=config.DB_PATH):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.row_factory = sqlite3.Row

    def init_schema(self):
        sql = config.SCHEMA_PATH.read_text(encoding="utf-8")
        self.conn.executescript(sql)
        self.conn.commit()
        log.info("Schéma SQLite initialisé.")

    def close(self):
        self.conn.close()

    # --- Helper UPSERT générique --------------------------------------------
    def _upsert(self, table: str, rows: Iterable[Mapping], pk: Iterable[str]):
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        col_list = ", ".join(cols)
        pk_list = ", ".join(pk)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
        conflict = (
            f"ON CONFLICT({pk_list}) DO UPDATE SET {updates}"
            if updates else f"ON CONFLICT({pk_list}) DO NOTHING"
        )
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict}"
        self.conn.executemany(sql, rows)
        self.conn.commit()
        return len(rows)

    # --- Méthodes métier -----------------------------------------------------
    def upsert_groups(self, names):
        rows = [{"group_name": n} for n in names]
        return self._upsert("groups", rows, ["group_name"])

    def upsert_teams(self, teams):
        return self._upsert("teams", teams, ["team_id"])

    def upsert_standings(self, rows):
        return self._upsert("standings", rows, ["group_name", "team_name"])

    def upsert_matches(self, rows):
        return self._upsert("matches", rows, ["match_id"])

    def upsert_players(self, rows):
        return self._upsert("players", rows, ["player_id"])

    def upsert_player_stats(self, player_id, category, team_name, stats: dict):
        row = {
            "player_id": player_id,
            "category": category,
            "team_name": team_name,
            "stats_json": json.dumps(stats, ensure_ascii=False),
        }
        return self._upsert("player_stats", [row], ["player_id", "category"])

    def dedupe_matches(self):
        """Supprime les doublons de fixture (même date + mêmes équipes),
        en gardant la ligne qui a un score. Auto-réparation défensive."""
        self.conn.execute(
            """DELETE FROM matches WHERE rowid NOT IN (
                 SELECT rowid FROM (
                   SELECT rowid, ROW_NUMBER() OVER (
                     PARTITION BY match_date, home_team, away_team
                     ORDER BY (home_score IS NOT NULL) DESC, match_id
                   ) rn FROM matches
                 ) WHERE rn = 1
               )""")
        self.conn.commit()

    def lineup_keys(self):
        """Clés (date, home, away) déjà scrapées — pour un scrape incrémental."""
        return {tuple(r) for r in self.conn.execute(
            "SELECT DISTINCT match_date, home_team, away_team FROM lineups")}

    def upsert_lineup(self, date, home, away, team, formation, players):
        row = {
            "match_date": date, "home_team": home, "away_team": away,
            "team_name": team, "formation": formation,
            "players_json": json.dumps(players, ensure_ascii=False),
        }
        return self._upsert("lineups", [row],
                            ["match_date", "home_team", "away_team", "team_name"])

    def upsert_team_stats(self, team_name, category, team_id, stats: dict):
        row = {
            "team_name": team_name, "category": category, "team_id": team_id,
            "stats_json": json.dumps(stats, ensure_ascii=False),
        }
        return self._upsert("team_stats", [row], ["team_name", "category"])

    def rebuild_venues_and_referees(self):
        """Recalcule les agrégats stades/arbitres depuis la table matches."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM venues;")
        cur.execute("DELETE FROM referees;")
        cur.execute(
            """INSERT INTO venues (venue_name, matches_count, total_attendance)
               SELECT venue, COUNT(*), COALESCE(SUM(attendance), 0)
               FROM matches WHERE venue IS NOT NULL AND venue != ''
               GROUP BY venue;"""
        )
        cur.execute(
            """INSERT INTO referees (referee_name, matches_count)
               SELECT referee, COUNT(*)
               FROM matches WHERE referee IS NOT NULL AND referee != ''
               GROUP BY referee;"""
        )
        self.conn.commit()

    def log_run(self, run_ts, mode, pages, notes=""):
        self._upsert(
            "scrape_runs",
            [{"run_ts": run_ts, "mode": mode, "pages": pages, "notes": notes}],
            ["run_ts"],
        )
