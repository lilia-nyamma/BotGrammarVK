import json
import logging
import os
import re
from pathlib import Path

from vkbottle import Bot, Keyboard, Text
from vkbottle.bot import Message

from database import (
    get_best_score,
    get_frequent_mistakes,
    get_previous_attempt,
    get_progress,
    get_user_level,
    init_db,
    save_attempt,
    save_user_level,
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

BASE_DIR = Path(__file__).resolve().parent
with open(BASE_DIR / "content.json", "r", encoding="utf-8") as f:
    CONTENT = json.load(f)

init_db()
logger.info("SQLite database initialized")

sessions = {}

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
    lines = [
        f"📝 {exercise['title']}",
        "",
        "Ответь на 15 вопросов.",
        "Пришли ответы одной строкой, например:",
        "1A 2B 3C 4A ... 15B",
        "",
    ]
    for i, item in enumerate(exercise["questions"], start=1):
        lines.append(f"{i}. {item['question']}")
        for letter, option in zip(("A", "B", "C"), item["options"]):
            lines.append(f"   {letter}) {option}")
        lines.append("")
    return "\n".join(lines)


def parse_answers(text):
    upper = text.upper().strip()

    numbered = re.findall(
        r"(?:^|\s|,|;)(\d{1,2})\s*[-.:)]?\s*([ABC])(?=\s|,|;|$)",
        upper,
    )
    if numbered:
        result = {}
        for num, letter in numbered:
            n = int(num)
            if 1 <= n <= 15:
                result[n] = letter
        if len(result) == 15:
            return [result[i] for i in range(1, 16)]

    letters = re.findall(r"\b[ABC]\b", upper)
    if len(letters) == 15:
        return letters

    compact = re.sub(r"[^ABC]", "", upper)
    if len(compact) == 15:
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
    questions = exercise["questions"]

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
        user_id,
        session["level"],
        session["topic"],
        session["exercise"] + 1,
        correct_count,
        total,
    )

    result = [f"🎯 Результат: {correct_count}/{total} — {percent}%"]

    if previous:
        prev_score = int(previous["score"])
        difference = correct_count - prev_score
        result.append(f"\n📌 Предыдущий результат: {prev_score}/{total}")
        if difference > 0:
            result.append(f"📈 Улучшение: +{difference} правильных ответа")
        elif difference < 0:
            result.append("🔁 В этот раз результат ниже — можно попробовать ещё раз.")
        else:
            result.append("➡️ Результат такой же, как в прошлый раз.")

    if best_before is not None and correct_count > best_before:
        result.append("🏆 Новый лучший результат!")

    if correct_count == total:
        result.append("\nОтлично! Все ответы правильные 🎉")
    elif correct_count >= 12:
        result.append("\nОчень хороший результат! Осталось разобрать несколько деталей.")
    elif correct_count >= 9:
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
        answers = parse_answers(text)
        if not answers:
            await message.answer(
                "Я не смог распознать ответы.\n\n"
                "Пришли ровно 15 ответов, например:\n"
                "1A 2B 3C 4A 5B 6C 7A 8B 9C 10A 11B 12C 13A 14B 15C"
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
