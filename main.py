import json
import os
import re
from pathlib import Path

from vkbottle import Bot, Keyboard, Text
from vkbottle.bot import Message


TOKEN = os.getenv("VK_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задана переменная окружения VK_TOKEN")

bot = Bot(token=TOKEN)

BASE_DIR = Path(__file__).resolve().parent
with open(BASE_DIR / "content.json", "r", encoding="utf-8") as f:
    CONTENT = json.load(f)

sessions = {}

EXERCISE_LABELS = {
    "🟢 Practice 1": 0,
    "🟡 Practice 2": 1,
    "🔴 Challenge": 2,
}


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
    return kb.get_json()


def topic_keyboard(level):
    kb = Keyboard(one_time=False)
    topics = CONTENT[level]
    for i, topic_name in enumerate(topics.keys()):
        if i and i % 2 == 0:
            kb.row()
        kb.add(Text(topic_name))
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
    kb.add(Text("🏠 Главное меню"))
    return kb.get_json()


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


async def show_levels(message):
    available = ", ".join(CONTENT.keys())
    await message.answer(
        "Привет! 👋 Я помогу потренировать английскую грамматику.\n\n"
        f"Сейчас доступны уровни: {available}.\n"
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


async def check_exercise(message, session, answers):
    exercise = CONTENT[session["level"]][session["topic"]]["exercises"][session["exercise"]]
    questions = exercise["questions"]

    correct_count = 0
    mistakes = []

    for i, (user_answer, item) in enumerate(zip(answers, questions), start=1):
        correct = item["answer"].upper()
        if user_answer == correct:
            correct_count += 1
        else:
            correct_option = item["options"][ord(correct) - ord("A")]
            mistakes.append(
                f"{i}. ❌ Твой ответ: {user_answer}\n"
                f"✅ Правильно: {correct} — {correct_option}\n"
                f"💡 {item['tip']}"
            )

    percent = round(correct_count / 15 * 100)
    result = [f"🎯 Результат: {correct_count}/15 — {percent}%"]

    if correct_count == 15:
        result.append("\nОтлично! Все ответы правильные 🎉")
    elif correct_count >= 12:
        result.append("\nОчень хороший результат! Осталось разобрать несколько деталей.")
    elif correct_count >= 9:
        result.append("\nХорошая база. Посмотри короткий разбор ошибок ниже.")
    else:
        result.append("\nЭту тему стоит повторить ещё раз. Ниже — только самое важное по ошибкам.")

    if mistakes:
        result.append("\n\n".join(mistakes))

    result.append("\n\nВыбери следующую тренировку, перечитай правило или вернись к темам.")
    await message.answer("\n".join(result), keyboard=after_exercise_keyboard())


@bot.on.message()
async def message_handler(message: Message):
    text = (message.text or "").strip()
    user_id = message.from_id

    if user_id not in sessions:
        sessions[user_id] = new_session()

    session = sessions[user_id]

    if text.lower() in {"начать", "start", "/start", "меню", "menu"} or text == "🏠 Главное меню":
        sessions[user_id] = new_session()
        await show_levels(message)
        return

    if text == "🔄 Сменить уровень":
        sessions[user_id] = new_session()
        await show_levels(message)
        return

    if text == "⬅️ К темам":
        if session["level"]:
            session["step"] = "topic"
            session["topic"] = None
            session["exercise"] = None
            await show_topics(message, session["level"])
        else:
            await show_levels(message)
        return

    if text == "📚 Правило ещё раз":
        if session["level"] and session["topic"]:
            session["step"] = "exercise_choice"
            await show_topic_rule(message, session["level"], session["topic"])
        else:
            await show_levels(message)
        return

    if session["step"] == "level":
        if text in CONTENT:
            session["level"] = text
            session["step"] = "topic"
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

        await check_exercise(message, session, answers)
        session["step"] = "after_exercise"
        return

    sessions[user_id] = new_session()
    await show_levels(message)


if __name__ == "__main__":
    bot.run()