import os
import sqlite3
from pathlib import Path
from typing import Iterable


DB_PATH = Path(os.getenv("DB_PATH", "/app/data/bot.db"))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create the SQLite database and tables if they do not exist."""
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                vk_id INTEGER PRIMARY KEY,
                level TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

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
            "CREATE INDEX IF NOT EXISTS idx_mistakes_user ON mistakes(vk_id, level, topic)"
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
            SELECT
                COUNT(*) AS attempts,
                ROUND(AVG(percent), 1) AS avg_percent
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
