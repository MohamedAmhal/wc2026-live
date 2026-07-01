"""Événements de match (buteurs + minutes + direct) via l'API libre worldcup26.ir.

C'est la seule source gratuite/sans-clé trouvée qui donne les BUTEURS avec la
minute (`home_scorers`/`away_scorers`) + un statut de match. API « hobby » :
on l'interroge avec retries et on échoue en douceur (jamais fatal)."""
import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

API_URL = "https://worldcup26.ir/get/games"
RETRIES = 3

PARIS = ZoneInfo("Europe/Paris")

# stadium_id (worldcup26.ir) -> fuseau horaire IANA du stade.
STADIUM_TZ = {
    "1": "America/Mexico_City",   # Estadio Azteca (Mexico City)
    "2": "America/Mexico_City",   # Estadio Akron (Guadalajara)
    "3": "America/Monterrey",     # Estadio BBVA (Monterrey)
    "4": "America/Chicago",       # AT&T Stadium (Dallas)
    "5": "America/Chicago",       # NRG Stadium (Houston)
    "6": "America/Chicago",       # Arrowhead (Kansas City)
    "7": "America/New_York",      # Mercedes-Benz (Atlanta)
    "8": "America/New_York",      # Hard Rock (Miami)
    "9": "America/New_York",      # Gillette (Boston)
    "10": "America/New_York",     # Lincoln Financial (Philadelphia)
    "11": "America/New_York",     # MetLife (New York/NJ)
    "12": "America/Toronto",      # BMO Field (Toronto)
    "13": "America/Vancouver",    # BC Place (Vancouver)
    "14": "America/Los_Angeles",  # Lumen Field (Seattle)
    "15": "America/Los_Angeles",  # Levi's (San Francisco Bay Area)
    "16": "America/Los_Angeles",  # SoFi (Los Angeles)
}


def _paris_time(iso, hhmm, stadium_id):
    """Heure de Paris correspondant à l'heure locale du match. '' si inconnu.
    Suffixe ' +1' si Paris est le jour suivant (décalage horaire)."""
    tz = STADIUM_TZ.get(str(stadium_id))
    if not (iso and hhmm and tz):
        return ""
    try:
        dt = datetime.strptime(f"{iso} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(tz))
        p = dt.astimezone(PARIS)
        return p.strftime("%H:%M") + ("" if p.date() == dt.date() else " +1")
    except Exception:
        return ""


def fetch_games():
    """Récupère la liste des matchs (avec retries). Renvoie [] si l'API tombe."""
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(API_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
            games = d if isinstance(d, list) else (d.get("data") or d.get("games") or [])
            if games:
                return games
        except Exception as exc:
            log.warning("API worldcup26 tentative %d échouée : %s", attempt, exc)
    log.error("API worldcup26 indisponible — section live ignorée ce run.")
    return []


def _parse_scorers(raw, team):
    """'{\"Nestory Irankunda 27\\'\",\"C. Metcalfe 45+2\\'\"}' ->
    [{'player':'Nestory Irankunda','minute':\"27\",'team':'home'}, ...]."""
    if not raw or str(raw).strip() in ("null", "None", ""):
        return []
    out = []
    for item in re.findall(r'"([^"]*)"', str(raw)) or [str(raw)]:
        item = item.strip()
        if not item:
            continue
        m = re.match(r"^(.*?)[\s]+(\d+(?:\+\d+)?)'?\s*$", item)
        if m:
            out.append({"player": m.group(1).strip(), "minute": m.group(2), "team": team})
        else:
            out.append({"player": item, "minute": None, "team": team})
    return out


def _kickoff_utc(g):
    """Coup d'envoi en UTC (depuis l'heure locale du stade). None si inconnu."""
    iso, hh = _parse_date(g.get("local_date"))
    tz = STADIUM_TZ.get(str(g.get("stadium_id")))
    if not (iso and hh and tz):
        return None
    try:
        naive = datetime.strptime(f"{iso} {hh}", "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    except Exception:
        return None


# Durée max d'un match (prolongations comprises) pour la fenêtre "en direct".
LIVE_WINDOW = timedelta(minutes=150)


def _status(g, now=None):
    """finished / live / upcoming. Le direct est détecté soit par l'API
    (time_elapsed = une minute), soit par l'HEURE (coup d'envoi passé et match
    non terminé) car l'API worldcup26 ne fournit pas la minute en cours."""
    fin = str(g.get("finished", "")).upper() == "TRUE"
    el = str(g.get("time_elapsed", "")).strip().lower()
    if fin or el == "finished":
        return "finished"
    if el not in ("", "notstarted", "not started"):
        return "live"  # l'API donne une minute / "HT"
    if now is not None:
        ko = _kickoff_utc(g)
        if ko and ko <= now <= ko + LIVE_WINDOW:
            return "live"  # fenêtre horaire : match en cours
    return "upcoming"


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_date(local_date):
    """'06/13/2026 21:00' -> ('2026-06-13', '21:00'). Tolérant."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}:\d{2}))?", str(local_date or ""))
    if not m:
        return "", ""
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}", (m.group(4) or "")


def build():
    """Renvoie {'live': [...], 'results': [...]} prêts pour la PWA.

    Chaque match : home/away, score, group, matchday, minute (si live),
    et `goals` trié par minute (joueur + minute + côté)."""
    games = fetch_games()
    now = datetime.now(timezone.utc)
    live, results, upcoming = [], [], []
    for g in games:
        st = _status(g, now)
        iso, hhmm = _parse_date(g.get("local_date"))
        goals = (_parse_scorers(g.get("home_scorers"), "home")
                 + _parse_scorers(g.get("away_scorers"), "away"))
        goals.sort(key=lambda x: (int(re.split(r"\+", x["minute"])[0])
                                  if x["minute"] else 999))
        m = {
            "home": g.get("home_team_name_en"), "away": g.get("away_team_name_en"),
            "hs": _to_int(g.get("home_score")), "as": _to_int(g.get("away_score")),
            "group": g.get("group"), "matchday": g.get("matchday"),
            "date": iso, "time": hhmm,
            "time_paris": _paris_time(iso, hhmm, g.get("stadium_id")),
        }
        if st == "live":
            el = str(g.get("time_elapsed", "")).strip()
            m["minute"] = el if re.match(r"^\d", el) else None  # minute si l'API la donne
            m["goals"] = goals
            live.append(m)
        elif st == "finished":
            m["goals"] = goals
            results.append(m)
        else:  # upcoming (notstarted) : un match commencé n'arrive jamais ici
            upcoming.append(m)

    results.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    upcoming.sort(key=lambda x: (x["date"], x["time"]))

    # --- Tableau à élimination directe (se remplit au fil du tournoi) ----
    ROUND_ORDER = ["r32", "r16", "qf", "sf", "final"]
    LABELS = {"r32": "16es de finale", "r16": "8es de finale",
              "qf": "Quarts de finale", "sf": "Demi-finales", "final": "Finale"}
    by_round = {k: [] for k in ROUND_ORDER}
    for g in games:
        ty = g.get("type")
        if ty not in by_round:
            continue
        iso, hh = _parse_date(g.get("local_date"))
        st = _status(g, now)
        el = str(g.get("time_elapsed", "")).strip()
        by_round[ty].append({
            "home": g.get("home_team_name_en"), "away": g.get("away_team_name_en"),
            "hs": _to_int(g.get("home_score")), "as": _to_int(g.get("away_score")),
            "date": iso, "time": hh, "status": st,
            "minute": (el if re.match(r"^\d", el) else None) if st == "live" else None,
            "id": str(g.get("id") or ""),
        })
    for k in by_round:
        by_round[k].sort(key=lambda m: (m["date"], m["time"], m["id"]))
    bracket = [{"key": k, "label": LABELS[k], "matches": by_round[k]} for k in ROUND_ORDER]

    log.info("Événements : %d en direct, %d résultats, %d à venir, bracket=%s",
             len(live), len(results), len(upcoming),
             {b["key"]: len(b["matches"]) for b in bracket})
    return {"live": live, "results": results, "upcoming": upcoming[:24], "bracket": bracket}
