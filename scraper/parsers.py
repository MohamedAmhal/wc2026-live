"""Parsers fbref basés sur l'attribut `data-stat` (stable) plutôt que sur la
position des colonnes. Extraient groupes/classements, calendrier/matchs,
effectifs et stats joueurs depuis un BeautifulSoup déjà dé-commenté."""
import logging
import re

log = logging.getLogger(__name__)

_ID_RE = {
    "squad": re.compile(r"/squads/([a-f0-9]+)/"),
    "player": re.compile(r"/players/([a-f0-9]+)/"),
    "match": re.compile(r"/matches/([a-f0-9]+)/"),
}


# --- Helpers -----------------------------------------------------------------
def _cell(row, stat):
    """Renvoie la cellule (td/th) d'une ligne portant data-stat=stat."""
    return row.find(attrs={"data-stat": stat})


def _text(row, stat):
    c = _cell(row, stat)
    return c.get_text(strip=True) if c else None


def _clean_name(s):
    """Retire le code drapeau pays collé en préfixe (ex: 'mxMexico' -> 'Mexico',
    'krKorea Republic' -> 'Korea Republic')."""
    if not s:
        return s
    return re.sub(r"^[a-z]{2,3}(?=[A-Z])", "", s).strip()


def _name_text(row, stat):
    """Nom d'équipe/joueur propre : texte du <a> si présent, sinon cellule
    nettoyée du préfixe drapeau. fbref place le drapeau hors du lien."""
    c = _cell(row, stat)
    if not c:
        return None
    a = c.find("a")
    raw = a.get_text(strip=True) if a else c.get_text(strip=True)
    return _clean_name(raw)


def _int(row, stat):
    t = _text(row, stat)
    if t is None or t == "":
        return None
    t = t.replace(",", "")
    try:
        return int(t)
    except ValueError:
        return None


def _float(row, stat):
    t = _text(row, stat)
    if t in (None, ""):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _id_from(row, stat, kind):
    c = _cell(row, stat)
    if not c:
        return None
    a = c.find("a", href=True)
    if not a:
        return None
    m = _ID_RE[kind].search(a["href"])
    return m.group(1) if m else None


def _slug_from(row, stat, kind):
    """Extrait le slug d'URL (ex: 'Argentina-Men') pour reconstruire l'effectif."""
    c = _cell(row, stat)
    if not c:
        return None
    a = c.find("a", href=True)
    if not a:
        return None
    # /en/squads/{id}/2026/c1/Argentina-Men-Stats-World-Cup
    m = re.search(r"/squads/[a-f0-9]+/\d+/c\d+/(.+?)-Stats", a["href"])
    if m:
        return m.group(1)
    m = re.search(r"/squads/[a-f0-9]+/(.+?)-Stats", a["href"])
    return m.group(1) if m else None


def _data_rows(table):
    """Lignes de données d'une table fbref (ignore en-têtes et séparateurs)."""
    body = table.find("tbody") or table
    out = []
    for tr in body.find_all("tr"):
        cls = tr.get("class") or []
        if "thead" in cls or "spacer" in cls or "over_header" in cls:
            continue
        if tr.find("th", attrs={"data-stat": "ranker"}) is None and not tr.find("td"):
            continue
        out.append(tr)
    return out


# --- Groupes & classements ---------------------------------------------------
def parse_groups_and_standings(soup):
    """Renvoie (groups, teams, standings) depuis le hub de l'édition.

    Les tableaux de classement de groupe ont un id contenant 'overall' et une
    caption du type 'Group A Table'.
    """
    groups, teams, standings = set(), {}, []

    for table in soup.find_all("table"):
        tid = table.get("id", "") or ""
        caption = table.find("caption")
        cap_txt = caption.get_text(strip=True) if caption else ""
        if "overall" not in tid and "Group" not in cap_txt:
            continue
        m = re.search(r"(Group\s+[A-Z])", cap_txt)
        if not m:
            continue
        group_name = m.group(1)
        groups.add(group_name)

        for row in _data_rows(table):
            team_name = _name_text(row, "team")
            if not team_name:
                continue
            team_id = _id_from(row, "team", "squad")
            slug = _slug_from(row, "team", "squad")
            if team_id:
                teams[team_id] = {
                    "team_id": team_id,
                    "name": team_name,
                    "slug": slug,
                    "confederation": None,
                    "group_name": group_name,
                }
            standings.append({
                "group_name": group_name,
                "team_id": team_id,
                "team_name": team_name,
                "rank": _int(row, "rank"),
                "mp": _int(row, "games"),
                "w": _int(row, "wins"),
                "d": _int(row, "ties"),
                "l": _int(row, "losses"),
                "gf": _int(row, "goals_for"),
                "ga": _int(row, "goals_against"),
                "gd": _int(row, "goal_diff"),
                "pts": _int(row, "points"),
                "xg": _float(row, "xg_for"),
                "xga": _float(row, "xg_against"),
            })

    log.info("Parsé %d groupes, %d équipes, %d lignes de classement",
             len(groups), len(teams), len(standings))
    return sorted(groups), list(teams.values()), standings


# --- Calendrier / matchs ------------------------------------------------------
def _split_score(text):
    """'2–1' -> (2, 1). Gère en-dash/hyphen ; None si pas encore joué."""
    if not text:
        return None, None
    m = re.search(r"(\d+)\s*[–—\-]\s*(\d+)", text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def parse_schedule(soup):
    """Renvoie la liste des matchs depuis la page Scores & Fixtures."""
    matches = []
    table = None
    for t in soup.find_all("table"):
        tid = t.get("id", "") or ""
        if tid.startswith("sched"):
            table = t
            break
    if table is None:
        log.warning("Table de calendrier introuvable.")
        return matches

    for row in _data_rows(table):
        home = _name_text(row, "home_team")
        away = _name_text(row, "away_team")
        if not home and not away:
            continue
        hs, as_ = _split_score(_text(row, "score"))
        # Clé naturelle STABLE (indépendante du lien de feuille de match, qui
        # n'apparaît qu'une fois le match joué) → idempotent, zéro doublon.
        match_id = f"{_text(row, 'date')}|{home}|{away}"
        matches.append({
            "match_id": match_id,
            "match_date": _text(row, "date"),
            "match_time": _text(row, "start_time"),
            "round": _text(row, "round") or _text(row, "gameweek"),
            "home_team": home,
            "away_team": away,
            "home_score": hs,
            "away_score": as_,
            "home_xg": _float(row, "home_xg"),
            "away_xg": _float(row, "away_xg"),
            "venue": _text(row, "venue"),
            "referee": _text(row, "referee"),
            "attendance": _int(row, "attendance"),
            "notes": _text(row, "notes"),
        })
    log.info("Parsé %d matchs depuis le calendrier", len(matches))
    return matches


# --- Stats joueurs ------------------------------------------------------------
# Colonnes traitées comme identité du joueur (le reste va dans stats_json).
_IDENTITY_STATS = {
    "ranker", "player", "nationality", "team", "position", "age", "birth_year",
    "matches",  # cellule "Matches" = lien vers les feuilles de match (parasite)
}


def parse_player_stats(soup, category):
    """Renvoie (players, stats) pour une page de stats agrégées.

    `players` : dict d'identité par player_id.
    `stats`   : liste de (player_id, team_name, {colonne: valeur}).
    """
    players, stats = {}, []
    # La page contient des tables d'équipes (stats_squads_*) AVANT la table
    # joueurs. On ne garde que la table joueurs (id stats_* sans 'squads').
    table = None
    for t in soup.find_all("table"):
        tid = t.get("id", "") or ""
        if tid.startswith("stats_") and "squads" not in tid:
            table = t
            break
    if table is None:
        log.warning("Table stats '%s' introuvable.", category)
        return players, stats

    for row in _data_rows(table):
        player_name = _name_text(row, "player")
        if not player_name:
            continue
        player_id = _id_from(row, "player", "player") or f"name:{player_name}"
        team_name = _name_text(row, "team")
        players[player_id] = {
            "player_id": player_id,
            "name": player_name,
            "team_name": team_name,
            "nationality": _text(row, "nationality"),
            "position": _text(row, "position"),
            "age": _text(row, "age"),
            "birth_year": _int(row, "birth_year"),
        }
        # Toutes les colonnes data-stat hors identité -> dict de stats.
        # On garde aussi les cellules vides (valeur None) pour conserver la
        # structure complète des colonnes (fbref remplit ces stats plus tard).
        s = {}
        for cell in row.find_all(attrs={"data-stat": True}):
            stat = cell["data-stat"]
            if stat in _IDENTITY_STATS:
                continue
            val = cell.get_text(strip=True)
            s[stat] = val if val != "" else None
        stats.append((player_id, team_name, s))

    log.info("Stats '%s' : %d joueurs", category, len(players))
    return players, stats


def parse_team_stats(soup, category):
    """Stats AU NIVEAU ÉQUIPE depuis la table `stats_squads_<cat>_for`.

    C'est là que vit la possession % et les totaux d'équipe (tirs, buts...).
    Renvoie [{team_name, team_id, category, stats:{data-stat: valeur}}]."""
    out = []
    table = None
    for t in soup.find_all("table"):
        tid = t.get("id", "") or ""
        if tid.startswith("stats_squads_") and tid.endswith("_for"):
            table = t
            break
    if table is None:
        return out
    for row in _data_rows(table):
        name = _name_text(row, "team")
        if not name:
            continue
        s = {}
        for cell in row.find_all(attrs={"data-stat": True}):
            stat = cell["data-stat"]
            if stat == "team":
                continue
            v = cell.get_text(strip=True)
            s[stat] = v if v != "" else None
        out.append({
            "team_name": name,
            "team_id": _id_from(row, "team", "squad"),
            "category": category,
            "stats": s,
        })
    log.info("Stats équipe '%s' : %d équipes", category, len(out))
    return out
