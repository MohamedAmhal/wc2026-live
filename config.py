"""Configuration centrale du scraper Coupe du Monde 2026 (fbref)."""
from pathlib import Path

# --- Chemins -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "worldcup2026.db"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"

for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Cible fbref -------------------------------------------------------------
BASE_URL = "https://fbref.com"
SEASON = "2026"
COMP_ID = "1"  # id de compétition fbref pour la Coupe du Monde

# Hub de l'édition : contient les 12 tableaux de groupes + classements.
HUB_URL = f"{BASE_URL}/en/comps/{COMP_ID}/{SEASON}/{SEASON}-World-Cup-Stats"

# Calendrier / scores complets du tournoi.
SCHEDULE_URL = (
    f"{BASE_URL}/en/comps/{COMP_ID}/{SEASON}/schedule/"
    f"{SEASON}-World-Cup-Scores-and-Fixtures"
)

# Pages de stats joueurs agrégées, une par catégorie fbref.
# clé interne -> segment d'URL fbref
PLAYER_STAT_CATEGORIES = {
    "standard": "stats",
    "shooting": "shooting",
    "passing": "passing",
    "passing_types": "passing_types",
    "gca": "gca",
    "defense": "defense",
    "possession": "possession",
    "playing_time": "playingtime",
    "misc": "misc",
    "keepers": "keepers",
    "keepers_adv": "keepersadv",
}


def player_stats_url(category_segment: str) -> str:
    return (
        f"{BASE_URL}/en/comps/{COMP_ID}/{SEASON}/{category_segment}/"
        f"{SEASON}-World-Cup-Stats"
    )


def squad_url(team_id: str, team_slug: str) -> str:
    """URL de l'effectif d'une équipe pour l'édition 2026."""
    return f"{BASE_URL}/en/squads/{team_id}/{SEASON}/c{COMP_ID}/{team_slug}-Stats-World-Cup"


# --- Politique de débit (respect strict des règles Sports-Reference) ---------
# fbref/Sports-Reference : max 10 requêtes / minute. On reste conservateur.
RATE_LIMIT_SECONDS = 7.0      # délai minimum entre deux requêtes
RATE_LIMIT_JITTER = 2.0       # jitter aléatoire ajouté (0..JITTER s)
MAX_RETRIES = 4               # tentatives en cas de 429 / blocage
RETRY_BACKOFF_SECONDS = 30.0  # base d'attente si 429 sans header Retry-After

# --- Selenium ----------------------------------------------------------------
# Cloudflare bloque beaucoup plus agressivement le mode headless : on lance
# Chrome en mode visible par défaut (le plus fiable).
HEADLESS = False

# Profil Chrome persistant : conserve le cookie cf_clearance posé après la
# résolution du défi Cloudflare, pour que les runs suivants passent tout seuls.
CHROME_PROFILE_DIR = BASE_DIR / ".chrome-profile"

PAGE_LOAD_TIMEOUT = 45        # secondes
UC_RECONNECT = 5.0            # délai de reconnexion uc_open (laisse le défi se lancer)
CLOUDFLARE_MAX_WAIT = 30      # plafond d'attente / tentative de clic auto (s)
CLOUDFLARE_POLL = 1.5         # intervalle de polling pendant le challenge

# Si le défi n'est pas franchi automatiquement et que Chrome est visible,
# on laisse à l'humain le temps de cocher la case manuellement (une seule fois).
MANUAL_SOLVE = True
MANUAL_SOLVE_WAIT = 150       # secondes accordées à la résolution manuelle

# On NE force PLUS le user-agent : un UA figé (ex. Chrome/126) qui ne correspond
# pas à la vraie version de Chrome est un signal fort pour Cloudflare. Laisser
# Chrome envoyer son UA natif réduit nettement le risque de défi.
USER_AGENT = None
