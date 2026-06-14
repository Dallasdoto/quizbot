import os
import json
import random
import asyncio
import logging
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
DATA_FILE = "quizzes.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"quizzes": {}, "user_state": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def api(method, **params):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{BASE_URL}/{method}", json=params)
        return r.json()

async def send(chat_id, text, **kwargs):
    await api("sendMessage", chat_id=chat_id, text=text, parse_mode="Markdown", **kwargs)

async def send_poll(chat_id, question, options, correct_index):
    return await api(
        "sendPoll",
        chat_id=chat_id,
        question=question,
        options=options,
        type="quiz",
        correct_option_id=correct_index,
        is_anonymous=False,
        open_period=30
    )

# ─── STATE MACHINE ────────────────────────────────────────────────────────────
# States: idle, wait_title, wait_question, wait_options, wait_correct, wait_more
#         wait_quiz_choice, wait_delete_choice, in_quiz

async def handle_update(update, data):
    # Poll answer (when user answers a quiz)
    if "poll_answer" in update:
        await handle_poll_answer(update["poll_answer"], data)
        return

    if "message" not in update:
        return

    msg = update["message"]
    chat_id = msg["chat"]["id"]
    user_id = str(msg["from"]["id"])
    text = msg.get("text", "").strip()

    # ── Пользователь отправил опрос боту ──
    if "poll" in msg:
        await handle_incoming_poll(msg, user_id, chat_id, data)
        return

    states = data.setdefault("user_state", {})
    state = states.get(user_id, {})
    current = state.get("step", "idle")

    # Commands
    if text == "/start" or text.startswith("/start "):
        states[user_id] = {"step": "idle"}
        await send(chat_id,
            "👋 Привет! Я бот для викторин.\n\n"
            "📋 *Команды:*\n"
            "/newquiz — создать викторину\n"
            "/myquizzes — мои викторины\n"
            "/startquiz — запустить викторину\n"
            "/deletequiz — удалить викторину\n"
            "/help — помощь"
        )
        return

    if text == "/help":
        await send(chat_id,
            "📖 *Как пользоваться:*\n\n"
            "1️⃣ /newquiz — создать викторину\n"
            "2️⃣ /myquizzes — список викторин\n"
            "3️⃣ /startquiz — запустить викторину\n"
            "4️⃣ /deletequiz — удалить викторину\n\n"
            "✅ Правильный ответ всегда сохраняется корректно!"
        )
        return

    if text == "/newquiz":
        states[user_id] = {"step": "wait_title", "questions": []}
        save_data(data)
        await send(chat_id, "📝 *Создание викторины*\n\nВведи *название* викторины:")
        return

    if text == "/myquizzes":
        quizzes = data.get("quizzes", {}).get(user_id, [])
        if not quizzes:
            await send(chat_id, "📭 Нет викторин. Создай с /newquiz")
        else:
            lines = "\n".join(f"{i+1}. *{q['title']}* — {len(q['questions'])} вопр." for i, q in enumerate(quizzes))
            await send(chat_id, f"📚 *Твои викторины:*\n\n{lines}\n\nЗапусти: /startquiz")
        return

    if text == "/startquiz":
        quizzes = data.get("quizzes", {}).get(user_id, [])
        if not quizzes:
            await send(chat_id, "📭 Нет викторин. Создай с /newquiz")
        else:
            lines = "\n".join(f"{i+1}. {q['title']} ({len(q['questions'])} вопр.)" for i, q in enumerate(quizzes))
            states[user_id] = {"step": "wait_quiz_choice"}
            save_data(data)
            await send(chat_id, f"🎯 *Выбери номер викторины:*\n\n{lines}")
        return

    if text == "/deletequiz":
        quizzes = data.get("quizzes", {}).get(user_id, [])
        if not quizzes:
            await send(chat_id, "📭 Нет викторин.")
        else:
            lines = "\n".join(f"{i+1}. {q['title']}" for i, q in enumerate(quizzes))
            states[user_id] = {"step": "wait_delete_choice"}
            save_data(data)
            await send(chat_id, f"🗑 *Какую удалить? Введи номер:*\n\n{lines}")
        return

    if text == "/settitle":
        state = states.get(user_id, {})
        if not state.get("questions"):
            await send(chat_id, "❌ Сначала отправь хотя бы один опрос-викторину!")
        else:
            state["step"] = "wait_title_for_poll"
            save_data(data)
            await send(chat_id, "✏️ Введи название для викторины:")
        return

    if current == "wait_title_for_poll":
        state["title"] = text
        state["step"] = "wait_more"
        save_data(data)
        await send(chat_id, f"✅ Название установлено: *{text}*\n\nОтправь ещё опрос или напиши *2* чтобы сохранить.")
        return

    if text == "/cancel":
        states[user_id] = {"step": "idle"}
        save_data(data)
        await send(chat_id, "❌ Отменено.")
        return

    # ── State handlers ──
    if current == "wait_title":
        if not text:
            await send(chat_id, "❌ Название не может быть пустым:")
            return
        state["title"] = text
        state["step"] = "wait_question"
        save_data(data)
        await send(chat_id, f"✅ Название: *{text}*\n\nВведи текст *первого вопроса*:")
        return

    if current == "wait_question":
        state["current_question"] = text
        state["step"] = "wait_options"
        save_data(data)
        await send(chat_id,
            "✏️ Введи *варианты ответов* — каждый с новой строки:\n\n"
            "Например:\nПариж\nЛондон\nБерлин\n\n_(минимум 2, максимум 10)_"
        )
        return

    if current == "wait_options":
        options = [line.strip() for line in text.split("\n") if line.strip()]
        if len(options) < 2:
            await send(chat_id, "❌ Нужно минимум 2 варианта. Попробуй ещё раз:")
            return
        if len(options) > 10:
            await send(chat_id, "❌ Максимум 10 вариантов. Попробуй ещё раз:")
            return
        state["current_options"] = options
        state["step"] = "wait_correct"
        save_data(data)
        numbered = "\n".join(f"{i+1}. {o}" for i, o in enumerate(options))
        await send(chat_id, f"📋 *Варианты:*\n{numbered}\n\nВведи *номер правильного ответа* (1–{len(options)}):")
        return

    if current == "wait_correct":
        options = state.get("current_options", [])
        try:
            num = int(text)
            if num < 1 or num > len(options):
                raise ValueError
        except ValueError:
            await send(chat_id, f"❌ Введи число от 1 до {len(options)}:")
            return
        correct_text = options[num - 1]
        state.setdefault("questions", []).append({
            "question": state["current_question"],
            "options": options,
            "correct_answer": correct_text  # Сохраняем ТЕКСТ правильного ответа
        })
        q_num = len(state["questions"])
        state["step"] = "wait_more"
        save_data(data)
        await send(chat_id,
            f"✅ Вопрос {q_num} добавлен!\n"
            f"Правильный ответ: *{correct_text}*\n\n"
            f"Что дальше?\n"
            f"Напиши *1* — добавить ещё вопрос\n"
            f"Напиши *2* — сохранить викторину"
        )
        return

    if current == "wait_more":
        if text == "1":
            state["step"] = "wait_question"
            save_data(data)
            await send(chat_id, "✏️ Введи текст следующего вопроса:")
        elif text == "2":
            quizzes = data.setdefault("quizzes", {}).setdefault(user_id, [])
            quiz = {
                "id": f"{user_id}_{len(quizzes)}",
                "title": state["title"],
                "questions": state["questions"]
            }
            quizzes.append(quiz)
            states[user_id] = {"step": "idle"}
            save_data(data)
            await send(chat_id,
                f"🎉 Викторина *«{quiz['title']}»* сохранена!\n"
                f"Вопросов: {len(quiz['questions'])}\n\n"
                f"Запусти командой /startquiz"
            )
        else:
            await send(chat_id, "Напиши *1* (ещё вопрос) или *2* (сохранить):")
        return

    if current == "wait_quiz_choice":
        quizzes = data.get("quizzes", {}).get(user_id, [])
        try:
            num = int(text)
            if num < 1 or num > len(quizzes):
                raise ValueError
        except ValueError:
            await send(chat_id, f"❌ Введи число от 1 до {len(quizzes)}:")
            return
        quiz = quizzes[num - 1]
        states[user_id] = {
            "step": "in_quiz",
            "quiz": quiz,
            "q_index": 0,
            "score": 0
        }
        save_data(data)
        await send(chat_id, f"🚀 Начинаем *«{quiz['title']}»*!\nВопросов: {len(quiz['questions'])}")
        await send_quiz_question(chat_id, user_id, data)
        return

    if current == "wait_delete_choice":
        quizzes = data.get("quizzes", {}).get(user_id, [])
        try:
            num = int(text)
            if num < 1 or num > len(quizzes):
                raise ValueError
        except ValueError:
            await send(chat_id, f"❌ Введи число от 1 до {len(quizzes)}:")
            return
        deleted = quizzes.pop(num - 1)
        states[user_id] = {"step": "idle"}
        save_data(data)
        await send(chat_id, f"🗑 Викторина *«{deleted['title']}»* удалена!")
        return

async def send_quiz_question(chat_id, user_id, data):
    state = data["user_state"][user_id]
    quiz = state["quiz"]
    idx = state["q_index"]

    if idx >= len(quiz["questions"]):
        score = state.get("score", 0)
        total = len(quiz["questions"])
        data["user_state"][user_id] = {"step": "idle"}
        save_data(data)
        await send(chat_id,
            f"🏁 *Викторина завершена!*\n\n"
            f"Результат: {score}/{total} ✅\n"
            f"Спасибо за участие! 🎉"
        )
        return

    q = quiz["questions"][idx]
    options = q["options"].copy()
    correct_text = q["correct_answer"]
    random.shuffle(options)
    correct_index = options.index(correct_text)  # Правильный индекс после перемешивания

    total = len(quiz["questions"])
    result = await send_poll(
        chat_id,
        question=f"❓ Вопрос {idx+1}/{total}: {q['question']}",
        options=options,
        correct_index=correct_index
    )

    if result.get("ok"):
        poll_id = result["result"]["poll"]["id"]
        data.setdefault("active_polls", {})[poll_id] = {
            "chat_id": chat_id,
            "user_id": user_id
        }
        save_data(data)

async def handle_incoming_poll(msg, user_id, chat_id, data):
    """Пользователь отправил опрос — сохраняем его как вопрос викторины."""
    poll = msg["poll"]
    question_text = poll.get("question", "").strip()
    options = [o["text"] for o in poll.get("options", [])]

    # Определяем правильный ответ
    correct_option_id = poll.get("correct_option_id")  # только для quiz-типа
    poll_type = poll.get("type", "regular")

    states = data.setdefault("user_state", {})
    state = states.get(user_id, {})
    current_step = state.get("step", "idle")

    if poll_type == "quiz" and correct_option_id is not None:
        # Опрос типа "Викторина" — правильный ответ уже задан
        correct_text = options[correct_option_id]

        question = {
            "question": question_text,
            "options": options,
            "correct_answer": correct_text
        }

        if current_step == "wait_question" or current_step == "wait_more" or current_step == "idle":
            # Добавляем вопрос к текущей создаваемой викторине
            if current_step == "idle":
                # Начинаем новую викторину автоматически
                state = {"step": "wait_more", "title": "Викторина", "questions": [question]}
                states[user_id] = state
                save_data(data)
                await send(chat_id,
                    f"✅ Вопрос добавлен!\n"
                    f"❓ *{question_text}*\n"
                    f"Правильный ответ: *{correct_text}*\n\n"
                    f"Отправь ещё опрос чтобы добавить вопрос,\n"
                    f"или напиши *2* чтобы сохранить викторину,\n"
                    f"или /settitle чтобы задать название."
                )
            else:
                state.setdefault("questions", []).append(question)
                state["step"] = "wait_more"
                save_data(data)
                q_num = len(state["questions"])
                await send(chat_id,
                    f"✅ Вопрос {q_num} добавлен!\n"
                    f"❓ *{question_text}*\n"
                    f"Правильный ответ: *{correct_text}*\n\n"
                    f"Отправь ещё опрос чтобы добавить вопрос,\n"
                    f"или напиши *2* чтобы сохранить викторину."
                )
        else:
            await send(chat_id, "⚠️ Сначала начни создание викторины командой /newquiz, или просто отправь опрос ещё раз.")
    else:
        # Обычный опрос без правильного ответа
        await send(chat_id,
            "⚠️ Это обычный опрос без правильного ответа.\n\n"
            "Создай опрос типа *Викторина* (с галочкой ✅ на правильном варианте) и отправь снова!\n\n"
            "Как создать: скрепка 📎 → Опрос → включи *Викторина* → отметь правильный ответ."
        )

async def handle_poll_answer(poll_answer, data):
    poll_id = poll_answer["poll_id"]
    poll_info = data.get("active_polls", {}).get(poll_id)
    if not poll_info:
        return

    user_id = poll_info["user_id"]
    chat_id = poll_info["chat_id"]
    state = data.get("user_state", {}).get(user_id, {})

    if state.get("step") != "in_quiz":
        return

    del data["active_polls"][poll_id]
    state["q_index"] += 1
    save_data(data)

    await asyncio.sleep(2)
    await send_quiz_question(chat_id, user_id, data)

# ─── POLLING LOOP ─────────────────────────────────────────────────────────────
async def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан!")
    logger.info("Bot started!")
    offset = 0
    while True:
        try:
            result = await api("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "poll_answer", "poll"])
            if result.get("ok"):
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    data = load_data()
                    try:
                        await handle_update(update, data)
                    except Exception as e:
                        logger.error(f"Error handling update: {e}")
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
