-- Schéma relationnel SQLite — Coupe du Monde 2026 (fbref)
-- Idempotent : `CREATE TABLE IF NOT EXISTS`, clés primaires naturelles pour
-- permettre des UPSERT (INSERT ... ON CONFLICT) lors des runs --update.

PRAGMA foreign_keys = ON;

-- Groupes (A → L, 12 groupes en 2026)
CREATE TABLE IF NOT EXISTS groups (
    group_name TEXT PRIMARY KEY            -- "Group A", ...
);

-- Équipes participantes
CREATE TABLE IF NOT EXISTS teams (
    team_id      TEXT PRIMARY KEY,         -- id fbref (ex: f9fddd6e)
    name         TEXT NOT NULL,
    slug         TEXT,                     -- pour reconstruire l'URL d'effectif
    confederation TEXT,
    group_name   TEXT,
    FOREIGN KEY (group_name) REFERENCES groups(group_name)
);

-- Classements par groupe (snapshot ; écrasé à chaque --update)
CREATE TABLE IF NOT EXISTS standings (
    group_name TEXT,
    team_id    TEXT,
    team_name  TEXT,
    rank       INTEGER,
    mp         INTEGER,  -- matchs joués
    w          INTEGER,
    d          INTEGER,
    l          INTEGER,
    gf         INTEGER,  -- buts pour
    ga         INTEGER,  -- buts contre
    gd         INTEGER,  -- différence
    pts        INTEGER,
    xg         REAL,
    xga        REAL,
    PRIMARY KEY (group_name, team_name),
    FOREIGN KEY (group_name) REFERENCES groups(group_name)
);

-- Matchs (groupes + phases finales)
CREATE TABLE IF NOT EXISTS matches (
    match_id    TEXT PRIMARY KEY,          -- id fbref du match (depuis l'URL)
    match_date  TEXT,
    match_time  TEXT,
    round       TEXT,                      -- "Group A", "Round of 32", ...
    home_team   TEXT,
    away_team   TEXT,
    home_score  INTEGER,
    away_score  INTEGER,
    home_xg     REAL,
    away_xg     REAL,
    venue       TEXT,
    referee     TEXT,
    attendance  INTEGER,
    notes       TEXT
);

-- Joueurs
CREATE TABLE IF NOT EXISTS players (
    player_id   TEXT PRIMARY KEY,          -- id fbref du joueur
    name        TEXT NOT NULL,
    team_name   TEXT,
    nationality TEXT,
    position    TEXT,
    age         TEXT,
    birth_year  INTEGER
);

-- Stats joueurs agrégées sur le tournoi.
-- Une ligne par (joueur, catégorie) ; les stats sont stockées en JSON pour
-- absorber le nombre variable de colonnes selon la catégorie fbref.
CREATE TABLE IF NOT EXISTS player_stats (
    player_id   TEXT,
    category    TEXT,                      -- standard, shooting, passing, ...
    team_name   TEXT,
    stats_json  TEXT,                      -- dict colonne->valeur sérialisé
    PRIMARY KEY (player_id, category),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

-- Stades / lieux (dérivés des matchs)
CREATE TABLE IF NOT EXISTS venues (
    venue_name      TEXT PRIMARY KEY,
    matches_count   INTEGER DEFAULT 0,
    total_attendance INTEGER DEFAULT 0
);

-- Arbitres (dérivés des matchs)
CREATE TABLE IF NOT EXISTS referees (
    referee_name  TEXT PRIMARY KEY,
    matches_count INTEGER DEFAULT 0
);

-- Métadonnées de run (traçabilité du suivi live)
CREATE TABLE IF NOT EXISTS scrape_runs (
    run_ts   TEXT PRIMARY KEY,
    mode     TEXT,
    pages    INTEGER,
    notes    TEXT
);
