#!/usr/bin/env python3
"""Exporte la base SQLite en un seul webapp/data.json consommé par la PWA.

Lancé en fin de pipeline (ou à la main : python export_json.py). Tout est
pré-agrégé pour que la PWA n'ait qu'à afficher (zéro calcul côté client)."""
import datetime as dt
import json
import logging
import re
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
    # Note /20 = niveau selon le classement officiel (points → diff → buts),
    # mise à l'échelle 4→20. Les points DOMINENT (×1000) pour que la note suive
    # exactement l'ordre du tableau : une équipe plus bas n'a jamais une meilleure note.
    power = []
    for r in st:
        y, rc = team_cards.get(r["t"], (0, 0))
        cs = clean.get(r["t"], 0)
        raw = r["pts"]*100 + r["gd"]*3 + r["gf"]*0.5
        power.append({
            "team": r["t"], "grp": r["g"], "mp": r["mp"], "pts": r["pts"],
            "gf": r["gf"], "gd": r["gd"], "cs": cs, "yellow": y, "red": rc,
            "_raw": raw,
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


def _int0(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _teams_detail(cur, events, note_map):
    """Compose un dashboard par équipe : indicateurs, forme, forces/faiblesses
    (percentile vs les 48), timing des buts, effectif, journal."""
    from scraper import native_stats as ns  # réutilise normalisation + alias

    teams = _rows(cur, """SELECT team_name t, group_name g, rank, mp, w, d, l,
                                 gf, ga, gd, pts FROM standings""")
    if not teams:
        return {}

    # Stats d'équipe fbref (possession, tirs...) préchargées.
    tstats = {}
    for r in cur.execute("SELECT team_name, category, stats_json FROM team_stats"):
        tstats.setdefault(r[0], {})[r[1]] = json.loads(r[2])

    def tget(team, cat, key):
        v = tstats.get(team, {}).get(cat, {}).get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    matches = _rows(cur, """SELECT match_date d, home_team h, away_team a,
                                   home_score hs, away_score as_ FROM matches
                            WHERE home_score IS NOT NULL ORDER BY match_date""")

    # Effectifs (standard + tirs).
    squad = {}
    for r in cur.execute("""SELECT players.team_name, players.name, players.position,
              json_extract(stats_json,'$.goals'), json_extract(stats_json,'$.assists'),
              json_extract(stats_json,'$.minutes') FROM players JOIN player_stats
              USING(player_id) WHERE category='standard'"""):
        squad.setdefault(r[0], []).append({
            "name": r[1], "pos": r[2], "goals": _int0(r[3]),
            "assists": _int0(r[4]), "minutes": _int0(r[5])})
    shots_by_player = {}
    for r in cur.execute("""SELECT name, json_extract(stats_json,'$.shots') s
              FROM players JOIN player_stats USING(player_id) WHERE category='shooting'"""):
        shots_by_player[r[0]] = _int0(r[1])

    # Compositions / tactiques par équipe (formation + XI), depuis lineups.
    lineups_by_team = {}
    for r in cur.execute("""SELECT match_date, home_team, away_team, team_name,
                                   formation, players_json FROM lineups"""):
        opp = r[2] if r[3] == r[1] else r[1]
        lineups_by_team.setdefault(r[3], []).append({
            "date": r[0], "opp": opp, "formation": r[4], "xi": json.loads(r[5])})

    # Résolution nom anglais (events) -> nom fbref.
    lookup = {ns._norm(t["t"]): t["t"] for t in teams}
    lookup.update(ns._ALIASES)
    resolve = lambda n: lookup.get(ns._norm(n))

    # Timing des buts (6 tranches) depuis les événements.
    BK = [(1, 15), (16, 30), (31, 45), (46, 60), (61, 75), (76, 200)]
    timing = {t["t"]: {"scored": [0]*6, "conceded": [0]*6} for t in teams}
    for m in (events.get("results") or []):
        H, A = resolve(m.get("home")), resolve(m.get("away"))
        for gl in m.get("goals") or []:
            scorer = H if gl["team"] == "home" else A
            concede = A if gl["team"] == "home" else H
            if not gl.get("minute"):
                continue
            try:
                mi = int(re.split(r"\+", str(gl["minute"]))[0])
            except ValueError:
                continue
            bi = next((i for i, (lo, hi) in enumerate(BK) if lo <= mi <= hi), 5)
            if scorer in timing:
                timing[scorer]["scored"][bi] += 1
            if concede in timing:
                timing[concede]["conceded"][bi] += 1

    # Clean sheets par équipe (depuis les matchs).
    cs = {t["t"]: 0 for t in teams}
    for m in matches:
        if m["as_"] == 0 and m["h"] in cs:
            cs[m["h"]] += 1
        if m["hs"] == 0 and m["a"] in cs:
            cs[m["a"]] += 1

    # Base de métriques pour le percentile.
    pm = lambda v, n: round(v/n, 2) if n else 0
    base = {}
    for t in teams:
        nm, n = t["t"], t["mp"] or 0
        shots, sot = tget(nm, "shooting", "shots"), tget(nm, "shooting", "shots_on_target")
        base[nm] = {
            "gf_pm": pm(t["gf"], n), "ga_pm": pm(t["ga"], n),
            "poss": tget(nm, "standard", "possession"),
            "conv": round(t["gf"]/shots*100, 1) if shots else None,
            "acc": round(sot/shots*100, 1) if shots else None,
            "cs": cs.get(nm, 0), "shots": shots, "sot": sot,
            "cards": (tget(nm, "standard", "cards_yellow") or 0)
                     + (tget(nm, "standard", "cards_red") or 0)*2,
        }

    def pct(metric, invert=False):
        vals = [base[x][metric] for x in base if base[x][metric] is not None]
        out = {}
        for nm in base:
            v = base[nm][metric]
            if v is None:
                out[nm] = None
                continue
            p = round(sum(1 for o in vals if o < v)/len(vals)*100) if vals else 0
            out[nm] = (100 - p) if invert else p
        return out

    P = {"Attaque": pct("gf_pm"), "Défense": pct("ga_pm", invert=True),
         "Possession": pct("poss"), "Finition": pct("conv"),
         "Solidité": pct("cs"), "Discipline": pct("cards", invert=True)}

    detail = {}
    for t in teams:
        nm, n = t["t"], t["mp"] or 0
        mine = [m for m in matches if nm in (m["h"], m["a"])]
        form, log = [], []
        for m in mine:
            home = m["h"] == nm
            gf_, ga_ = (m["hs"], m["as_"]) if home else (m["as_"], m["hs"])
            res = "V" if gf_ > ga_ else ("N" if gf_ == ga_ else "D")
            form.append(res)
            log.append({"date": m["d"], "opp": m["a"] if home else m["h"],
                        "gf": gf_, "ga": ga_, "res": res})
        sc = [(k, P[k].get(nm)) for k in P if P[k].get(nm) is not None]
        strengths = [k for k, v in sorted(sc, key=lambda x: -x[1]) if v >= 70][:3]
        weak = [k for k, v in sorted(sc, key=lambda x: x[1]) if v <= 30][:3]
        b = base[nm]
        style = []
        if b["poss"] is not None:
            style.append("Possession" if b["poss"] >= 52
                         else ("Bloc bas" if b["poss"] <= 46 else "Équilibré"))
        if b["gf_pm"] >= 2:
            style.append("Offensif")
        if b["ga_pm"] <= 0.7:
            style.append("Défense solide")
        if b["conv"] and b["conv"] >= 20:
            style.append("Finition clinique")
        sq = sorted(squad.get(nm, []), key=lambda p: (-p["goals"], -p["assists"], -p["minutes"]))
        for p in sq:
            p["shots"] = shots_by_player.get(p["name"])
        detail[nm] = {
            "name": nm, "group": t["g"], "rank": t["rank"], "pts": t["pts"],
            "mp": n, "w": t["w"], "d": t["d"], "l": t["l"],
            "gf": t["gf"], "ga": t["ga"], "gd": t["gd"], "note": note_map.get(nm),
            "ppg": round(t["pts"]/n, 2) if n else 0,
            "gf_pm": b["gf_pm"], "ga_pm": b["ga_pm"], "possession": b["poss"],
            "shots": b["shots"], "sot": b["sot"], "accuracy": b["acc"],
            "conversion": b["conv"], "clean_sheets": b["cs"],
            "cards_y": _int0(tget(nm, "standard", "cards_yellow")),
            "cards_r": _int0(tget(nm, "standard", "cards_red")),
            "form": form[-5:], "strengths": strengths, "weaknesses": weak,
            "style": style, "timing": timing[nm], "squad": sq[:26], "log": log,
            "formations": sorted(lineups_by_team.get(nm, []),
                                 key=lambda x: x["date"], reverse=True)[:6],
        }
    return detail


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
        events = {"live": [], "results": [], "upcoming": [], "bracket": []}

    # « À venir » vient de l'API (statut notstarted) : un match commencé en sort
    # automatiquement (il passe en direct). Repli sur la base si l'API est down.
    if events.get("upcoming"):
        upcoming = events["upcoming"]

    try:
        note_map = {p["team"]: p.get("note") for p in analytics.get("power", [])}
        teams_detail = _teams_detail(cur, events, note_map)
    except Exception:
        log.exception("Dashboard équipes indisponible ce run.")
        teams_detail = {}

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
        "teams": teams_detail,
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
