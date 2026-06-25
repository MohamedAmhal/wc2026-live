"""Source LIVE : native-stats.org (Phoenix LiveView, sans Cloudflare).

fbref a ~1 jour de retard sur les résultats ; native-stats est à jour. On y
récupère les scores des matchs, puis on RECALCULE le classement par groupe à
partir des matchs (MJ/V/N/D/BP/BC/diff/pts) — points exacts et cohérents.

Le découpage en groupes vient de fbref (table teams) : pendant la phase de
groupes, les deux équipes d'un match sont dans le même groupe."""
import logging
import re
import time
import unicodedata

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By

log = logging.getLogger(__name__)

NATIVE_URL = "https://native-stats.org/competition/WC/"
LIVEVIEW_WAIT = 5      # secondes pour la connexion LiveView
PAGE_WAIT = 2.5        # secondes après un clic de pagination
MAX_PAGES = 10         # garde-fou de pagination

# Correspondance noms native-stats -> noms fbref (forme normalisée -> nom fbref).
# Seuls les cas qui diffèrent ; le reste matche par normalisation directe.
_ALIASES = {
    "southkorea": "Korea Republic",
    "korearepublic": "Korea Republic",
    "bosniaherzegovina": "Bosnia & Herz.",
    "iran": "IR Iran",
    "iriran": "IR Iran",
    "ivorycoast": "Côte d'Ivoire",
    "cotedivoire": "Côte d'Ivoire",
    "turkey": "Türkiye",
    "turkiye": "Türkiye",
    "usa": "United States",
    "unitedstates": "United States",
    "capeverde": "Cabo Verde",
    "capeverdeislands": "Cabo Verde",
    "caboverde": "Cabo Verde",
    "curacao": "Curaçao",
    "congodr": "Congo DR",
    "drcongo": "Congo DR",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def _team_name_from_block(side: str) -> str:
    """'South Korea South Korea KOR' -> 'South Korea'. native-stats répète le
    nom (responsive) puis met le code pays en majuscules à la fin."""
    side = side.strip()
    side = re.sub(r"\s+[A-Z]{2,4}$", "", side)   # retire le code pays final
    tokens = side.split()
    if len(tokens) % 2 == 0 and tokens[: len(tokens) // 2] == tokens[len(tokens) // 2:]:
        tokens = tokens[: len(tokens) // 2]       # nom dédoublé -> première moitié
    return " ".join(tokens).strip()


def _parse_recent_rows(soup):
    """Renvoie [(home_raw, away_raw, hs, as)] de la table des matchs récents."""
    out = []
    tables = soup.find_all("table")
    if not tables:
        return out
    recent = tables[0]
    for tr in recent.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 3:
            continue
        label = cells[1].get_text(" ", strip=True)
        score = cells[2].get_text(strip=True)
        if " - " not in label:
            continue
        m = re.match(r"\s*(\d+)\s*:\s*(\d+)\s*$", score)
        if not m:
            continue  # match non joué (pas de score)
        home_raw, away_raw = label.split(" - ", 1)
        out.append((
            _team_name_from_block(home_raw),
            _team_name_from_block(away_raw),
            int(m.group(1)), int(m.group(2)),
        ))
    return out


def collect_matches(driver):
    """Charge native-stats et pagine la table « matchs récents » pour récupérer
    TOUS les matchs joués. Renvoie un dict {(home,away): (hs, as)}."""
    driver.get(NATIVE_URL)
    time.sleep(LIVEVIEW_WAIT)
    collected = {}
    for page in range(MAX_PAGES):
        soup = BeautifulSoup(driver.page_source, "html.parser")
        rows = _parse_recent_rows(soup)
        new = 0
        for home, away, hs, as_ in rows:
            key = (home, away)
            if key not in collected:
                collected[key] = (hs, as_)
                new += 1
        log.info("native-stats page %d : %d matchs (%d nouveaux)", page + 1, len(rows), new)
        if new == 0 and page > 0:
            break  # plus rien de nouveau -> fin de l'historique
        # Clique le « » » de la table des matchs récents (1er bouton set_page).
        btns = driver.find_elements(By.CSS_SELECTOR, '[phx-click="set_page"]')
        if not btns:
            break
        try:
            btns[0].click()
        except Exception as exc:
            log.debug("pagination terminée : %s", exc)
            break
        time.sleep(PAGE_WAIT)
    log.info("native-stats : %d matchs joués collectés", len(collected))
    return collected


def update_live(store, driver):
    """Met à jour les scores des matchs puis recalcule le classement par groupe.

    Renvoie (nb_matchs_maj, nb_non_appariés)."""
    conn = store.conn
    # 1) Table de correspondance noms -> nom fbref (depuis la base).
    fbref_names = [r[0] for r in conn.execute("SELECT team_name FROM standings")]
    lookup = {_norm(n): n for n in fbref_names}
    lookup.update({k: v for k, v in _ALIASES.items()})

    def resolve(name):
        return lookup.get(_norm(name))

    # 2) Scores depuis native-stats.
    native = collect_matches(driver)

    # 3) Met à jour matches.home_score/away_score (toutes orientations).
    updated, unmatched = 0, 0
    cur = conn.cursor()
    for (h_raw, a_raw), (hs, as_) in native.items():
        h, a = resolve(h_raw), resolve(a_raw)
        if not h or not a:
            unmatched += 1
            log.warning("Non apparié : %r vs %r", h_raw, a_raw)
            continue
        # même affiche, orientation directe ou inversée
        r = cur.execute(
            "UPDATE matches SET home_score=?, away_score=? "
            "WHERE home_team=? AND away_team=?", (hs, as_, h, a)).rowcount
        if r == 0:
            r = cur.execute(
                "UPDATE matches SET home_score=?, away_score=? "
                "WHERE home_team=? AND away_team=?", (as_, hs, a, h)).rowcount
        updated += 1 if r else 0
    conn.commit()
    store.dedupe_matches()  # auto-répare d'éventuels doublons de fixture
    store.rebuild_venues_and_referees()

    # 4) Recalcule le classement par groupe à partir des matchs joués.
    _recompute_standings(store)
    log.info("Live native-stats : %d matchs mis à jour, %d non appariés", updated, unmatched)
    return updated, unmatched


def _recompute_standings(store):
    conn = store.conn
    team_group = {r["team_name"]: r["group_name"]
                  for r in conn.execute("SELECT team_name, group_name FROM standings")}
    # Init des stats par équipe.
    stats = {t: dict(mp=0, w=0, d=0, l=0, gf=0, ga=0, pts=0) for t in team_group}

    rows = conn.execute(
        "SELECT home_team, away_team, home_score, away_score FROM matches "
        "WHERE home_score IS NOT NULL AND away_score IS NOT NULL")
    for r in rows:
        h, a, hs, as_ = r["home_team"], r["away_team"], r["home_score"], r["away_score"]
        if h not in stats or a not in stats:
            continue
        for team, gf, ga in ((h, hs, as_), (a, as_, hs)):
            s = stats[team]
            s["mp"] += 1; s["gf"] += gf; s["ga"] += ga
            if gf > ga:
                s["w"] += 1; s["pts"] += 3
            elif gf == ga:
                s["d"] += 1; s["pts"] += 1
            else:
                s["l"] += 1

    # Classement par groupe : tri pts, diff, BP.
    by_group = {}
    for team, g in team_group.items():
        by_group.setdefault(g, []).append(team)
    new_rows = []
    for g, teams in by_group.items():
        ranked = sorted(teams, key=lambda t: (
            stats[t]["pts"], stats[t]["gf"] - stats[t]["ga"], stats[t]["gf"]), reverse=True)
        for rank, t in enumerate(ranked, 1):
            s = stats[t]
            new_rows.append({
                "group_name": g, "team_id": None, "team_name": t, "rank": rank,
                "mp": s["mp"], "w": s["w"], "d": s["d"], "l": s["l"],
                "gf": s["gf"], "ga": s["ga"], "gd": s["gf"] - s["ga"], "pts": s["pts"],
                "xg": None, "xga": None,
            })
    # team_id/xg ne doivent pas être écrasés à NULL : on ne met à jour que les
    # colonnes calculées via un upsert ciblé.
    for row in new_rows:
        conn.execute(
            """INSERT INTO standings (group_name, team_name, rank, mp, w, d, l, gf, ga, gd, pts)
               VALUES (:group_name, :team_name, :rank, :mp, :w, :d, :l, :gf, :ga, :gd, :pts)
               ON CONFLICT(group_name, team_name) DO UPDATE SET
                 rank=excluded.rank, mp=excluded.mp, w=excluded.w, d=excluded.d,
                 l=excluded.l, gf=excluded.gf, ga=excluded.ga, gd=excluded.gd,
                 pts=excluded.pts""", row)
    conn.commit()
    log.info("Classement recalculé pour %d groupes", len(by_group))
