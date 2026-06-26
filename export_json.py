#!/usr/bin/env python3
"""Exporte la base SQLite en un seul webapp/data.json consommé par la PWA.

Lancé en fin de pipeline (ou à la main : python export_json.py). Tout est
pré-agrégé pour que la PWA n'ait qu'à afficher (zéro calcul côté client)."""
import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

import config

log = logging.getLogger(__name__)

OUT_DIR = config.BASE_DIR / "webapp"
OUT_FILE = OUT_DIR / "data.json"


def _rows(cur, sql, *a):
    return [dict(r) for r in cur.execute(sql, a).fetchall()]


def _table_exists(cur, name):
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _analytics(cur):
    """Agrégats avancés (niveau data analyst) calculés côté serveur."""
    played = cur.execute(
        """SELECT home_team AS h, away_team AS a, home_score AS hs, away_score AS as_
           FROM matches WHERE home_score IS NOT NULL AND away_score IS NOT NULL"""
    ).fetchall()
    n = len(played)

    # Distribution des scores, marges, buts/jour, clean sheets, BTTS.
    from collections import Counter, defaultdict
    scorelines = Counter()
    margins = Counter()
    goals_by_day = defaultdict(int)
    clean = Counter()
    scored = Counter()
    btts = decisive = 0
    biggest = top_match = None

    day_rows = cur.execute(
        """SELECT match_date d, home_team h, away_team a, home_score hs, away_score as_
           FROM matches WHERE home_score IS NOT NULL ORDER BY match_date"""
    ).fetchall()
    for r in day_rows:
        hs, as_ = r["hs"], r["as_"]
        goals_by_day[r["d"]] += hs + as_

    for r in played:
        hs, as_, h, a = r["hs"], r["as_"], r["h"], r["a"]
        hi, lo = max(hs, as_), min(hs, as_)
        scorelines[f"{hi}-{lo}"] += 1
        diff = hi - lo
        margins[("Nul" if diff == 0 else "1 but" if diff == 1
                 else "2 buts" if diff == 2 else "3+ buts")] += 1
        if hs and as_:
            btts += 1
        if hs != as_:
            decisive += 1
        # clean sheets & a marqué
        if as_ == 0: clean[h] += 1
        if hs == 0: clean[a] += 1
        if hs > 0: scored[h] += 1
        if as_ > 0: scored[a] += 1
        if biggest is None or diff > biggest["diff"]:
            biggest = {"home": h, "away": a, "hs": hs, "as": as_, "diff": diff}
        if top_match is None or (hs + as_) > top_match["tot"]:
            top_match = {"home": h, "away": a, "hs": hs, "as": as_, "tot": hs + as_}

    margin_order = ["Nul", "1 but", "2 buts", "3+ buts"]

    # Cartons (depuis stats_standard si dispo).
    cards_y = cards_r = 0
    discipline = []
    if _table_exists(cur, "stats_standard"):
        cy = cur.execute("SELECT COALESCE(SUM(cards_yellow),0), COALESCE(SUM(cards_red),0) "
                         "FROM stats_standard").fetchone()
        cards_y, cards_r = cy[0], cy[1]
        discipline = _rows(cur, """SELECT name, team_name team, cards_yellow y, cards_red r
                                   FROM stats_standard
                                   WHERE COALESCE(cards_yellow,0)+COALESCE(cards_red,0)>0
                                   ORDER BY (COALESCE(cards_red,0)*2+COALESCE(cards_yellow,0)) DESC,
                                            cards_red DESC LIMIT 12""")
        involvements = _rows(cur, """SELECT name, team_name team, goals g, assists a,
                                            COALESCE(goals,0)+COALESCE(assists,0) ga
                                     FROM stats_standard
                                     WHERE COALESCE(goals,0)+COALESCE(assists,0)>0
                                     ORDER BY ga DESC, g DESC LIMIT 12""")
    else:
        involvements = []

    # Efficacité : buts / tir (min 5 tirs).
    conversion = []
    if _table_exists(cur, "stats_shooting"):
        for r in _rows(cur, """SELECT name, team_name team, goals g, shots s,
                                      shots_on_target sot FROM stats_shooting
                               WHERE COALESCE(shots,0)>=5"""):
            conversion.append({"name": r["name"], "team": r["team"], "goals": r["g"],
                               "shots": r["s"], "conv": round(100*(r["g"] or 0)/r["s"])})
        conversion.sort(key=lambda x: (-x["conv"], -x["goals"]))
        conversion = conversion[:12]

    # Points par match (équipes).
    ppg = []
    for r in _rows(cur, "SELECT team_name team, group_name grp, pts, mp FROM standings"):
        if r["mp"]:
            ppg.append({"team": r["team"], "grp": r["grp"],
                        "ppg": round(r["pts"]/r["mp"], 2), "pts": r["pts"], "mp": r["mp"]})
    ppg.sort(key=lambda x: -x["ppg"])

    clean_total = sum(clean.values())

    # Cartons par équipe (pour le power ranking).
    team_cards = {}
    if _table_exists(cur, "stats_standard"):
        for r in cur.execute("""SELECT team_name t, COALESCE(SUM(cards_yellow),0) y,
                                       COALESCE(SUM(cards_red),0) r
                                FROM stats_standard GROUP BY team_name"""):
            team_cards[r["t"]] = (r["y"], r["r"])

    st = _rows(cur, """SELECT team_name t, group_name g, rank, mp, w, d, l,
                              gf, ga, gd, pts FROM standings""")

    # --- Meilleurs 3es (8 qualifiés en 2026) : tri Pts, diff, BM ----------
    thirds = sorted([r for r in st if r["rank"] == 3],
                    key=lambda r: (r["pts"], r["gd"], r["gf"]), reverse=True)
    thirds_tbl = [{
        "pos": i + 1, "team": r["t"], "grp": r["g"], "mp": r["mp"],
        "w": r["w"], "d": r["d"], "l": r["l"], "gd": r["gd"], "gf": r["gf"],
        "pts": r["pts"], "qualified": i < 8,
    } for i, r in enumerate(thirds)]

    # --- Classement général : toutes les équipes, tri Pts > Diff > BM -----
    # Note /20 = performance PAR MATCH (juste entre équipes à 2 ou 3 matchs),
    # combinée (résultats + attaque + défense − cartons) puis mise à l'échelle.
    power = []
    for r in st:
        y, rc = team_cards.get(r["t"], (0, 0))
        cs = clean.get(r["t"], 0)
        mp = r["mp"] or 1
        raw_pm = (4*r["pts"] + 3*r["gd"] + 2*r["gf"] + 3*cs - 2*rc - 0.5*y) / mp
        power.append({
            "team": r["t"], "grp": r["g"], "mp": r["mp"], "pts": r["pts"],
            "gf": r["gf"], "gd": r["gd"], "cs": cs, "yellow": y, "red": rc,
            "_raw": raw_pm,
        })
    raws = [p["_raw"] for p in power] or [0]
    lo, hi = min(raws), max(raws)
    span = (hi - lo) or 1
    for p in power:
        p["note"] = round(4 + 16 * (p["_raw"] - lo) / span, 1)  # échelle 4,0 → 20,0
        del p["_raw"]
    power.sort(key=lambda x: (x["pts"], x["gd"], x["gf"]), reverse=True)
    for i, p in enumerate(power):
        p["pos"] = i + 1

    return {
        "overview": {
            "clean_sheets": clean_total,
            "btts_pct": round(100*btts/n) if n else 0,
            "decisive_pct": round(100*decisive/n) if n else 0,
            "draws": n - decisive,
            "cards_yellow": cards_y, "cards_red": cards_r,
            "biggest_win": biggest, "top_match": top_match,
        },
        "goals_by_day": [{"date": d, "goals": g} for d, g in sorted(goals_by_day.items())],
        "scorelines": [{"score": s, "n": c} for s, c in scorelines.most_common(8)],
        "margins": [{"label": m, "n": margins.get(m, 0)} for m in margin_order],
        "team_ppg": ppg[:12],
        "team_clean_sheets": [{"team": t, "cs": c}
                              for t, c in clean.most_common(12) if c > 0],
        "involvements": involvements,
        "conversion": conversion,
        "discipline": discipline,
        "thirds": thirds_tbl,
        "power": power,
    }


def build(db_path=config.DB_PATH, generated_at=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- Groupes + classements ---------------------------------------------
    groups = {}
    for r in _rows(cur, """SELECT group_name, rank, team_name, mp, w, d, l,
                                  gf, ga, gd, pts FROM standings
                           ORDER BY group_name, rank"""):
        groups.setdefault(r["group_name"], []).append({
            "rank": r["rank"], "name": r["team_name"], "mp": r["mp"],
            "w": r["w"], "d": r["d"], "l": r["l"],
            "gf": r["gf"], "ga": r["ga"], "gd": r["gd"], "pts": r["pts"],
        })
    groups_list = [{"group": g, "teams": t} for g, t in sorted(groups.items())]

    # --- Matchs -------------------------------------------------------------
    recent = _rows(cur, """SELECT match_date AS date, match_time AS time,
                                  home_team AS home, home_score AS hs,
                                  away_score AS as_, away_team AS away, venue
                           FROM matches WHERE home_score IS NOT NULL
                           ORDER BY match_date DESC, match_time DESC LIMIT 40""")
    for m in recent:
        m["as"] = m.pop("as_")
    upcoming = _rows(cur, """SELECT match_date AS date, match_time AS time,
                                    home_team AS home, away_team AS away, venue
                             FROM matches WHERE home_score IS NULL
                             ORDER BY match_date, match_time LIMIT 24""")

    # --- Buteurs / passeurs / tirs (depuis les tables éclatées) ------------
    scorers, assisters, shots_leaders = [], [], []
    if _table_exists(cur, "stats_standard"):
        scorers = _rows(cur, """SELECT name, team_name AS team, goals, assists, minutes
                                FROM stats_standard WHERE goals IS NOT NULL AND goals>0
                                ORDER BY goals DESC, assists DESC LIMIT 20""")
        assisters = _rows(cur, """SELECT name, team_name AS team, assists, goals
                                  FROM stats_standard WHERE assists IS NOT NULL AND assists>0
                                  ORDER BY assists DESC, goals DESC LIMIT 15""")
    if _table_exists(cur, "stats_shooting"):
        shots_leaders = _rows(cur, """SELECT name, team_name AS team, shots,
                                             shots_on_target AS sot
                                      FROM stats_shooting WHERE shots IS NOT NULL AND shots>0
                                      ORDER BY shots DESC LIMIT 15""")

    # --- Leaderboards équipes ----------------------------------------------
    team_rows = _rows(cur, """SELECT team_name AS team, group_name AS grp,
                                     gf, ga, gd, pts, mp FROM standings""")
    attack = sorted(team_rows, key=lambda t: (-(t["gf"] or 0), t["ga"] or 0))[:12]
    defense = sorted(team_rows, key=lambda t: ((t["ga"] or 0), -(t["gf"] or 0)))[:12]

    # --- Résumé tournoi -----------------------------------------------------
    played = cur.execute("SELECT COUNT(*) FROM matches WHERE home_score IS NOT NULL").fetchone()[0]
    total = cur.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    goals = cur.execute("""SELECT COALESCE(SUM(home_score+away_score),0)
                           FROM matches WHERE home_score IS NOT NULL""").fetchone()[0]
    summary = {
        "teams": cur.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
        "groups": len(groups_list),
        "matches_played": played,
        "matches_total": total,
        "total_goals": goals,
        "avg_goals": round(goals / played, 2) if played else 0,
    }

    analytics = _analytics(cur)

    # Événements live (buteurs + minutes + matchs en direct) via API externe.
    try:
        from scraper import wc26_events
        events = wc26_events.build()
    except Exception:
        log.exception("Événements live indisponibles ce run.")
        events = {"live": [], "results": [], "upcoming": []}

    # « À venir » vient de l'API (statut notstarted) : un match commencé en sort
    # automatiquement (il passe en direct). Repli sur la base si l'API est down.
    if events.get("upcoming"):
        upcoming = events["upcoming"]

    data = {
        "generated_at": (generated_at or dt.datetime.now()).isoformat(timespec="seconds"),
        "summary": summary,
        "groups": groups_list,
        "matches_recent": recent,
        "matches_upcoming": upcoming,
        "scorers": scorers,
        "assisters": assisters,
        "shots_leaders": shots_leaders,
        "team_attack": attack,
        "team_defense": defense,
        "team_scatter": team_rows,
        "analytics": analytics,
        "events": events,
    }
    conn.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Export → %s (%d groupes, %d matchs récents, %d buteurs)",
             OUT_FILE, len(groups_list), len(recent), len(scorers))
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    d = build()
    print(f"\n✅ {OUT_FILE}")
    print(f"   {d['summary']['matches_played']}/{d['summary']['matches_total']} matchs, "
          f"{d['summary']['total_goals']} buts, {len(d['scorers'])} buteurs")
