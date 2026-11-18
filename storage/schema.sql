-- МАРСИ-Job: схема БД (новая модель монетизации, SQLite → PostgreSQL-совместимая)
-- Спецификация: data/pricing.json, промпт «Рефакторинг монетизации» §3.2

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    tg_id INTEGER UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('candidate', 'employer', 'admin')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidate_profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data_json TEXT NOT NULL,
    norm_json TEXT NOT NULL,
    completeness REAL DEFAULT 0,
    sensitive_consent INTEGER DEFAULT 0,
    weights_json TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS employers (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    company_json TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    trust_index REAL NOT NULL DEFAULT 0.7
);

CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY,
    employer_id INTEGER NOT NULL REFERENCES employers(user_id) ON DELETE CASCADE,
    data_json TEXT NOT NULL,
    norm_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'paused', 'closed', 'archived')),
    trust_index REAL NOT NULL DEFAULT 0.7,
    moderation TEXT NOT NULL DEFAULT 'pending',
    paid_until TEXT,                          -- срок оплаты размещения (миграция)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS matches (
    candidate_id INTEGER NOT NULL REFERENCES candidate_profiles(user_id) ON DELETE CASCADE,
    vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    band TEXT NOT NULL,
    breakdown_json TEXT NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0,
    block_reasons_json TEXT,
    iterations INTEGER NOT NULL DEFAULT 0,
    frozen INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (candidate_id, vacancy_id)
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    match_candidate_id INTEGER,
    match_vacancy_id INTEGER,
    kind TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS factor_config (
    version INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    factors_json TEXT NOT NULL,
    phi_json TEXT NOT NULL,
    param_weights_json TEXT NOT NULL,
    calibration_json TEXT
);

CREATE TABLE IF NOT EXISTS seed (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===== Новая модель монетизации =====

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY,
    tg_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sku_id TEXT NOT NULL,
    amount_kop INTEGER NOT NULL,
    robokassa_inv_id INTEGER UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'failed')),
    delivered INTEGER NOT NULL DEFAULT 0,     -- oneoff-услуга оказана (why_report)
    payload TEXT,                             -- напр. vacancy_id для рассылки
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    paid_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchases_tg ON purchases(tg_id, status);

-- Потребляемое: баланс приглашений (и прочие consumable)
CREATE TABLE IF NOT EXISTS entitlements (
    tg_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tg_id, kind)
);

-- Подписки: one row per plan per user (актуальная перезаписывается)
CREATE TABLE IF NOT EXISTS subscriptions (
    tg_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan TEXT NOT NULL CHECK (plan IN ('plus', 'pro', 'business')),
    active_until TEXT NOT NULL,
    last_paid_at TEXT,
    reminded_at TEXT,
    pro_period_invites_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tg_id, plan)
);

-- Флаги пользователей
CREATE TABLE IF NOT EXISTS user_flags (
    tg_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    mini_why_used INTEGER NOT NULL DEFAULT 0,
    audit_teaser_used INTEGER NOT NULL DEFAULT 0,
    consent_invites INTEGER NOT NULL DEFAULT 0,
    partner_employer INTEGER NOT NULL DEFAULT 0
);

-- Суточные счётчики
CREATE TABLE IF NOT EXISTS daily_usage (
    tg_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,                        -- YYYY-MM-DD (UTC)
    matches_shown INTEGER NOT NULL DEFAULT 0,
    alerts_sent INTEGER NOT NULL DEFAULT 0,
    broadcasts_received INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tg_id, day)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_vacancies_status ON vacancies(status);
CREATE INDEX IF NOT EXISTS idx_matches_score ON matches(candidate_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback_events(user_id, created_at);

-- Реферальная программа
CREATE TABLE IF NOT EXISTS referral_codes (
    tg_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    code TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY,
    referrer_tg_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referred_tg_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'credited')),
    credited_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_tg_id, status);

-- Свежесть: реактивации вакансий (cooldown для бесплатных)
CREATE TABLE IF NOT EXISTS vacancy_events (
    id INTEGER PRIMARY KEY,
    vacancy_db_id INTEGER NOT NULL,
    kind TEXT NOT NULL,               -- reactivate | touch | archive
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vac_events ON vacancy_events(vacancy_db_id, kind, created_at);

-- Мок-интервью: только сводки, транскрипты НЕ сохраняются
CREATE TABLE IF NOT EXISTS interview_sessions (
    id INTEGER PRIMARY KEY,
    tg_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'finished', 'expired', 'cancelled')),
    focus TEXT NOT NULL,               -- weak_points | vacancy
    vacancy_id INTEGER,                -- NULL для weak_points
    questions_asked INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,                 -- скоры по критериям, без транскрипта
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_interview_tg ON interview_sessions(tg_id, status);

-- Дорожные карты
CREATE TABLE IF NOT EXISTS roadmaps (
    id INTEGER PRIMARY KEY,
    tg_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK (target_type IN ('vacancy', 'overall')),
    vacancy_id INTEGER,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_roadmaps_tg ON roadmaps(tg_id, created_at);
