# wc2026-scraper

Scraper Selenium pour collecter les données de la **Coupe du Monde 2026**
(USA/Canada/Mexique) depuis [fbref.com](https://fbref.com/en/comps/1/2026/2026-World-Cup-Stats)
et construire une base **SQLite** détaillée pour le suivi du tournoi.

## Deux sources (important)

- **native-stats.org** (sans Cloudflare, LiveView) → **scores en direct + classement** :
  les points/MJ sont **recalculés par groupe à partir des matchs joués**.
- **fbref.com** (via SeleniumBase UC) → **groupes A→L + stats joueurs riches**
  (buts, xG, passes, gardiens…).

> ⚠️ fbref a souvent **~1 jour de retard** sur les résultats. C'est pourquoi le
> classement et les scores viennent de native-stats, qui est à jour. À chaque
> `--update`, native-stats écrase scores/points avec les données live.

## Ce qui est collecté

- **Groupes** (A → L) et **classements** (MJ, V/N/D, BP/BC, diff, pts, xG/xGA)
- **Matchs** : date, phase, scores, xG, stade, arbitre, affluence
- **Joueurs** + **stats agrégées** par catégorie fbref (standard, tirs, passes,
  GCA, défense, possession, temps de jeu, divers, gardiens)
- **Stades** et **arbitres** (agrégés depuis les matchs)

Tout est stocké dans `data/worldcup2026.db` (voir le schéma dans
[`db/schema.sql`](db/schema.sql)).

## Installation

```bash
cd wc2026-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> Nécessite **Google Chrome** installé (le chromedriver est géré automatiquement
> par **SeleniumBase**, qui télécharge la version assortie à ton Chrome).

## Contournement de Cloudflare

fbref est protégé par **Cloudflare Turnstile** (case « Vérifiez que vous êtes
humain »). Le scraper utilise **SeleniumBase en mode UC** : il clique la case
automatiquement (`uc_gui_click_captcha()`) et déconnecte brièvement le webdriver
au chargement pour ne pas être détecté. Un **profil Chrome persistant**
(`.chrome-profile/`) conserve le cookie `cf_clearance` entre les runs.

- La fenêtre Chrome est **visible par défaut** (Cloudflare bloque le headless).
- Si le clic auto échoue (permission macOS « Accessibilité »), coche la case
  **à la main** : le script attend puis mémorise le cookie.

## Utilisation

```bash
python run.py --full        # collecte initiale complète
python run.py --update      # mise à jour live (classements, scores, stats clés)
python run.py --full --headless   # force le mode invisible (moins fiable)
```

Le scraper est **idempotent** : on peut le relancer autant de fois que voulu,
les données sont mises à jour (UPSERT) sans créer de doublons. Idéal pour un
suivi régulier pendant le tournoi.

### Suivi automatique (optionnel)

Pour un `--update` quotidien via cron pendant la compétition :

```cron
0 9 * * *  cd /Users/trusk/Documents/codes/wc2026-scraper && .venv/bin/python run.py --update >> logs/cron.log 2>&1
```

## Stats joueurs éclatées en colonnes (preprocessing)

Les stats sont stockées en JSON dans `player_stats.stats_json`, mais
[`preprocess.py`](preprocess.py) les **éclate en tables typées** une par
catégorie : `stats_standard`, `stats_shooting`, `stats_passing`,
`stats_keepers`, etc. (colonnes INTEGER/REAL/TEXT + identité du joueur).

```bash
python preprocess.py     # à la main (lancé aussi automatiquement par run.py)
```

Plus besoin de `json_extract` :
```sql
SELECT name, team_name, goals, assists, minutes
FROM stats_standard ORDER BY goals DESC LIMIT 10;
```

> ℹ️ Certaines catégories (passing, possession, defense, gca) sont **encore
> vides** : fbref n'a pas publié ces stats avancées pour le Mondial 2026 pour
> l'instant. Les colonnes existent et se rempliront aux prochains `run.py`
> quand fbref les publiera. Les tables `stats_standard`, `stats_shooting`,
> `stats_playing_time` et `stats_keepers` sont, elles, bien remplies.

## Exemples de requêtes

```sql
-- Classement du groupe A
SELECT rank, team_name, mp, pts, gd FROM standings
WHERE group_name = 'Group A' ORDER BY rank;

-- Meilleurs buteurs (stats standard)
SELECT name, team_name,
       json_extract(stats_json, '$.goals') AS buts
FROM players p JOIN player_stats s USING(player_id)
WHERE s.category = 'standard'
ORDER BY CAST(buts AS INTEGER) DESC LIMIT 10;

-- Matchs joués, par stade
SELECT venue_name, matches_count, total_attendance FROM venues
ORDER BY matches_count DESC;
```

## ⚠️ Limites & bon usage

- fbref / Sports-Reference impose **max 10 requêtes / minute**. Le scraper
  throttle à ~1 requête / 7 s — **ne pas baisser** cette valeur sous peine de
  bannissement IP.
- Données pour **usage personnel** uniquement (pas de redistribution massive).
- Le site est derrière Cloudflare ; `undetected-chromedriver` franchit le
  challenge JS mais le premier chargement peut prendre quelques secondes.

## Architecture

```
config.py            URLs, rate limit, chemins
scraper/driver.py    driver SeleniumBase UC (anti-Cloudflare)
scraper/fetcher.py   clic Turnstile + throttle ≤10/min + dé-commentage des tables
scraper/parsers.py   parsing par data-stat (groupes, matchs, joueurs)
scraper/pipeline.py  orchestration full / update
db/schema.sql        schéma relationnel SQLite
db/store.py          UPSERT idempotent
run.py               CLI
```
