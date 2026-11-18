"""Хранилище МАРСИ-Job на SQLite (схема storage/schema.sql)."""
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "storage" / "schema.sql"


def clamp01f(x: float) -> float:
    return max(0.0, min(1.0, x))


class Storage:
    def __init__(self, db_path: Union[str, Path] = ":memory:"):
        # каждое соединение живёт в своём потоке (sqlite3.threadsafety=1):
        # фоновый синк открывает отдельный Storage с тем же путём;
        # timeout=30 — ожидание файловой блокировки между соединениями
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._archive_legacy_tables()
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._apply_column_migrations()
        self.conn.commit()

    def clone(self) -> "Storage":
        """Отдельное соединение к той же БД (для фоновых потоков)."""
        return Storage(self.db_path)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------ пользователи ------------------------
    def upsert_user(self, tg_id: int, role: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO users (tg_id, role) VALUES (?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET role = excluded.role",
            (tg_id, role),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
        return int(row["id"])

    def get_user(self, tg_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()

    # ------------------------------ профили ------------------------------
    def save_candidate_profile(
        self, user_id: int, data: Dict[str, Any], completeness: float = 1.0,
        sensitive_consent: bool = False,
    ) -> None:
        self.conn.execute(
            "INSERT INTO candidate_profiles (user_id, data_json, norm_json, completeness, "
            "sensitive_consent, updated_at) VALUES (?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET data_json = excluded.data_json, "
            "norm_json = excluded.norm_json, completeness = excluded.completeness, "
            "sensitive_consent = excluded.sensitive_consent, updated_at = excluded.updated_at",
            (user_id, json.dumps(data, ensure_ascii=False), json.dumps(data, ensure_ascii=False),
             completeness, int(sensitive_consent)),
        )
        self.conn.commit()

    def get_candidate_profile(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT data_json FROM candidate_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        return json.loads(row["data_json"]) if row else None

    # ------------------------------ внешние вакансии (hh.ru) -------------
    def ensure_external_employer(self, name: str) -> int:
        """Служебный работодатель для внешних источников; возвращает его user_id."""
        row = self.conn.execute(
            "SELECT user_id FROM employers WHERE company_json LIKE ? LIMIT 1",
            (f'%"name": "{name}"%',),
        ).fetchone()
        if row:
            return int(row["user_id"])
        user_id = self.upsert_user(-abs(hash(name)) % 10**9, "employer")
        self.conn.execute(
            "INSERT INTO employers (user_id, company_json, verified, trust_index) "
            "VALUES (?, ?, 0, 0.55) ON CONFLICT(user_id) DO NOTHING",
            (user_id, json.dumps({"name": name, "external": True}, ensure_ascii=False)),
        )
        self.conn.commit()
        return user_id

    def upsert_external_vacancy(self, data: Dict[str, Any], employer_id: int) -> str:
        """Вставка/обновление внешней вакансии по коду. Возвращает 'inserted'|'updated'."""
        code = data["id"]
        row = self.conn.execute(
            "SELECT id FROM vacancies WHERE data_json LIKE ? LIMIT 1",
            (f'%"id": "{code}"%',),
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE vacancies SET data_json = ?, norm_json = ?, status = 'active', "
                "created_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), json.dumps(data, ensure_ascii=False),
                 int(row["id"])),
            )
            self.conn.commit()
            return "updated"
        self.insert_vacancy(employer_id, data, status="active", moderation="approved")
        return "inserted"

    def prune_external_vacancies(self, stale_days: int = 14,
                                 delete_after_days: int = 60) -> int:
        """Закрывает протухшие внешние вакансии (hh/trudvsem), удаляет старые закрытые."""
        cur = self.conn.execute(
            "UPDATE vacancies SET status = 'closed' "
            "WHERE status = 'active' AND created_at < datetime('now', ?) "
            "AND (data_json LIKE '%\"source\": \"hh\"%' "
            "     OR data_json LIKE '%\"source\": \"trudvsem\"%' "
            "     OR data_json LIKE '%\"source\": \"habr\"%')",
            (f'-{stale_days} days',),
        )
        closed = cur.rowcount
        self.conn.execute(
            "DELETE FROM vacancies WHERE status = 'closed' "
            "AND created_at < datetime('now', ?)",
            (f'-{delete_after_days} days',),
        )
        self.conn.commit()
        return closed

    def vacancy_source_stats(self) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT CASE "
            "  WHEN data_json LIKE '%\"source\": \"hh\"%' THEN 'hh' "
            "  WHEN data_json LIKE '%\"source\": \"trudvsem\"%' THEN 'trudvsem' "
            "  WHEN data_json LIKE '%\"source\": \"habr\"%' THEN 'habr' "
            "  ELSE 'direct' END AS src, COUNT(*) AS c "
            "FROM vacancies WHERE status = 'active' GROUP BY src"
        ).fetchall()
        return {row["src"]: int(row["c"]) for row in rows}

    def count_active_vacancies(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM vacancies WHERE status = 'active'"
        ).fetchone()
        return int(row["c"])

    # ------------------------- миграции и legacy -------------------------
    def _archive_legacy_tables(self) -> None:
        """Старые монетизационные таблицы (кредиты/пакеты/премиум) — в *_legacy.
        Данные пользователей (профили, вакансии, матчинги) не трогаем."""
        legacy_map = {"purchases": "product", "credits": "balance", "premium": "until"}
        for table, marker_col in legacy_map.items():
            row = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not row:
                continue
            cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(%s)" % table)]
            if marker_col not in cols:
                continue  # уже новая форма
            legacy = table + "_legacy"
            exists = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (legacy,)
            ).fetchone()
            if not exists:
                self.conn.execute("ALTER TABLE %s RENAME TO %s" % (table, legacy))
        self.conn.commit()

    def _apply_column_migrations(self) -> None:
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(vacancies)")]
        if cols and "paid_until" not in cols:
            self.conn.execute("ALTER TABLE vacancies ADD COLUMN paid_until TEXT")
        if cols and "updated_at" not in cols:
            self.conn.execute("ALTER TABLE vacancies ADD COLUMN updated_at TEXT")
            # backfill: обновление = создание, пока правок не было
            self.conn.execute(
                "UPDATE vacancies SET updated_at = created_at WHERE updated_at IS NULL")
        self.conn.commit()
        # старая боевая схема: CHECK не допускает 'archived' — пересобираем таблицу
        ddl = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vacancies'"
        ).fetchone()
        if ddl and "archived" not in (ddl["sql"] or ""):
            self._rebuild_vacancies_table()
        flags_cols = [r["name"] for r in
                      self.conn.execute("PRAGMA table_info(user_flags)")]
        if flags_cols and "audit_teaser_used" not in flags_cols:
            self.conn.execute(
                "ALTER TABLE user_flags ADD COLUMN audit_teaser_used "
                "INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version) VALUES (3)")
        self.conn.commit()

    def _rebuild_vacancies_table(self) -> None:
        """Пересборка vacancies с CHECK включая 'archived' (сохраняя данные)."""
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("DROP TABLE IF EXISTS vacancies_mig")
        self.conn.execute("""
            CREATE TABLE vacancies_mig (
                id INTEGER PRIMARY KEY,
                employer_id INTEGER NOT NULL REFERENCES employers(user_id) ON DELETE CASCADE,
                data_json TEXT NOT NULL,
                norm_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'active', 'paused', 'closed', 'archived')),
                trust_index REAL NOT NULL DEFAULT 0.7,
                moderation TEXT NOT NULL DEFAULT 'pending',
                paid_until TEXT,
                updated_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self.conn.execute("""
            INSERT INTO vacancies_mig (id, employer_id, data_json, norm_json, status,
                trust_index, moderation, paid_until, updated_at, created_at)
            SELECT id, employer_id, data_json, norm_json, status, trust_index,
                   moderation, paid_until, updated_at, created_at FROM vacancies
        """)
        self.conn.execute("DROP TABLE vacancies")
        self.conn.execute("ALTER TABLE vacancies_mig RENAME TO vacancies")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vacancies_status ON vacancies(status)")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.commit()

    def _now(self) -> str:
        return self.conn.execute("SELECT datetime('now')").fetchone()[0]

    # ------------------------------ кандидаты (для работодателя) --------
    def list_candidate_profiles(self) -> List[Dict[str, Any]]:
        """Все профили кандидатов: [{user_id, tg_id, profile}]."""
        rows = self.conn.execute(
            "SELECT cp.user_id AS uid, u.tg_id AS tg, cp.data_json AS data "
            "FROM candidate_profiles cp JOIN users u ON u.id = cp.user_id"
        ).fetchall()
        return [
            {"user_id": int(r["uid"]), "tg_id": int(r["tg"]),
             "profile": json.loads(r["data"])}
            for r in rows
        ]

    # ------------------------------ вакансии ------------------------------
    def insert_vacancy(
        self, employer_id: int, data: Dict[str, Any], status: str = "active",
        moderation: str = "approved",
    ) -> int:
        # работодатель мог не создать карточку компании — заводим минимальную
        self.conn.execute(
            "INSERT INTO employers (user_id, company_json) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (employer_id, json.dumps({"name": "", "auto": True}, ensure_ascii=False)),
        )
        cur = self.conn.execute(
            "INSERT INTO vacancies (employer_id, data_json, norm_json, status, moderation, "
            "updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (employer_id, json.dumps(data, ensure_ascii=False),
             json.dumps(data, ensure_ascii=False), status, moderation),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_vacancy_paid_until(self, vacancy_db_id: int, paid_until: str) -> None:
        self.conn.execute(
            "UPDATE vacancies SET paid_until = ? WHERE id = ?", (paid_until, vacancy_db_id))
        self.conn.commit()

    def list_active_vacancies(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, data_json, updated_at, created_at FROM vacancies WHERE status = 'active' "
            "AND moderation = 'approved' "
            "AND (paid_until IS NULL OR paid_until > datetime('now'))"
        ).fetchall()
        result = []
        for row in rows:
            data = json.loads(row["data_json"])
            data["db_id"] = int(row["id"])
            data["updated_at"] = row["updated_at"] or row["created_at"]
            data["created_at"] = row["created_at"]
            result.append(data)
        return result

    def get_vacancy(self, vacancy_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id, data_json FROM vacancies WHERE data_json LIKE ? LIMIT 1",
            (f'%"id": "{vacancy_id}"%',),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["data_json"])
        data["db_id"] = int(row["id"])
        return data

    def list_employer_vacancies(self, employer_user_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, data_json, status, updated_at, created_at, paid_until "
            "FROM vacancies WHERE employer_id = ? ORDER BY id DESC",
            (employer_user_id,),
        ).fetchall()
        result = []
        for row in rows:
            data = json.loads(row["data_json"])
            data["db_id"] = int(row["id"])
            data["status"] = row["status"]
            data["updated_at"] = row["updated_at"] or row["created_at"]
            data["created_at"] = row["created_at"]
            data["paid_until"] = row["paid_until"]
            result.append(data)
        return result

    def update_vacancy_trust(self, vacancy_db_id: int, delta: float) -> float:
        row = self.conn.execute(
            "SELECT trust_index FROM vacancies WHERE id = ?", (vacancy_db_id,)
        ).fetchone()
        if not row:
            return 0.0
        new_value = max(0.2, min(1.0, float(row["trust_index"]) + delta))
        self.conn.execute(
            "UPDATE vacancies SET trust_index = ? WHERE id = ?", (new_value, vacancy_db_id)
        )
        self.conn.commit()
        return new_value

    def get_employer_of_vacancy(self, vacancy_db_id: int) -> Optional[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT e.* FROM vacancies v JOIN employers e ON e.user_id = v.employer_id "
            "WHERE v.id = ?",
            (vacancy_db_id,),
        ).fetchone()
        return row

    def get_vacancy_by_db_id(self, vacancy_db_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id, data_json, status, trust_index FROM vacancies WHERE id = ?",
            (vacancy_db_id,),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["data_json"])
        data["db_id"] = int(row["id"])
        data["status"] = row["status"]
        data["trust_index"] = float(row["trust_index"])
        return data

    def get_employer_name(self, user_id: int) -> Optional[str]:
        """Название компании работодателя (для уведомлений кандидатам)."""
        row = self.conn.execute(
            "SELECT company_json FROM employers WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["company_json"]).get("name") or None
        except (json.JSONDecodeError, AttributeError):
            return None

    def get_candidate_by_user_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT u.tg_id AS tg, cp.data_json AS data FROM users u "
            "JOIN candidate_profiles cp ON cp.user_id = u.id WHERE u.id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {"tg_id": int(row["tg"]), "profile": json.loads(row["data"])}

    # ------------------------------ матчинги ------------------------------
    def save_match(
        self, candidate_id: int, vacancy_db_id: int, result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Сохраняет матч с логикой остановки цикла (feedback §8).

        Возвращает {frozen, iterations, score}.
        """
        prev = self.conn.execute(
            "SELECT score, iterations FROM matches WHERE candidate_id = ? AND vacancy_id = ?",
            (candidate_id, vacancy_db_id),
        ).fetchone()
        prev_score = float(prev["score"]) if prev else None
        prev_iters = int(prev["iterations"]) if prev else 0

        from .feedback import should_freeze
        frozen, iterations = should_freeze(prev_score, float(result["score"]), prev_iters)

        self.conn.execute(
            "INSERT INTO matches (candidate_id, vacancy_id, score, band, breakdown_json, "
            "blocked, block_reasons_json, iterations, frozen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(candidate_id, vacancy_id) DO UPDATE SET "
            "score = excluded.score, band = excluded.band, "
            "breakdown_json = excluded.breakdown_json, blocked = excluded.blocked, "
            "block_reasons_json = excluded.block_reasons_json, "
            "iterations = excluded.iterations, frozen = excluded.frozen, "
            "computed_at = datetime('now')",
            (candidate_id, vacancy_db_id, result["score"], result["band"],
             json.dumps(result, ensure_ascii=False), int(result["blocked"]),
             json.dumps(result.get("block_reasons", []), ensure_ascii=False),
             iterations, int(frozen)),
        )
        self.conn.commit()
        return {"frozen": frozen, "iterations": iterations, "score": result["score"]}

    def is_match_frozen(self, candidate_id: int, vacancy_db_id: int) -> bool:
        row = self.conn.execute(
            "SELECT frozen FROM matches WHERE candidate_id = ? AND vacancy_id = ?",
            (candidate_id, vacancy_db_id),
        ).fetchone()
        return bool(row and row["frozen"])

    def save_candidate_weights(self, user_id: int, weights: Dict[str, float]) -> None:
        self.conn.execute(
            "UPDATE candidate_profiles SET weights_json = ? WHERE user_id = ?",
            (json.dumps(weights, ensure_ascii=False), user_id),
        )
        self.conn.commit()

    def get_candidate_weights(self, user_id: int) -> Optional[Dict[str, float]]:
        row = self.conn.execute(
            "SELECT weights_json FROM candidate_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row or not row["weights_json"]:
            return None
        return json.loads(row["weights_json"])

    def bump_vacancy_epsilon(self, vacancy_db_id: int, delta: float) -> float:
        """Добавляет шум ε в карточку вакансии (события «без ответа»)."""
        row = self.conn.execute(
            "SELECT data_json FROM vacancies WHERE id = ?", (vacancy_db_id,)
        ).fetchone()
        if not row:
            return 0.0
        data = json.loads(row["data_json"])
        data["epsilon_extra"] = clamp01f(float(data.get("epsilon_extra", 0.0)) + delta)
        self.conn.execute(
            "UPDATE vacancies SET data_json = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), vacancy_db_id),
        )
        self.conn.commit()
        return float(data["epsilon_extra"])

    def get_vacancy_by_code(self, vacancy_code: str) -> Optional[Dict[str, Any]]:
        """Поиск по КОДУ вакансии из пользовательского ввода.

        % и _ экранируются: код вида «S0%» не должен матчить чужие записи
        (LIKE-инъекция через /dislike и /respond)."""
        safe = (vacancy_code.replace("\\", "\\\\")
                .replace("%", "\\%").replace("_", "\\_"))
        row = self.conn.execute(
            "SELECT id, data_json FROM vacancies WHERE data_json LIKE ? "
            "ESCAPE '\\' LIMIT 1",
            (f'%"id": "{safe}"%',),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["data_json"])
        data["db_id"] = int(row["id"])
        return data

    def list_matches(self, candidate_id: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM matches WHERE candidate_id = ? ORDER BY score DESC",
            (candidate_id,),
        ).fetchall()

    # ------------------------------ seed ----------------------------------
    def seed_vacancies(self, seed_path: Path, employer_tg_id: int = 0) -> int:
        """Загружает стартовый набор вакансий от служебного работодателя."""
        user_id = self.upsert_user(employer_tg_id, "employer")
        self.conn.execute(
            "INSERT INTO employers (user_id, company_json, verified, trust_index) "
            "VALUES (?, ?, 1, 0.9) ON CONFLICT(user_id) DO UPDATE SET verified = 1",
            (user_id, json.dumps({"name": "Демо-работодатель", "seed": True},
                                 ensure_ascii=False)),
        )
        self.conn.commit()
        count = 0
        for vac in json.loads(seed_path.read_text(encoding="utf-8")):
            self.insert_vacancy(user_id, vac)
            count += 1
        return count

    # ------------------------------ фидбек --------------------------------
    def log_feedback(
        self, user_id: int, kind: str, candidate_id: Optional[int] = None,
        vacancy_id: Optional[int] = None, payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO feedback_events (user_id, match_candidate_id, match_vacancy_id, "
            "kind, payload_json) VALUES (?, ?, ?, ?, ?)",
            (user_id, candidate_id, vacancy_id, kind,
             json.dumps(payload or {}, ensure_ascii=False)),
        )
        self.conn.commit()

    def count_feedback(self, vacancy_db_id: int, kind: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM feedback_events "
            "WHERE match_vacancy_id = ? AND kind = ?",
            (vacancy_db_id, kind),
        ).fetchone()
        return int(row["c"])

    def daily_count(self, user_id: int, kind: str) -> int:
        """Сколько событий данного типа у пользователя сегодня (квоты)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM feedback_events "
            "WHERE user_id = ? AND kind = ? AND created_at >= date('now')",
            (user_id, kind),
        ).fetchone()
        return int(row["c"])

    # ------------------------------ реферальная программа ------------------
    def get_or_create_referral_code(self, tg_id: int, length: int = 8) -> str:
        """Стабильный код пользователя (создаётся при первом обращении)."""
        import secrets
        import string
        row = self.conn.execute(
            "SELECT code FROM referral_codes WHERE tg_id = ?", (tg_id,)).fetchone()
        if row:
            return row["code"]
        alphabet = string.ascii_lowercase + string.digits
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(length))
            exists = self.conn.execute(
                "SELECT 1 FROM referral_codes WHERE code = ?", (code,)).fetchone()
            if not exists:
                break
        self.conn.execute(
            "INSERT INTO referral_codes (tg_id, code) VALUES (?, ?)", (tg_id, code))
        self.conn.commit()
        return code

    def get_tg_by_referral_code(self, code: str):
        row = self.conn.execute(
            "SELECT tg_id FROM referral_codes WHERE code = ?", (code,)).fetchone()
        return int(row["tg_id"]) if row else None

    def attach_referrer(self, referred_tg_id: int, code: str) -> str:
        """Привязка рефerrer→referred при первом /start по ссылке.

        Возврат: 'bound' | 'self' | 'already' | 'not_new' | 'unknown_code'.
        Реферал — аккаунт, впервые увиденный системой: запись о привязке
        создаётся только до создания профиля/истории.
        """
        referrer_tg = self.get_tg_by_referral_code(code)
        if referrer_tg is None:
            return "unknown_code"
        if referrer_tg == referred_tg_id:
            return "self"
        existing = self.conn.execute(
            "SELECT 1 FROM referrals WHERE referred_tg_id = ?", (referred_tg_id,)
        ).fetchone()
        if existing:
            return "already"
        # «впервые увиденный»: нет профиля и нет прошлой активности (фидбек)
        has_profile = self.conn.execute(
            "SELECT 1 FROM candidate_profiles WHERE user_id = ?", (referred_tg_id,)
        ).fetchone()
        has_history = self.conn.execute(
            "SELECT 1 FROM feedback_events WHERE user_id = ? LIMIT 1", (referred_tg_id,)
        ).fetchone()
        if has_profile or has_history:
            return "not_new"
        self.conn.execute(
            "INSERT INTO referrals (referrer_tg_id, referred_tg_id) VALUES (?, ?)",
            (referrer_tg, referred_tg_id))
        self.conn.commit()
        return "bound"

    def credit_referral_onboarding(self, referred_tg_id: int, reward_days: int,
                                    max_per_month: int) -> Dict[str, Any]:
        """Начисление награды за завершённый онбординг referred.

        Возвращает {'status': ..., 'referrer_tg': ...}: обоим +reward_days Plus
        (рефerrerу — если месячный лимит не исчерпан).
        """
        row = self.conn.execute(
            "SELECT id, referrer_tg_id, status FROM referrals WHERE referred_tg_id = ?",
            (referred_tg_id,)).fetchone()
        if not row:
            return {"status": "no_referral"}
        if row["status"] == "credited":
            return {"status": "already_credited", "referrer_tg": row["referrer_tg_id"]}
        month_prefix = self._today()[:7]  # YYYY-MM
        month_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_tg_id = ? "
            "AND status = 'credited' AND credited_at LIKE ?",
            (row["referrer_tg_id"], month_prefix + "%")).fetchone()["c"]
        referrer_within_limit = month_count < max_per_month
        # новичку — всегда
        self.grant_subscription(referred_tg_id, "plus", reward_days)
        if referrer_within_limit:
            self.grant_subscription(row["referrer_tg_id"], "plus", reward_days)
        self.conn.execute(
            "UPDATE referrals SET status = 'credited', credited_at = datetime('now') "
            "WHERE id = ?", (row["id"],))
        self.conn.commit()
        return {"status": "credited",
                "referrer_tg": row["referrer_tg_id"],
                "referrer_rewarded": referrer_within_limit}

    def referral_stats(self, tg_id: int) -> Dict[str, int]:
        invited = self.conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_tg_id = ?", (tg_id,)
        ).fetchone()["c"]
        credited = self.conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_tg_id = ? "
            "AND status = 'credited'", (tg_id,)).fetchone()["c"]
        month_prefix = self._today()[:7]
        month_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_tg_id = ? "
            "AND status = 'credited' AND credited_at LIKE ?",
            (tg_id, month_prefix + "%")).fetchone()["c"]
        return {"invited": invited, "credited": credited,
                "month_used": month_count}

    def refstats_global(self) -> Dict[str, Any]:
        total = self.conn.execute("SELECT COUNT(*) AS c FROM referrals").fetchone()["c"]
        credited = self.conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE status = 'credited'").fetchone()["c"]
        top = self.conn.execute(
            "SELECT r.referrer_tg_id AS tg, COUNT(*) AS c FROM referrals r "
            "WHERE r.status = 'credited' GROUP BY r.referrer_tg_id "
            "ORDER BY c DESC LIMIT 10").fetchall()
        starts = self.conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role = 'candidate'").fetchone()["c"]
        return {"total": total, "credited": credited,
                "top": [(int(r["tg"]), int(r["c"])) for r in top],
                "candidates": starts,
                "conversion": round(credited / starts, 3) if starts else 0.0}

    # ------------------------------ свежесть вакансий ----------------------
    def vacancy_age_days(self, vacancy_row: Dict[str, Any]) -> int:
        """Возраст вакансии в днях по updated_at (или created_at)."""
        stamp = vacancy_row.get("updated_at") or vacancy_row.get("created_at") \
            or self._now()
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        try:
            dt = datetime.strptime(str(stamp)[:19], fmt)
        except ValueError:
            return 0
        now = datetime.strptime(self._now(), fmt)
        return max(0, (now - dt).days)

    def touch_vacancy(self, vacancy_db_id: int) -> None:
        """Обновление вакансии работодателем — updated_at = сейчас."""
        self.conn.execute(
            "UPDATE vacancies SET updated_at = datetime('now') WHERE id = ?",
            (vacancy_db_id,))
        self.conn.execute(
            "INSERT INTO vacancy_events (vacancy_db_id, kind) VALUES (?, 'touch')",
            (vacancy_db_id,))
        self.conn.commit()

    def reactivate_vacancy(self, vacancy_db_id: int, cooldown_days: int,
                           is_paid: bool = False) -> Tuple[bool, str]:
        """Реактивация архивной вакансии; бесплатной — раз в cooldown дней."""
        if not is_paid:
            recent = self.conn.execute(
                "SELECT 1 FROM vacancy_events WHERE vacancy_db_id = ? "
                "AND kind = 'reactivate' AND created_at > datetime('now', ?) LIMIT 1",
                (vacancy_db_id, "-" + str(cooldown_days) + " days")).fetchone()
            if recent:
                return False, "cooldown"
        cur = self.conn.execute(
            "UPDATE vacancies SET status = 'active', updated_at = datetime('now') "
            "WHERE id = ? AND status = 'archived'", (vacancy_db_id,))
        if cur.rowcount == 0:
            return False, "not_archived"
        self.conn.execute(
            "INSERT INTO vacancy_events (vacancy_db_id, kind) VALUES (?, 'reactivate')",
            (vacancy_db_id,))
        self.conn.commit()
        return True, "ok"

    def archive_stale_vacancies(self, auto_archive_days: int) -> int:
        """>N дней без обновления — авто-архивация (уходит из /match)."""
        cur = self.conn.execute(
            "UPDATE vacancies SET status = 'archived' "
            "WHERE status = 'active' AND COALESCE(updated_at, created_at) < "
            "datetime('now', ?)", ("-" + str(auto_archive_days) + " days",))
        self.conn.commit()
        return cur.rowcount

    def dedup_imported_vacancies(self) -> int:
        """Дедупликация импортированных: (название+работодатель+город) —
        оставляем последнюю по дате, остальные в архив. Возврат: заархивировано."""
        rows = self.conn.execute(
            "SELECT v.id, v.data_json, COALESCE(v.updated_at, v.created_at) AS stamp "
            "FROM vacancies v WHERE v.status IN ('active', 'archived') "
            "AND v.data_json LIKE '%\"source\": %'").fetchall()
        import json as _json
        groups: Dict[tuple, list] = {}
        for r in rows:
            d = _json.loads(r["data_json"])
            key = (str(d.get("title", "")).strip().lower(),
                   str(d.get("employer_name", "")).strip().lower(),
                   str((d.get("location") or {}).get("city", "")).strip().lower())
            groups.setdefault(key, []).append((r["stamp"] or "", int(r["id"])))
        archived = 0
        for key, items in groups.items():
            if len(items) < 2:
                continue
            items.sort(reverse=True)  # последняя первой
            for _stamp, vid in items[1:]:
                self.conn.execute(
                    "UPDATE vacancies SET status = 'archived' WHERE id = ? "
                    "AND status = 'active'", (vid,))
                archived += 1
        self.conn.commit()
        return archived

    def freshness_stats(self) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT id, status, COALESCE(updated_at, created_at) AS stamp "
            "FROM vacancies").fetchall()
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        now = datetime.strptime(self._now(), fmt)
        buckets = {"0-7": 0, "8-30": 0, "31-45": 0, ">45": 0}
        active = archived = 0
        for r in rows:
            if r["status"] == "active":
                active += 1
            elif r["status"] == "archived":
                archived += 1
            if r["status"] != "active":
                continue
            try:
                age = (now - datetime.strptime(r["stamp"][:19], fmt)).days
            except (ValueError, TypeError):
                age = 0
            if age <= 7:
                buckets["0-7"] += 1
            elif age <= 30:
                buckets["8-30"] += 1
            elif age <= 45:
                buckets["31-45"] += 1
            else:
                buckets[">45"] += 1
        return {"total": len(rows), "active": active, "archived": archived,
                "freshness": buckets}

    def vacancies_archive_reminders(self, auto_archive_days: int, warn_days: int = 7
                                    ) -> List[Dict[str, Any]]:
        """Вакансии, уйдущие в архив в пределах warn_days (напоминание раз/сутки)."""
        threshold = auto_archive_days - warn_days
        rows = self.conn.execute(
            "SELECT v.id, v.employer_id, v.data_json, "
            "COALESCE(v.updated_at, v.created_at) AS stamp "
            "FROM vacancies v WHERE v.status = 'active' "
            "AND COALESCE(v.updated_at, v.created_at) < datetime('now', ?)",
            ("-" + str(threshold) + " days",)).fetchall()
        import json as _json
        result = []
        today = self._today()
        for r in rows:
            # напоминание раз в сутки: vacancy_events kind='remind' за сегодня
            reminded = self.conn.execute(
                "SELECT 1 FROM vacancy_events WHERE vacancy_db_id = ? "
                "AND kind = 'remind' AND created_at >= ? LIMIT 1",
                (r["id"], today + " 00:00:00")).fetchone()
            if reminded:
                continue
            d = _json.loads(r["data_json"])
            result.append({"db_id": int(r["id"]), "employer_id": int(r["employer_id"]),
                           "title": d.get("title", "?")})
            self.conn.execute(
                "INSERT INTO vacancy_events (vacancy_db_id, kind) VALUES (?, 'remind')",
                (r["id"],))
        self.conn.commit()
        return result

    # ------------------------------ покупки -------------------------------
    def next_invoice_id(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(robokassa_inv_id), 1000) + 1 AS n FROM purchases"
        ).fetchone()
        return int(row["n"])

    def create_purchase(self, tg_id: int, sku_id: str, amount_kop: int,
                        invoice_id: int, payload: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO purchases (tg_id, sku_id, amount_kop, robokassa_inv_id, "
            "status, payload) VALUES (?, ?, ?, ?, 'pending', ?)",
            (tg_id, sku_id, amount_kop, invoice_id, payload),
        )
        self.conn.commit()

    def complete_purchase(self, invoice_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Помечает счёт оплаченным (идемпотентно). Возвращает (ок, msg, счёт)."""
        row = self.conn.execute(
            "SELECT * FROM purchases WHERE robokassa_inv_id = ?", (invoice_id,)
        ).fetchone()
        if not row:
            return False, "счёт не найден", None
        if row["status"] == "paid":
            return True, "уже оплачен", dict(row)
        self.conn.execute(
            "UPDATE purchases SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
            (row["id"],))
        self.conn.commit()
        fresh = self.conn.execute(
            "SELECT * FROM purchases WHERE id = ?", (row["id"],)).fetchone()
        return True, "оплачено", dict(fresh)

    def purchase_status(self, invoice_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM purchases WHERE robokassa_inv_id = ?", (invoice_id,)
        ).fetchone()
        return dict(row) if row else None

    def paid_undelivered(self, tg_id: int, sku_id: str) -> Optional[Dict[str, Any]]:
        """Оплаченная, но ещё не оказанная oneoff-услуга (why_report)."""
        row = self.conn.execute(
            "SELECT * FROM purchases WHERE tg_id = ? AND sku_id = ? "
            "AND status = 'paid' AND delivered = 0 ORDER BY id LIMIT 1",
            (tg_id, sku_id)).fetchone()
        return dict(row) if row else None

    def mark_delivered(self, purchase_id: int) -> None:
        self.conn.execute(
            "UPDATE purchases SET delivered = 1 WHERE id = ?", (purchase_id,))
        self.conn.commit()

    # ------------------------------ entitlements ---------------------------
    def add_entitlements(self, tg_id: int, kind: str, quantity: int) -> int:
        self.conn.execute(
            "INSERT INTO entitlements (tg_id, kind, quantity) VALUES (?, ?, ?) "
            "ON CONFLICT(tg_id, kind) DO UPDATE SET quantity = quantity + excluded.quantity",
            (tg_id, kind, quantity))
        self.conn.commit()
        return self.get_entitlements(tg_id, kind)

    def get_entitlements(self, tg_id: int, kind: str) -> int:
        row = self.conn.execute(
            "SELECT quantity FROM entitlements WHERE tg_id = ? AND kind = ?",
            (tg_id, kind)).fetchone()
        return int(row["quantity"]) if row else 0

    def consume_entitlement(self, tg_id: int, kind: str, n: int = 1) -> bool:
        cur = self.conn.execute(
            "UPDATE entitlements SET quantity = quantity - ? "
            "WHERE tg_id = ? AND kind = ? AND quantity >= ?",
            (n, tg_id, kind, n))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------ подписки -------------------------------
    def grant_subscription(self, tg_id: int, plan: str, days: int) -> str:
        """Выдаёт/продлевает подписку (дни суммируются с активной). → until."""
        from datetime import datetime, timedelta
        fmt = "%Y-%m-%d %H:%M:%S"
        row = self.conn.execute(
            "SELECT active_until FROM subscriptions WHERE tg_id = ? AND plan = ?",
            (tg_id, plan)).fetchone()
        now = self._now()
        base = (datetime.strptime(row["active_until"], fmt)
                if row and row["active_until"] > now else datetime.strptime(now, fmt))
        until = (base + timedelta(days=days)).strftime(fmt)
        self.conn.execute(
            "INSERT INTO subscriptions (tg_id, plan, active_until, last_paid_at, "
            "pro_period_invites_used) VALUES (?, ?, ?, datetime('now'), 0) "
            "ON CONFLICT(tg_id, plan) DO UPDATE SET active_until = excluded.active_until, "
            "last_paid_at = datetime('now')",
            (tg_id, plan, until))
        if plan in ("pro", "business"):
            self.conn.execute(
                "UPDATE subscriptions SET pro_period_invites_used = 0 "
                "WHERE tg_id = ? AND plan = ?", (tg_id, plan))
        self.conn.commit()
        return until

    def get_subscription(self, tg_id: int, plan: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM subscriptions WHERE tg_id = ? AND plan = ? "
            "AND active_until > datetime('now')", (tg_id, plan)).fetchone()
        return dict(row) if row else None

    def is_subscribed(self, tg_id: int, plan: str) -> bool:
        return self.get_subscription(tg_id, plan) is not None

    def subscription_reminder_due(self, tg_id: int, remind_days: int = 3
                                  ) -> Optional[Dict[str, Any]]:
        """Активная подписка, истекающая ≤ remind_days; напоминание раз в сутки."""
        rows = self.conn.execute(
            "SELECT * FROM subscriptions WHERE tg_id = ? AND active_until > datetime('now') "
            "AND active_until <= datetime('now', ?)",
            (tg_id, "+" + str(remind_days) + " days")).fetchall()
        today = self._now()[:10]
        for r in rows:
            if not r["reminded_at"] or r["reminded_at"][:10] != today:
                self.conn.execute(
                    "UPDATE subscriptions SET reminded_at = datetime('now') "
                    "WHERE tg_id = ? AND plan = ?", (tg_id, r["plan"]))
                self.conn.commit()
                return dict(r)
        return None

    def pro_invite_quota_left(self, tg_id: int, quota: int) -> int:
        """Остаток месячной квоты приглашений Pro/Business."""
        sub = (self.get_subscription(tg_id, "pro") or self.get_subscription(tg_id, "business"))
        if not sub:
            return 0
        return max(0, quota - int(sub["pro_period_invites_used"]))

    def pro_invite_used(self, tg_id: int) -> None:
        plan = "pro" if self.get_subscription(tg_id, "pro") else "business"
        if plan:
            self.conn.execute(
                "UPDATE subscriptions SET pro_period_invites_used = pro_period_invites_used + 1 "
                "WHERE tg_id = ? AND plan = ?", (tg_id, plan))
            self.conn.commit()

    # ------------------------------ флаги и счётчики -----------------------
    def get_flag(self, tg_id: int, name: str) -> int:
        row = self.conn.execute(
            "SELECT %s FROM user_flags WHERE tg_id = ?" % name, (tg_id,)).fetchone()
        return int(row[name]) if row else 0

    def set_flag(self, tg_id: int, name: str, value: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO user_flags (tg_id) VALUES (?)", (tg_id,))
        self.conn.execute(
            "UPDATE user_flags SET %s = ? WHERE tg_id = ?" % name, (value, tg_id))
        self.conn.commit()

    def _today(self) -> str:
        return self._now()[:10]

    def get_usage(self, tg_id: int, day: Optional[str] = None) -> Dict[str, int]:
        day = day or self._today()
        row = self.conn.execute(
            "SELECT * FROM daily_usage WHERE tg_id = ? AND day = ?", (tg_id, day)).fetchone()
        if row:
            return {k: int(row[k]) for k in
                    ("matches_shown", "alerts_sent", "broadcasts_received")}
        return {"matches_shown": 0, "alerts_sent": 0, "broadcasts_received": 0}

    def bump_usage(self, tg_id: int, field: str, n: int = 1) -> int:
        assert field in ("matches_shown", "alerts_sent", "broadcasts_received")
        self.conn.execute(
            "INSERT INTO daily_usage (tg_id, day, %s) VALUES (?, ?, ?) "
            "ON CONFLICT(tg_id, day) DO UPDATE SET %s = %s + ?" % (field, field, field),
            (tg_id, self._today(), n, n))
        self.conn.commit()
        return self.get_usage(tg_id)[field]

