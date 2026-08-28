import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from vkbottle import Bot, Keyboard, Text
from vkbottle.bot import Message

from database import (
    get_admin_difficult_topics,
    get_admin_overall_stats,
    get_admin_recent_attempts,
    get_admin_student_attempts,
    get_admin_student_count,
    get_admin_student_mistakes,
    get_admin_student_summary,
    get_admin_student_topics,
    get_admin_students,
    get_best_score,
    get_frequent_mistakes,
    get_previous_attempt,
    get_progress,
    get_user_level,
    get_user_profile,
    init_db,
    save_attempt,
    save_user_level,
    save_user_profile,
    touch_user,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("grammar_bot")

TOKEN = os.getenv("VK_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задана переменная окружения VK_TOKEN")

bot = Bot(token=TOKEN)

# One ID is enough, but comma-separated IDs are also supported.
_raw_admin_ids = os.getenv("ADMIN_VK_IDS") or os.getenv("ADMIN_VK_ID", "")
ADMIN_VK_IDS = {
    int(value)
    for value in re.split(r"[\s,;]+", _raw_admin_ids.strip())
    if value.isdigit()
}

BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow")
try:
    LOCAL_TZ = ZoneInfo(BOT_TIMEZONE)
except Exception:
    LOCAL_TZ = timezone.utc

BASE_DIR = Path(__file__).resolve().parent
with open(BASE_DIR / "content.json", "r", encoding="utf-8") as f:
    CONTENT = json.load(f)

init_db()
logger.info("SQLite database initialized")

sessions = {}
admin_sessions = {}

QUESTIONS_PER_EXERCISE = 10

EXERCISE_LABELS = {
    "🟢 Practice 1": 0,
    "🟡 Practice 2": 1,
    "🔴 Challenge": 2,
}

STATS_LABELS = {"📊 Мой прогресс", "🔎 Частые ошибки"}


def new_session():
    return {
        "step": "level",
        "level": None,
        "topic": None,
        "exercise": None,
    }


def level_keyboard():
    kb = Keyboard(one_time=False)
    levels = list(CONTENT.keys())
    for i, level in enumerate(levels):
        if i and i % 3 == 0:
            kb.row()
        kb.add(Text(level))
    kb.row()
    kb.add(Text("📊 Мой прогресс"))
    kb.add(Text("🔎 Частые ошибки"))
    return kb.get_json()


def topic_keyboard(level):
    kb = Keyboard(one_time=False)
    topics = CONTENT[level]
    for i, topic_name in enumerate(topics.keys()):
        if i and i % 2 == 0:
            kb.row()
        kb.add(Text(topic_name))
    kb.row()
    kb.add(Text("📊 Мой прогресс"))
    kb.add(Text("🔎 Частые ошибки"))
    kb.row()
    kb.add(Text("🔄 Сменить уровень"))
    kb.add(Text("🏠 Главное меню"))
    return kb.get_json()


def exercise_keyboard():
    kb = Keyboard(one_time=False)
    kb.add(Text("🟢 Practice 1"))
    kb.add(Text("🟡 Practice 2"))
    kb.add(Text("🔴 Challenge"))
    kb.row()
    kb.add(Text("📚 Правило ещё раз"))
    kb.add(Text("⬅️ К темам"))
    kb.row()
    kb.add(Text("📊 Мой прогресс"))
    kb.add(Text("🔎 Частые ошибки"))
    kb.row()
    kb.add(Text("🏠 Главное меню"))
    return kb.get_json()


def keyboard_for_session(session):
    if session["step"] == "level" or not session.get("level"):
        return level_keyboard()
    if session["step"] == "topic":
        return topic_keyboard(session["level"])
    return exercise_keyboard()


def after_exercise_keyboard():
    return exercise_keyboard()


def format_exercise(exercise):
    questions = exercise["questions"][:QUESTIONS_PER_EXERCISE]
    total = len(questions)
    lines = [
        f"📝 {exercise['title']}",
        "",
        f"Ответь на {total} вопросов.",
        "Пришли ответы одной строкой, например:",
        "1A 2B 3C 4A 5B 6C 7A 8B 9C 10A",
        "Можно писать латиницей или кириллицей: A/А, B/В, C/С. Регистр не важен.",
        "",
    ]
    for i, item in enumerate(questions, start=1):
        lines.append(f"{i}. {item['question']}")
        for letter, option in zip(("A", "B", "C"), item["options"]):
            lines.append(f"   {letter}) {option}")
        lines.append("")
    return "\n".join(lines)


def parse_answers(text, expected_count=QUESTIONS_PER_EXERCISE):
    # Регистр не важен: a/b/c и A/B/C воспринимаются одинаково.
    # Также поддерживаем визуально похожие кириллические буквы:
    # А/а → A, В/в → B, С/с → C.
    normalized = (text or "").upper().strip().translate(
        str.maketrans({
            "А": "A",
            "В": "B",
            "С": "C",
        })
    )

    numbered = re.findall(
        r"(?:^|\s|,|;)(\d{1,2})\s*[-.:)]?\s*([ABC])(?=\s|,|;|$)",
        normalized,
    )
    if numbered:
        result = {}
        for num, letter in numbered:
            n = int(num)
            if 1 <= n <= expected_count:
                result[n] = letter
        if len(result) == expected_count:
            return [result[i] for i in range(1, expected_count + 1)]

    letters = re.findall(r"\b[ABC]\b", normalized)
    if len(letters) == expected_count:
        return letters

    compact = re.sub(r"[^ABC]", "", normalized)
    if len(compact) == expected_count:
        return list(compact)

    return None


def total_exercises_for_level(level):
    return sum(len(topic["exercises"]) for topic in CONTENT[level].values())


def format_progress(user_id):
    progress = get_progress(user_id)
    saved_level = get_user_level(user_id)

    if progress["attempts"] == 0:
        return (
            "📊 Твой прогресс\n\n"
            "Пока нет сохранённых результатов.\n"
            "Пройди любое упражнение — и статистика появится здесь автоматически."
        )

    lines = [
        "📊 Твой прогресс",
        "",
        f"Всего попыток: {progress['attempts']}",
        f"Средний результат: {progress['avg_percent']:.0f}%",
    ]

    if saved_level:
        lines.append(f"Текущий уровень: {saved_level}")

    if progress["by_level"]:
        lines.extend(["", "По уровням:"])
        for row in progress["by_level"]:
            level = row["level"]
            total = total_exercises_for_level(level) if level in CONTENT else "?"
            lines.append(
                f"• {level}: {row['unique_exercises']}/{total} тренировок, "
                f"средний результат {float(row['avg_percent'] or 0):.0f}%"
            )

    topic_stats = progress["topic_stats"]
    if topic_stats:
        best = topic_stats[:3]
        weakest = sorted(
            topic_stats,
            key=lambda item: (float(item["avg_percent"] or 0), -int(item["attempts"])),
        )[:3]

        lines.extend(["", "🏆 Лучшие темы:"])
        for row in best:
            lines.append(
                f"• {row['level']} · {row['topic']} — {float(row['avg_percent'] or 0):.0f}%"
            )

        if len(topic_stats) > 1:
            lines.extend(["", "🔁 Стоит повторить:"])
            for row in weakest:
                lines.append(
                    f"• {row['level']} · {row['topic']} — {float(row['avg_percent'] or 0):.0f}%"
                )

    return "\n".join(lines)


def format_frequent_mistakes(user_id):
    stats = get_frequent_mistakes(user_id)

    if stats["total"] == 0:
        return (
            "🔎 Частые ошибки\n\n"
            "Пока ошибок в базе нет. Отличный старт 🙂\n"
            "После выполненных упражнений я начну собирать статистику."
        )

    lines = [
        "🔎 Частые ошибки",
        "",
        f"Всего сохранено ошибок: {stats['total']}",
        "",
        "Темы, где ошибок больше всего:",
    ]

    for i, row in enumerate(stats["by_topic"], start=1):
        lines.append(
            f"{i}. {row['level']} · {row['topic']} — {row['error_count']}"
        )

    if stats["common_tips"]:
        lines.extend(["", "💡 Повторяющиеся правила:"])
        for row in stats["common_tips"][:3]:
            lines.append(
                f"• {row['tip']} ({row['error_count']} раза)"
            )

    return "\n".join(lines)



# -------------------- Teacher/admin panel --------------------

ADMIN_PAGE_SIZE = 6


def is_admin(user_id):
    return user_id in ADMIN_VK_IDS


def mask_vk_id(user_id):
    value = str(user_id)
    return f"***{value[-4:]}" if len(value) > 4 else "***"


def format_db_time(value):
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


def person_name(row):
    first = (row or {}).get("first_name") or ""
    last = (row or {}).get("last_name") or ""
    name = f"{first} {last}".strip()
    if name:
        return name
    vk_id = (row or {}).get("vk_id")
    return f"VK ID {vk_id}" if vk_id else "Ученик"


async def ensure_vk_profiles(rows):
    """Fetch missing VK names in one request and cache them in SQLite."""
    missing = []
    for row in rows:
        if row.get("vk_id") and not row.get("first_name"):
            missing.append(int(row["vk_id"]))

    if not missing:
        return rows

    try:
        profiles = await bot.api.users.get(user_ids=missing)
        by_id = {}
        for profile in profiles:
            vk_id = int(profile.id)
            first_name = str(profile.first_name or "")
            last_name = str(profile.last_name or "")
            save_user_profile(vk_id, first_name, last_name)
            by_id[vk_id] = (first_name, last_name)

        for row in rows:
            values = by_id.get(int(row.get("vk_id") or 0))
            if values:
                row["first_name"], row["last_name"] = values
    except Exception as exc:
        logger.warning("Could not load VK user names: %s", exc)

    return rows


async def ensure_single_profile(user_id):
    profile = get_user_profile(user_id)
    if profile and profile.get("first_name"):
        return

    try:
        result = await bot.api.users.get(user_ids=[user_id])
        if result:
            save_user_profile(
                user_id,
                str(result[0].first_name or ""),
                str(result[0].last_name or ""),
            )
    except Exception as exc:
        logger.debug("Could not sync VK profile for %s: %s", mask_vk_id(user_id), exc)


def admin_main_keyboard():
    kb = Keyboard(one_time=False)
    kb.add(Text("👥 Ученики"))
    kb.add(Text("📊 Общая статистика"))
    kb.row()
    kb.add(Text("⚠️ Сложные темы"))
    kb.add(Text("🕒 Последние попытки"))
    kb.row()
    kb.add(Text("🏠 Главное меню"))
    return kb.get_json()


def student_detail_keyboard():
    kb = Keyboard(one_time=False)
    kb.add(Text("🕒 История ученика"))
    kb.add(Text("❌ Ошибки ученика"))
    kb.row()
    kb.add(Text("📚 Темы ученика"))
    kb.row()
    kb.add(Text("⬅️ К ученикам"))
    kb.add(Text("⚙️ Админ-меню"))
    return kb.get_json()


def get_admin_state(admin_id):
    if admin_id not in admin_sessions:
        admin_sessions[admin_id] = {
            "page": 0,
            "selected_student": None,
            "student_buttons": {},
        }
    return admin_sessions[admin_id]


async def show_admin_menu(message, admin_id):
    state = get_admin_state(admin_id)
    state["selected_student"] = None
    await message.answer(
        "👩‍🏫 Панель преподавателя\n\n"
        "Здесь можно посмотреть прогресс учеников, историю попыток "
        "и конкретные ошибки.",
        keyboard=admin_main_keyboard(),
    )


async def show_admin_students(message, admin_id, page=None):
    state = get_admin_state(admin_id)
    if page is None:
        page = state.get("page", 0)

    total = get_admin_student_count(ADMIN_VK_IDS)
    max_page = max((total - 1) // ADMIN_PAGE_SIZE, 0) if total else 0
    page = max(0, min(int(page), max_page))
    state["page"] = page

    rows = get_admin_students(
        limit=ADMIN_PAGE_SIZE,
        offset=page * ADMIN_PAGE_SIZE,
        exclude_ids=ADMIN_VK_IDS,
    )
    await ensure_vk_profiles(rows)

    if not rows:
        state["student_buttons"] = {}
        await message.answer(
            "👥 Ученики\n\nПока в базе нет учеников.",
            keyboard=admin_main_keyboard(),
        )
        return

    lines = [
        "👥 Ученики",
        "",
        f"Всего: {total} · страница {page + 1}/{max_page + 1}",
        "",
    ]

    kb = Keyboard(one_time=False)
    button_map = {}

    for row in rows:
        name = person_name(row)
        avg = float(row.get("avg_percent") or 0)
        attempts = int(row.get("attempts") or 0)
        level = row.get("level") or "—"
        lines.append(
            f"• {name} · {level} · попыток {attempts} · средний {avg:.0f}%"
        )

        short_name = name if len(name) <= 28 else name[:27] + "…"
        label = f"👤 {short_name} · {str(row['vk_id'])[-4:]}"
        button_map[label] = int(row["vk_id"])
        kb.add(Text(label))
        kb.row()

    nav_added = False
    if page > 0:
        kb.add(Text("◀️ Назад"))
        nav_added = True
    if page < max_page:
        kb.add(Text("▶️ Далее"))
        nav_added = True
    if nav_added:
        kb.row()

    kb.add(Text("⚙️ Админ-меню"))
    kb.add(Text("🏠 Главное меню"))

    state["student_buttons"] = button_map
    await message.answer("\n".join(lines), keyboard=kb.get_json())


async def show_student_summary(message, admin_id, student_id):
    await ensure_single_profile(student_id)
    summary = get_admin_student_summary(student_id)
    user = summary["user"] or {"vk_id": student_id}
    state = get_admin_state(admin_id)
    state["selected_student"] = student_id

    lines = [
        f"👤 {person_name(user)}",
        "",
        f"Уровень: {user.get('level') or '—'}",
        f"Попыток: {summary['attempts']}",
        f"Уникальных тренировок: {summary['unique_exercises']}",
        f"Средний результат: {summary['avg_percent']:.0f}%",
        f"Лучший результат: {summary['best_percent']}%",
        f"Ошибок сохранено: {summary['mistakes']}",
        f"Последняя попытка: {format_db_time(summary['last_attempt'])}",
    ]

    if summary["by_level"]:
        lines.extend(["", "По уровням:"])
        for row in summary["by_level"]:
            lines.append(
                f"• {row['level']}: {row['unique_exercises']} тренировок, "
                f"средний {float(row['avg_percent'] or 0):.0f}%"
            )

    await message.answer("\n".join(lines), keyboard=student_detail_keyboard())


async def show_student_history(message, admin_id):
    state = get_admin_state(admin_id)
    student_id = state.get("selected_student")
    if not student_id:
        await show_admin_students(message, admin_id)
        return

    await ensure_single_profile(student_id)
    user = get_user_profile(student_id) or {"vk_id": student_id}
    rows = get_admin_student_attempts(student_id, limit=12)

    lines = [f"🕒 История · {person_name(user)}", ""]
    if not rows:
        lines.append("Пока нет выполненных упражнений.")
    else:
        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. {format_db_time(row['created_at'])}\n"
                f"   {row['level']} · {row['topic']} · Practice {int(row['exercise']) + 1}\n"
                f"   🎯 {row['score']}/{row['total']} — {row['percent']}%"
            )

    await message.answer("\n\n".join(lines), keyboard=student_detail_keyboard())


def option_text(level, topic_name, exercise_index, question_number, letter):
    try:
        item = CONTENT[level][topic_name]["exercises"][int(exercise_index)]["questions"][
            int(question_number) - 1
        ]
        index = ord(str(letter).upper()) - ord("A")
        if 0 <= index < len(item["options"]):
            return item["options"][index]
    except Exception:
        pass
    return "—"


def question_text(level, topic_name, exercise_index, question_number):
    try:
        return CONTENT[level][topic_name]["exercises"][int(exercise_index)]["questions"][
            int(question_number) - 1
        ]["question"]
    except Exception:
        return "Вопрос недоступен в текущей версии content.json."


async def show_student_mistakes(message, admin_id):
    state = get_admin_state(admin_id)
    student_id = state.get("selected_student")
    if not student_id:
        await show_admin_students(message, admin_id)
        return

    await ensure_single_profile(student_id)
    user = get_user_profile(student_id) or {"vk_id": student_id}
    rows = get_admin_student_mistakes(student_id, limit=10)

    lines = [f"❌ Последние ошибки · {person_name(user)}", ""]
    if not rows:
        lines.append("Ошибок пока нет.")
    else:
        for i, row in enumerate(rows, start=1):
            q_text = question_text(
                row["level"], row["topic"], row["exercise"], row["question_number"]
            )
            user_option = option_text(
                row["level"],
                row["topic"],
                row["exercise"],
                row["question_number"],
                row["user_answer"],
            )
            correct_option = option_text(
                row["level"],
                row["topic"],
                row["exercise"],
                row["question_number"],
                row["correct_answer"],
            )

            lines.append(
                f"{i}. {format_db_time(row['created_at'])}\n"
                f"   {row['level']} · {row['topic']} · Practice {int(row['exercise']) + 1}\n"
                f"   Вопрос {row['question_number']}: {q_text}\n"
                f"   ❌ Ученик: {row['user_answer']} — {user_option}\n"
                f"   ✅ Верно: {row['correct_answer']} — {correct_option}\n"
                f"   💡 {row.get('tip') or '—'}"
            )

    await message.answer("\n\n".join(lines), keyboard=student_detail_keyboard())


async def show_student_topics(message, admin_id):
    state = get_admin_state(admin_id)
    student_id = state.get("selected_student")
    if not student_id:
        await show_admin_students(message, admin_id)
        return

    await ensure_single_profile(student_id)
    user = get_user_profile(student_id) or {"vk_id": student_id}
    rows = get_admin_student_topics(student_id)

    lines = [f"📚 Темы · {person_name(user)}", ""]
    if not rows:
        lines.append("Пока нет статистики по темам.")
    else:
        for row in rows:
            lines.append(
                f"• {row['level']} · {row['topic']}\n"
                f"  тренировок: {row['exercises']}/3 · попыток: {row['attempts']} · "
                f"средний: {float(row['avg_percent'] or 0):.0f}% · "
                f"лучший: {int(row['best_percent'] or 0)}%"
            )

    await message.answer("\n\n".join(lines), keyboard=student_detail_keyboard())


async def show_admin_overall(message):
    stats = get_admin_overall_stats(ADMIN_VK_IDS)

    lines = [
        "📊 Общая статистика",
        "",
        f"Учеников: {stats['students']}",
        f"Активны за 7 дней: {stats['active_7d']}",
        f"Всего попыток: {stats['attempts']}",
        f"Попыток за 24 часа: {stats['attempts_24h']}",
        f"Средний результат: {stats['avg_percent']:.0f}%",
        f"Всего ошибок: {stats['mistakes']}",
    ]

    if stats["by_level"]:
        lines.extend(["", "Текущий уровень учеников:"])
        for row in stats["by_level"]:
            lines.append(f"• {row['level']}: {row['students']}")

    await message.answer("\n".join(lines), keyboard=admin_main_keyboard())


async def show_admin_difficult(message):
    rows = get_admin_difficult_topics(limit=10, exclude_ids=ADMIN_VK_IDS)
    lines = ["⚠️ Самые сложные темы", ""]

    if not rows:
        lines.append("Пока недостаточно данных.")
    else:
        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. {row['level']} · {row['topic']}\n"
                f"   средний {float(row['avg_percent'] or 0):.0f}% · "
                f"попыток {row['attempts']} · учеников {row['students']}"
            )

    await message.answer("\n\n".join(lines), keyboard=admin_main_keyboard())


async def show_admin_recent(message):
    rows = get_admin_recent_attempts(limit=12, exclude_ids=ADMIN_VK_IDS)
    await ensure_vk_profiles(rows)

    lines = ["🕒 Последние попытки", ""]
    if not rows:
        lines.append("Пока нет выполненных упражнений.")
    else:
        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. {format_db_time(row['created_at'])} · {person_name(row)}\n"
                f"   {row['level']} · {row['topic']} · Practice {int(row['exercise']) + 1}\n"
                f"   🎯 {row['score']}/{row['total']} — {row['percent']}%"
            )

    await message.answer("\n\n".join(lines), keyboard=admin_main_keyboard())


async def handle_admin_action(message, admin_id, text):
    if not is_admin(admin_id):
        return False

    state = get_admin_state(admin_id)

    if text == "⚙️ Админ-меню":
        await show_admin_menu(message, admin_id)
        return True

    if text == "👥 Ученики":
        await show_admin_students(message, admin_id, 0)
        return True

    if text == "◀️ Назад":
        await show_admin_students(message, admin_id, state.get("page", 0) - 1)
        return True

    if text == "▶️ Далее":
        await show_admin_students(message, admin_id, state.get("page", 0) + 1)
        return True

    if text == "⬅️ К ученикам":
        await show_admin_students(message, admin_id)
        return True

    student_id = state.get("student_buttons", {}).get(text)
    if student_id:
        await show_student_summary(message, admin_id, student_id)
        return True

    if text == "🕒 История ученика":
        await show_student_history(message, admin_id)
        return True

    if text == "❌ Ошибки ученика":
        await show_student_mistakes(message, admin_id)
        return True

    if text == "📚 Темы ученика":
        await show_student_topics(message, admin_id)
        return True

    if text == "📊 Общая статистика":
        await show_admin_overall(message)
        return True

    if text == "⚠️ Сложные темы":
        await show_admin_difficult(message)
        return True

    if text == "🕒 Последние попытки":
        await show_admin_recent(message)
        return True

    return False

async def show_levels(message, user_id=None):
    available = ", ".join(CONTENT.keys())
    saved_level = get_user_level(user_id) if user_id else None

    extra = ""
    if saved_level in CONTENT:
        extra = f"\nТвой сохранённый уровень: {saved_level}."

    await message.answer(
        "Привет! 👋 Я помогу потренировать английскую грамматику.\n\n"
        f"Сейчас доступны уровни: {available}."
        f"{extra}\n"
        "Выбери уровень:",
        keyboard=level_keyboard(),
    )


async def show_topics(message, level):
    await message.answer(
        f"Уровень: {level}\n\nВыбери грамматическую тему:",
        keyboard=topic_keyboard(level),
    )


async def show_topic_rule(message, level, topic_name):
    topic = CONTENT[level][topic_name]
    display_title = topic.get("title", topic_name)
    await message.answer(
        f"📚 {display_title}\n\n{topic['rule']}\n\n"
        "Теперь выбери тренировку:",
        keyboard=exercise_keyboard(),
    )


async def show_exercise(message, level, topic_name, exercise_index):
    exercise = CONTENT[level][topic_name]["exercises"][exercise_index]
    await message.answer(format_exercise(exercise))


async def check_exercise(message, user_id, session, answers):
    exercise = CONTENT[session["level"]][session["topic"]]["exercises"][session["exercise"]]
    questions = exercise["questions"][:QUESTIONS_PER_EXERCISE]

    correct_count = 0
    mistake_texts = []
    mistake_rows = []

    for i, (user_answer, item) in enumerate(zip(answers, questions), start=1):
        correct = item["answer"].upper()
        if user_answer == correct:
            correct_count += 1
        else:
            correct_option = item["options"][ord(correct) - ord("A")]
            mistake_texts.append(
                f"{i}. ❌ Твой ответ: {user_answer}\n"
                f"✅ Правильно: {correct} — {correct_option}\n"
                f"💡 {item['tip']}"
            )
            mistake_rows.append(
                {
                    "question_number": i,
                    "user_answer": user_answer,
                    "correct_answer": correct,
                    "tip": item["tip"],
                }
            )

    total = len(questions)
    percent = round(correct_count / total * 100)

    previous = get_previous_attempt(
        user_id,
        session["level"],
        session["topic"],
        session["exercise"],
    )
    best_before = get_best_score(
        user_id,
        session["level"],
        session["topic"],
        session["exercise"],
    )

    save_attempt(
        vk_id=user_id,
        level=session["level"],
        topic=session["topic"],
        exercise=session["exercise"],
        score=correct_count,
        total=total,
        mistakes=mistake_rows,
    )

    logger.info(
        "Exercise completed | user=%s | level=%s | topic=%s | exercise=%s | score=%s/%s",
        mask_vk_id(user_id),
        session["level"],
        session["topic"],
        session["exercise"] + 1,
        correct_count,
        total,
    )

    result = [f"🎯 Результат: {correct_count}/{total} — {percent}%"]

    if previous:
        prev_score = int(previous["score"])
        prev_total = int(previous["total"])
        prev_percent = int(previous["percent"])
        difference = percent - prev_percent
        result.append(
            f"\n📌 Предыдущий результат: {prev_score}/{prev_total} — {prev_percent}%"
        )
        if difference > 0:
            result.append(f"📈 Улучшение: +{difference} п.п.")
        elif difference < 0:
            result.append("🔁 В этот раз результат ниже — можно попробовать ещё раз.")
        else:
            result.append("➡️ Результат такой же, как в прошлый раз.")

    if best_before is not None and percent > best_before:
        result.append("🏆 Новый лучший результат!")

    if correct_count == total:
        result.append("\nОтлично! Все ответы правильные 🎉")
    elif percent >= 80:
        result.append("\nОчень хороший результат! Осталось разобрать несколько деталей.")
    elif percent >= 60:
        result.append("\nХорошая база. Посмотри короткий разбор ошибок ниже.")
    else:
        result.append("\nЭту тему стоит повторить ещё раз. Ниже — только самое важное по ошибкам.")

    if mistake_texts:
        result.append("\n\n".join(mistake_texts))

    result.append(
        "\n\nРезультат сохранён 📊\n"
        "Выбери следующую тренировку, перечитай правило или посмотри прогресс."
    )
    await message.answer("\n".join(result), keyboard=after_exercise_keyboard())


@bot.on.message()
async def message_handler(message: Message):
    text = (message.text or "").strip()
    user_id = message.from_id

    touch_user(user_id)
    await ensure_single_profile(user_id)

    if text.lower() in {"/myid", "мой id", "мой айди"}:
        await message.answer(f"Твой VK ID: {user_id}")
        return

    if text.lower() == "/admin":
        if not ADMIN_VK_IDS:
            await message.answer(
                "Админ-панель пока не настроена.\n\n"
                "Добавь в Bothost переменную окружения ADMIN_VK_ID "
                "со своим числовым VK ID и перезапусти бота.\n\n"
                "Свой ID можно узнать командой /myid."
            )
            return
        if not is_admin(user_id):
            await message.answer("Эта команда доступна только преподавателю.")
            return
        await show_admin_menu(message, user_id)
        return

    if await handle_admin_action(message, user_id, text):
        return

    if user_id not in sessions:
        sessions[user_id] = new_session()

    session = sessions[user_id]

    if text == "📊 Мой прогресс":
        await message.answer(
            format_progress(user_id),
            keyboard=keyboard_for_session(session),
        )
        return

    if text == "🔎 Частые ошибки":
        await message.answer(
            format_frequent_mistakes(user_id),
            keyboard=keyboard_for_session(session),
        )
        return

    if text.lower() in {"начать", "start", "/start", "меню", "menu"} or text == "🏠 Главное меню":
        sessions[user_id] = new_session()
        await show_levels(message, user_id)
        return

    if text == "🔄 Сменить уровень":
        sessions[user_id] = new_session()
        await show_levels(message, user_id)
        return

    if text == "⬅️ К темам":
        if session["level"]:
            session["step"] = "topic"
            session["topic"] = None
            session["exercise"] = None
            await show_topics(message, session["level"])
        else:
            await show_levels(message, user_id)
        return

    if text == "📚 Правило ещё раз":
        if session["level"] and session["topic"]:
            session["step"] = "exercise_choice"
            await show_topic_rule(message, session["level"], session["topic"])
        else:
            await show_levels(message, user_id)
        return

    if session["step"] == "level":
        if text in CONTENT:
            session["level"] = text
            session["step"] = "topic"
            save_user_level(user_id, text)
            await show_topics(message, text)
        else:
            await message.answer(
                "Пожалуйста, выбери доступный уровень кнопкой.",
                keyboard=level_keyboard(),
            )
        return

    if session["step"] == "topic":
        topics = CONTENT[session["level"]]
        if text in topics:
            session["topic"] = text
            session["step"] = "exercise_choice"
            await show_topic_rule(message, session["level"], text)
        else:
            await message.answer(
                "Выбери одну из тем на клавиатуре.",
                keyboard=topic_keyboard(session["level"]),
            )
        return

    if session["step"] in {"exercise_choice", "after_exercise"}:
        if text in EXERCISE_LABELS:
            index = EXERCISE_LABELS[text]
            session["exercise"] = index
            session["step"] = "answers"
            await show_exercise(message, session["level"], session["topic"], index)
        else:
            await message.answer(
                "Выбери 🟢 Practice 1, 🟡 Practice 2 или 🔴 Challenge.",
                keyboard=exercise_keyboard(),
            )
        return

    if session["step"] == "answers":
        exercise = CONTENT[session["level"]][session["topic"]]["exercises"][session["exercise"]]
        expected_count = min(QUESTIONS_PER_EXERCISE, len(exercise["questions"]))
        answers = parse_answers(text, expected_count)
        if not answers:
            await message.answer(
                "Я не смог распознать ответы.\n\n"
                f"Пришли ровно {expected_count} ответов, например:\n"
                "1A 2B 3C 4A 5B 6C 7A 8B 9C 10A\n\n"
                "Можно использовать и кириллицу: А/В/С. Регистр не важен."
            )
            return

        await check_exercise(message, user_id, session, answers)
        session["step"] = "after_exercise"
        return

    sessions[user_id] = new_session()
    await show_levels(message, user_id)


if __name__ == "__main__":
    logger.info("Bot starting")
    bot.run()
