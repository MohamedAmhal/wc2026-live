#!/usr/bin/env python3
"""Consulter la base Coupe du Monde 2026 sans écrire de SQL.

Exemples :
    python voir_base.py groupes              # classement des 12 groupes
    python voir_base.py groupe F             # un groupe précis
    python voir_base.py buteurs              # top buteurs
    python voir_base.py passeurs             # top passeurs
    python voir_base.py matchs               # matchs joués
    python voir_base.py matchs --avenir      # matchs à venir
    python voir_base.py equipe Argentina     # fiche d'une équipe
    python voir_base.py stades               # affluences par stade
    python voir_base.py sql "SELECT ..."     # requête SQL libre
"""
import argparse
import json
import sqlite3
import sys

import config


def connect():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _print(rows, cols=None):
    rows = [dict(r) for r in rows]
    if not rows:
        print("  (aucun résultat)")
        return
    cols = cols or list(rows[0].keys())
    widths = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    line = "  ".join(str(c).ljust(widths[c]) for c in cols)
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


# --- Vues ---------------------------------------------------------------------
def groupes(c, nom=None):
    q = ("SELECT group_name, rank, team_name, mp, w, d, l, gf, ga, gd, pts "
         "FROM standings ")
    if nom:
        g = nom if nom.lower().startswith("group") else f"Group {nom.upper()}"
        q += f"WHERE group_name='{g}' "
    q += "ORDER BY group_name, rank"
    rows = c.execute(q).fetchall()
    current = None
    for r in rows:
        if r["group_name"] != current:
            current = r["group_name"]
            print(f"\n=== {current} ===")
        print(f"  {r['rank']}. {r['team_name']:<22} {r['pts']:>2}pts  "
              f"MJ{r['mp']} {r['w']}-{r['d']}-{r['l']}  ({r['gf']}:{r['ga']}, diff {r['gd']:+})")


def _top(c, stat, label, limit):
    rows = c.execute(
        """SELECT p.name, p.team_name AS equipe,
                  CAST(json_extract(s.stats_json, ?) AS INT) AS v
           FROM players p JOIN player_stats s USING(player_id)
           WHERE s.category='standard'
           ORDER BY v DESC, p.name LIMIT ?""",
        (f"$.{stat}", limit)).fetchall()
    print(f"\nTop {limit} — {label} :")
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}. {r['v']:>2}  {r['name']} ({r['equipe']})")


def matchs(c, avenir=False):
    cond = "home_score IS NULL" if avenir else "home_score IS NOT NULL"
    rows = c.execute(
        f"""SELECT match_date, round, home_team, home_score, away_score,
                   away_team, venue FROM matches
            WHERE {cond} ORDER BY match_date, match_time""").fetchall()
    print(f"\n{'Matchs à venir' if avenir else 'Matchs joués'} ({len(rows)}) :")
    for r in rows:
        score = (f"{r['home_score']}-{r['away_score']}" if not avenir else "vs")
        print(f"  {r['match_date']}  [{r['round']}]  "
              f"{r['home_team']} {score} {r['away_team']}  @ {r['venue']}")


def equipe(c, nom):
    print(f"\n=== {nom} ===")
    st = c.execute("SELECT * FROM standings WHERE team_name LIKE ?",
                   (f"%{nom}%",)).fetchone()
    if st:
        print(f"  {st['group_name']} — {st['rank']}e, {st['pts']}pts, "
              f"MJ{st['mp']} ({st['gf']}:{st['ga']})")
    print("  Matchs :")
    for r in c.execute(
            """SELECT match_date, home_team, home_score, away_score, away_team
               FROM matches WHERE home_team LIKE ? OR away_team LIKE ?
               ORDER BY match_date""", (f"%{nom}%", f"%{nom}%")):
        sc = (f"{r['home_score']}-{r['away_score']}"
              if r["home_score"] is not None else "vs")
        print(f"    {r['match_date']}  {r['home_team']} {sc} {r['away_team']}")
    print("  Effectif (buteurs en tête) :")
    for r in c.execute(
            """SELECT p.name, p.position,
                      CAST(json_extract(s.stats_json,'$.goals') AS INT) g,
                      CAST(json_extract(s.stats_json,'$.assists') AS INT) a
               FROM players p JOIN player_stats s USING(player_id)
               WHERE s.category='standard' AND p.team_name LIKE ?
               ORDER BY g DESC, a DESC LIMIT 12""", (f"%{nom}%",)):
        print(f"    {r['name']:<24} {r['position'] or '':<4} {r['g']}b {r['a']}pd")


def stades(c):
    _print(c.execute(
        "SELECT venue_name, matches_count, total_attendance "
        "FROM venues ORDER BY matches_count DESC, total_attendance DESC").fetchall())


def main():
    p = argparse.ArgumentParser(description="Consulter la base Coupe du Monde 2026")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("groupes"); g.add_argument("nom", nargs="?")
    sub.add_parser("groupe").add_argument("nom")
    b = sub.add_parser("buteurs"); b.add_argument("-n", type=int, default=15)
    pa = sub.add_parser("passeurs"); pa.add_argument("-n", type=int, default=15)
    m = sub.add_parser("matchs"); m.add_argument("--avenir", action="store_true")
    sub.add_parser("equipe").add_argument("nom")
    sub.add_parser("stades")
    sub.add_parser("sql").add_argument("requete")
    a = p.parse_args()

    c = connect()
    if a.cmd in ("groupes", "groupe"):
        groupes(c, getattr(a, "nom", None))
    elif a.cmd == "buteurs":
        _top(c, "goals", "Buteurs", a.n)
    elif a.cmd == "passeurs":
        _top(c, "assists", "Passeurs", a.n)
    elif a.cmd == "matchs":
        matchs(c, a.avenir)
    elif a.cmd == "equipe":
        equipe(c, a.nom)
    elif a.cmd == "stades":
        stades(c)
    elif a.cmd == "sql":
        _print(c.execute(a.requete).fetchall())


if __name__ == "__main__":
    sys.exit(main())
