"""Événements de match (buteurs + minutes + direct) via l'API libre worldcup26.ir.

C'est la seule source gratuite/sans-clé trouvée qui donne les BUTEURS avec la
minute (`home_scorers`/`away_scorers`) + un statut de match. API « hobby » :
on l'interroge avec retries et on échoue en douceur (jamais fatal)."""
import json
import logging
import re
import urllib.request

log = logging.getLogger(__name__)

API_URL = "https://worldcup26.ir/get/games"
RETRIES = 3


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


def _status(g):
    fin = str(g.get("finished", "")).upper() == "TRUE"
    el = str(g.get("time_elapsed", "")).strip().lower()
    if fin or el in ("finished",):
        return "finished"
    if el in ("", "notstarted", "not started"):
        return "upcoming"
    return "live"  # une minute / "HT" / autre => match en cours


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
    live, results, upcoming = [], [], []
    for g in games:
        st = _status(g)
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
        }
        if st == "live":
            m["minute"] = g.get("time_elapsed")
            m["goals"] = goals
            live.append(m)
        elif st == "finished":
            m["goals"] = goals
            results.append(m)
        else:  # upcoming (notstarted) : un match commencé n'arrive jamais ici
            upcoming.append(m)

    results.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    upcoming.sort(key=lambda x: (x["date"], x["time"]))

    log.info("Événements : %d en direct, %d résultats, %d à venir",
             len(live), len(results), len(upcoming))
    return {"live": live, "results": results[:30], "upcoming": upcoming[:24]}
