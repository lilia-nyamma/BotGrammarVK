import os
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence


DB_PATH = Path(os.getenv("DB_PATH", "/app/data/bot.db"))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _exclude_clause(column: str, exclude_ids: Sequence[int]) -> tuple[str, list[int]]:
    ids = [int(value) for value in exclude_ids]
    if not ids:
        return "", []
    placeholders = ",".join("?" for _ in ids)
    return f" AND {column} NOT IN ({placeholders})", ids


def init_db() -> None:
    """Create the SQLite database and migrate older versions safely."""
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                vk_id INTEGER PRIMARY KEY,
                level TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Migration for databases created before names were added.
        user_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "first_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
        if "last_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_name TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vk_id INTEGER NOT NULL,
                level TEXT NOT NULL,
                topic TEXT NOT NULL,
                exercise INTEGER NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                percent INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (vk_id) REFERENCES users(vk_id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mistakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                vk_id INTEGER NOT NULL,
                level TEXT NOT NULL,
                topic TEXT NOT NULL,
                exercise INTEGER NOT NULL,
                question_number INTEGER NOT NULL,
                user_answer TEXT NOT NULL,
                correct_answer TEXT NOT NULL,
                tip TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
                FOREIGN KEY (vk_id) REFERENCES users(vk_id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(vk_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_topic ON attempts(vk_id, level, topic, exercise)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_created ON attempts(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mistakes_user ON mistakes(vk_id, level, topic)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mistakes_created ON mistakes(created_at)"
        )


def touch_user(vk_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (vk_id)
            VALUES (?)
            ON CONFLICT(vk_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP
            """,
            (vk_id,),
        )


def save_user_profile(vk_id: int, first_name: str, last_name: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (vk_id, first_name, last_name)
            VALUES (?, ?, ?)
            ON CONFLICT(vk_id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_seen = CURRENT_TIMESTAMP
            """,
            (vk_id, first_name, last_name),
        )


def get_user_profile(vk_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT vk_id, level, first_name, last_name, created_at, last_seen
            FROM users
            WHERE vk_id = ?
            """,
            (vk_id,),
        ).fetchone()
    return dict(row) if row else None


def save_user_level(vk_id: int, level: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (vk_id, level)
            VALUES (?, ?)
            ON CONFLICT(vk_id) DO UPDATE SET
                level = excluded.level,
                last_seen = CURRENT_TIMESTAMP
            """,
            (vk_id, level),
        )


def get_user_level(vk_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT level FROM users WHERE vk_id = ?",
            (vk_id,),
        ).fetchone()
    return row["level"] if row and row["level"] else None


def get_previous_attempt(
    vk_id: int,
    level: str,
    topic: str,
    exercise: int,
) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT score, total, percent, created_at
            FROM attempts
            WHERE vk_id = ? AND level = ? AND topic = ? AND exercise = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (vk_id, level, topic, exercise),
        ).fetchone()
    return dict(row) if row else None


def get_best_score(
    vk_id: int,
    level: str,
    topic: str,
    exercise: int,
) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT MAX(score) AS best_score
            FROM attempts
            WHERE vk_id = ? AND level = ? AND topic = ? AND exercise = ?
            """,
            (vk_id, level, topic, exercise),
        ).fetchone()
    if not row or row["best_score"] is None:
        return None
    return int(row["best_score"])


def save_attempt(
    vk_id: int,
    level: str,
    topic: str,
    exercise: int,
    score: int,
    total: int,
    mistakes: Iterable[dict],
) -> int:
    """Save an exercise attempt and its mistakes. Returns attempt id."""
    percent = round(score / total * 100) if total else 0

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (vk_id, level)
            VALUES (?, ?)
            ON CONFLICT(vk_id) DO UPDATE SET
                level = excluded.level,
                last_seen = CURRENT_TIMESTAMP
            """,
            (vk_id, level),
        )

        cursor = conn.execute(
            """
            INSERT INTO attempts (
                vk_id, level, topic, exercise, score, total, percent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (vk_id, level, topic, exercise, score, total, percent),
        )
        attempt_id = int(cursor.lastrowid)

        rows = [
            (
                attempt_id,
                vk_id,
                level,
                topic,
                exercise,
                int(item["question_number"]),
                str(item["user_answer"]),
                str(item["correct_answer"]),
                str(item.get("tip", "")),
            )
            for item in mistakes
        ]

        if rows:
            conn.executemany(
                """
                INSERT INTO mistakes (
                    attempt_id,
                    vk_id,
                    level,
                    topic,
                    exercise,
                    question_number,
                    user_answer,
                    correct_answer,
                    tip
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    return attempt_id


def get_progress(vk_id: int) -> dict:
    with _connect() as conn:
        overall = conn.execute(
            """
            SELECT COUNT(*) AS attempts, ROUND(AVG(percent), 1) AS avg_percent
            FROM attempts
            WHERE vk_id = ?
            """,
            (vk_id,),
        ).fetchone()

        by_level = conn.execute(
            """
            SELECT
                level,
                COUNT(*) AS attempts,
                COUNT(DISTINCT topic || '|' || exercise) AS unique_exercises,
                ROUND(AVG(percent), 1) AS avg_percent,
                MAX(percent) AS best_percent
            FROM attempts
            WHERE vk_id = ?
            GROUP BY level
            ORDER BY level
            """,
            (vk_id,),
        ).fetchall()

        topic_stats = conn.execute(
            """
            SELECT
                level,
                topic,
                COUNT(*) AS attempts,
                ROUND(AVG(percent), 1) AS avg_percent,
                MAX(percent) AS best_percent
            FROM attempts
            WHERE vk_id = ?
            GROUP BY level, topic
            ORDER BY avg_percent DESC, attempts DESC
            """,
            (vk_id,),
        ).fetchall()

    return {
        "attempts": int(overall["attempts"] or 0),
        "avg_percent": float(overall["avg_percent"] or 0),
        "by_level": [dict(row) for row in by_level],
        "topic_stats": [dict(row) for row in topic_stats],
    }


def get_frequent_mistakes(vk_id: int, limit: int = 5) -> dict:
    with _connect() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS total FROM mistakes WHERE vk_id = ?",
            (vk_id,),
        ).fetchone()

        by_topic = conn.execute(
            """
            SELECT level, topic, COUNT(*) AS error_count
            FROM mistakes
            WHERE vk_id = ?
            GROUP BY level, topic
            ORDER BY error_count DESC, level, topic
            LIMIT ?
            """,
            (vk_id, limit),
        ).fetchall()

        common_tips = conn.execute(
            """
            SELECT level, topic, tip, COUNT(*) AS error_count
            FROM mistakes
            WHERE vk_id = ? AND tip IS NOT NULL AND TRIM(tip) <> ''
            GROUP BY level, topic, tip
            HAVING COUNT(*) >= 2
            ORDER BY error_count DESC
            LIMIT ?
            """,
            (vk_id, limit),
        ).fetchall()

    return {
        "total": int(total_row["total"] or 0),
        "by_topic": [dict(row) for row in by_topic],
        "common_tips": [dict(row) for row in common_tips],
    }


# -------------------- Admin queries --------------------

def get_admin_student_count(exclude_ids: Sequence[int] = ()) -> int:
    clause, params = _exclude_clause("vk_id", exclude_ids)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS total FROM users WHERE 1=1{clause}",
            params,
        ).fetchone()
    return int(row["total"] or 0)


def get_admin_students(
    limit: int = 6,
    offset: int = 0,
    exclude_ids: Sequence[int] = (),
) -> list[dict]:
    clause, params = _exclude_clause("u.vk_id", exclude_ids)
    sql = f"""
        SELECT
            u.vk_id,
            u.first_name,
            u.last_name,
            u.level,
            u.created_at,
            u.last_seen,
            COUNT(a.id) AS attempts,
            ROUND(AVG(a.percent), 1) AS avg_percent,
            MAX(a.created_at) AS last_attempt
        FROM users u
        LEFT JOIN attempts a ON a.vk_id = u.vk_id
        WHERE 1=1{clause}
        GROUP BY u.vk_id
        ORDER BY COALESCE(MAX(a.created_at), u.last_seen) DESC
        LIMIT ? OFFSET ?
    """
    with _connect() as conn:
        rows = conn.execute(sql, [*params, int(limit), int(offset)]).fetchall()
    return [dict(row) for row in rows]


def get_admin_overall_stats(exclude_ids: Sequence[int] = ()) -> dict:
    u_clause, u_params = _exclude_clause("vk_id", exclude_ids)
    a_clause, a_params = _exclude_clause("vk_id", exclude_ids)
    m_clause, m_params = _exclude_clause("vk_id", exclude_ids)

    with _connect() as conn:
        users_row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS students,
                SUM(CASE WHEN last_seen >= datetime('now', '-7 days') THEN 1 ELSE 0 END) AS active_7d
            FROM users
            WHERE 1=1{u_clause}
            """,
            u_params,
        ).fetchone()

        attempts_row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS attempts,
                ROUND(AVG(percent), 1) AS avg_percent,
                SUM(CASE WHEN created_at >= datetime('now', '-1 day') THEN 1 ELSE 0 END) AS attempts_24h
            FROM attempts
            WHERE 1=1{a_clause}
            """,
            a_params,
        ).fetchone()

        mistakes_row = conn.execute(
            f"SELECT COUNT(*) AS mistakes FROM mistakes WHERE 1=1{m_clause}",
            m_params,
        ).fetchone()

        level_rows = conn.execute(
            f"""
            SELECT level, COUNT(*) AS students
            FROM users
            WHERE level IS NOT NULL AND TRIM(level) <> ''{u_clause}
            GROUP BY level
            ORDER BY level
            """,
            u_params,
        ).fetchall()

    return {
        "students": int(users_row["students"] or 0),
        "active_7d": int(users_row["active_7d"] or 0),
        "attempts": int(attempts_row["attempts"] or 0),
        "attempts_24h": int(attempts_row["attempts_24h"] or 0),
        "avg_percent": float(attempts_row["avg_percent"] or 0),
        "mistakes": int(mistakes_row["mistakes"] or 0),
        "by_level": [dict(row) for row in level_rows],
    }


def get_admin_difficult_topics(
    limit: int = 10,
    exclude_ids: Sequence[int] = (),
) -> list[dict]:
    clause, params = _exclude_clause("vk_id", exclude_ids)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                level,
                topic,
                COUNT(*) AS attempts,
                COUNT(DISTINCT vk_id) AS students,
                ROUND(AVG(percent), 1) AS avg_percent
            FROM attempts
            WHERE 1=1{clause}
            GROUP BY level, topic
            ORDER BY avg_percent ASC, attempts DESC
            LIMIT ?
            """,
            [*params, int(limit)],
        ).fetchall()
    return [dict(row) for row in rows]


def get_admin_recent_attempts(
    limit: int = 12,
    exclude_ids: Sequence[int] = (),
) -> list[dict]:
    clause, params = _exclude_clause("a.vk_id", exclude_ids)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.id,
                a.vk_id,
                u.first_name,
                u.last_name,
                a.level,
                a.topic,
                a.exercise,
                a.score,
                a.total,
                a.percent,
                a.created_at
            FROM attempts a
            LEFT JOIN users u ON u.vk_id = a.vk_id
            WHERE 1=1{clause}
            ORDER BY a.id DESC
            LIMIT ?
            """,
            [*params, int(limit)],
        ).fetchall()
    return [dict(row) for row in rows]


def get_admin_student_summary(vk_id: int) -> dict:
    with _connect() as conn:
        user = conn.execute(
            """
            SELECT vk_id, first_name, last_name, level, created_at, last_seen
            FROM users
            WHERE vk_id = ?
            """,
            (vk_id,),
        ).fetchone()

        overall = conn.execute(
            """
            SELECT
                COUNT(*) AS attempts,
                ROUND(AVG(percent), 1) AS avg_percent,
                MAX(percent) AS best_percent,
                COUNT(DISTINCT level || '|' || topic) AS topics,
                COUNT(DISTINCT level || '|' || topic || '|' || exercise) AS unique_exercises,
                MAX(created_at) AS last_attempt
            FROM attempts
            WHERE vk_id = ?
            """,
            (vk_id,),
        ).fetchone()

        mistakes = conn.execute(
            "SELECT COUNT(*) AS total FROM mistakes WHERE vk_id = ?",
            (vk_id,),
        ).fetchone()

        by_level = conn.execute(
            """
            SELECT
                level,
                COUNT(*) AS attempts,
                COUNT(DISTINCT topic || '|' || exercise) AS unique_exercises,
                ROUND(AVG(percent), 1) AS avg_percent
            FROM attempts
            WHERE vk_id = ?
            GROUP BY level
            ORDER BY level
            """,
            (vk_id,),
        ).fetchall()

    return {
        "user": dict(user) if user else None,
        "attempts": int(overall["attempts"] or 0),
        "avg_percent": float(overall["avg_percent"] or 0),
        "best_percent": int(overall["best_percent"] or 0),
        "topics": int(overall["topics"] or 0),
        "unique_exercises": int(overall["unique_exercises"] or 0),
        "last_attempt": overall["last_attempt"],
        "mistakes": int(mistakes["total"] or 0),
        "by_level": [dict(row) for row in by_level],
    }


def get_admin_student_attempts(vk_id: int, limit: int = 12) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, level, topic, exercise, score, total, percent, created_at
            FROM attempts
            WHERE vk_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (vk_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def get_admin_student_topics(vk_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                level,
                topic,
                COUNT(*) AS attempts,
                COUNT(DISTINCT exercise) AS exercises,
                ROUND(AVG(percent), 1) AS avg_percent,
                MAX(percent) AS best_percent
            FROM attempts
            WHERE vk_id = ?
            GROUP BY level, topic
            ORDER BY level, avg_percent ASC, topic
            """,
            (vk_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_admin_student_mistakes(vk_id: int, limit: int = 10) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                m.id,
                m.attempt_id,
                m.level,
                m.topic,
                m.exercise,
                m.question_number,
                m.user_answer,
                m.correct_answer,
                m.tip,
                m.created_at
            FROM mistakes m
            WHERE m.vk_id = ?
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (vk_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]
