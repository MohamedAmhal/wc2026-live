#!/usr/bin/env python3
"""Éclate la colonne JSON `player_stats.stats_json` en vraies colonnes typées.

Pour chaque catégorie fbref (standard, shooting, passing, ...), crée une table
`stats_<categorie>` avec :
  - colonnes d'identité : player_id, name, team_name, nationality, position,
    age, birth_year
  - une colonne par statistique (numérique → REAL/INT, sinon TEXT)

Idempotent : les tables sont reconstruites à chaque exécution.
Lancé automatiquement en fin de `run.py`, ou à la main : `python preprocess.py`.
"""
import json
import logging
import re
import sqlite3

import config

log = logging.getLogger(__name__)

# Colonnes d'identité tirées de la table players.
_IDENTITY = ["name", "team_name", "nationality", "position", "age", "birth_year"]

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _is_num(v):
    return v is not None and v != "" and bool(_NUM_RE.match(str(v).strip()))


def _to_num(v):
    f = float(v)
    return int(f) if f.is_integer() else f


def _safe_col(name):
    """Nom de colonne SQL sûr (les data-stat fbref sont déjà en snake_case)."""
    c = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if c and c[0].isdigit():
        c = "_" + c
    return c


def preprocess(db_path=config.DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Identité des joueurs.
    players = {r["player_id"]: dict(r) for r in
               cur.execute(f"SELECT player_id, {', '.join(_IDENTITY)} FROM players")}

    categories = [r[0] for r in
                  cur.execute("SELECT DISTINCT category FROM player_stats")]
    if not categories:
        log.warning("Aucune donnée dans player_stats — lance d'abord run.py.")
        return []

    created = []
    for cat in categories:
        rows = cur.execute(
            "SELECT player_id, team_name, stats_json FROM player_stats "
            "WHERE category=?", (cat,)).fetchall()
        parsed = [(r["player_id"], r["team_name"], json.loads(r["stats_json"]))
                  for r in rows]

        # Union ordonnée des clés + type par colonne (INTEGER / REAL / TEXT).
        stat_keys, numeric, integral = [], {}, {}
        for _, _, s in parsed:
            for k, v in s.items():
                if k not in numeric:
                    numeric[k] = True
                    integral[k] = True
                    stat_keys.append(k)
                if v in (None, ""):
                    continue
                if not _is_num(v):
                    numeric[k] = False
                elif "." in str(v):
                    integral[k] = False  # valeur décimale => REAL plutôt qu'INTEGER

    # type final : INTEGER si numérique entier, REAL si numérique décimal, sinon TEXT
        def col_type(k):
            if not numeric[k]:
                return "TEXT"
            return "INTEGER" if integral[k] else "REAL"

        table = f"stats_{_safe_col(cat)}"
        cols_sql = ['player_id TEXT PRIMARY KEY']
        cols_sql += [f'{c} TEXT' for c in _IDENTITY]
        for k in stat_keys:
            cols_sql.append(f'"{_safe_col(k)}" {col_type(k)}')

        cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(f"CREATE TABLE {table} ({', '.join(cols_sql)})")

        # Insertion.
        all_cols = ["player_id"] + _IDENTITY + [_safe_col(k) for k in stat_keys]
        placeholders = ", ".join("?" * len(all_cols))
        quoted_cols = ", ".join('"%s"' % c for c in all_cols)
        insert = f'INSERT INTO {table} ({quoted_cols}) VALUES ({placeholders})'
        batch = []
        for pid, team_name, s in parsed:
            ident = players.get(pid, {})
            vals = [pid]
            vals += [ident.get(c) for c in _IDENTITY]
            for k in stat_keys:
                v = s.get(k)
                vals.append(_to_num(v) if numeric[k] and _is_num(v)
                            else (v if v != "" else None))
            batch.append(vals)
        cur.executemany(insert, batch)
        conn.commit()
        log.info("Table %s : %d joueurs, %d colonnes de stats",
                 table, len(batch), len(stat_keys))
        created.append((table, len(batch), len(stat_keys)))

    conn.close()
    return created


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    res = preprocess()
    print("\n✅ Tables créées :")
    for table, nrows, ncols in res:
        print(f"  {table:<24} {nrows:>4} joueurs  {ncols:>3} colonnes")
