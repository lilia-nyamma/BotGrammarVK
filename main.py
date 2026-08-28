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

# Для первой версии состояние хранится в памяти.
# После перезапуска бота пользователь просто снова выберет уровень.
sessions = {}


def new_session():
    return {
        "step": "level",
        "level": None,
        "topic": None,
        "exercise": None,
    }


def level_keyboard():
    kb = Keyboard(one_time=False)
    kb.add(Text("A2"))
    kb.add(Text("B1"))
    kb.add(Text("B2"))
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
    return kb.get_json()


def exercise_keyboard():
    kb = Keyboard(one_time=False)
    kb.add(Text("Упражнение 1"))
    kb.add(Text("Упражнение 2"))
    kb.add(Text("Упражнение 3"))
    kb.row()
    kb.add(Text("⬅️ К темам"))
    return kb.get_json()


def after_exercise_keyboard():
    kb = Keyboard(one_time=False)
    kb.add(Text("Упражнение 1"))
    kb.add(Text("Упражнение 2"))
    kb.add(Text("Упражнение 3"))
    kb.row()
    kb.add(Text("⬅️ К темам"))
    kb.add(Text("🔄 Сменить уровень"))
    return kb.get_json()


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
    # Поддерживает форматы:
    # 1A 2B 3C ...
    # 1-A, 2-B, 3-C ...
    # A B C ...
    # ABCABC...
    upper = text.upper().strip()

    numbered = re.findall(r"(?:^|\s|,|;)(\d{1,2})\s*[-.:)]?\s*([ABC])(?=\s|,|;|$)", upper)
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
    await message.answer(
        "Привет! 👋 Я помогу потренировать английскую грамматику.\n\n"
        "Сначала выбери свой уровень:",
        keyboard=level_keyboard(),
    )


async def show_topics(message, level):
    await message.answer(
        f"Уровень: {level}\n\nВыбери грамматическую тему:",
        keyboard=topic_keyboard(level),
    )


async def show_topic_rule(message, level, topic_name):
    topic = CONTENT[level][topic_name]
    await message.answer(
        f"📚 {topic_name}\n\n{topic['rule']}\n\n"
        "Теперь выбери упражнение:",
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
            mistakes.append(
                f"{i}. Твой ответ: {user_answer}. Правильно: {correct} — "
                f"{item['options'][ord(correct) - ord('A')]}\n"
                f"💡 {item['tip']}"
            )

    result = [f"✅ Результат: {correct_count}/15."]

    if not mistakes:
        result.append("\nОтлично! Все ответы правильные 🎉")
    else:
        result.append(f"\nОшибок: {15 - correct_count}. Короткий разбор:")
        result.append("\n\n".join(mistakes))

    result.append("\nВыбери следующее упражнение или вернись к темам.")
    await message.answer("\n".join(result), keyboard=after_exercise_keyboard())


@bot.on.message()
async def message_handler(message: Message):
    text = (message.text or "").strip()
    user_id = message.from_id

    if user_id not in sessions:
        sessions[user_id] = new_session()

    session = sessions[user_id]

    # Команды, которые работают из любого места
    if text.lower() in {"начать", "start", "/start", "меню", "menu"}:
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

    # Шаг 1: выбор уровня
    if session["step"] == "level":
        if text in CONTENT:
            session["level"] = text
            session["step"] = "topic"
            await show_topics(message, text)
        else:
            await message.answer(
                "Пожалуйста, выбери уровень кнопкой: A2, B1 или B2.",
                keyboard=level_keyboard(),
            )
        return

    # Шаг 2: выбор темы
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

    # Шаг 3: выбор упражнения
    if session["step"] in {"exercise_choice", "after_exercise"}:
        match = re.fullmatch(r"Упражнение\s+([123])", text)
        if match:
            index = int(match.group(1)) - 1
            session["exercise"] = index
            session["step"] = "answers"
            await show_exercise(message, session["level"], session["topic"], index)
        else:
            await message.answer(
                "Выбери упражнение 1, 2 или 3.",
                keyboard=exercise_keyboard(),
            )
        return

    # Шаг 4: проверка ответов
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

    # На всякий случай
    sessions[user_id] = new_session()
    await show_levels(message)


if __name__ == "__main__":
    bot.run_forever()