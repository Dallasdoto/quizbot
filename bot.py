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
    return {"quizzes": {}, "user_state": {}, "active_polls": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def api(method, **params):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{BASE_URL}/{method}", json=params)
        return r.json()

async def send(chat_id, text, reply_markup=None, **kwargs):
    params = dict(chat_id=chat_id, text=text, parse_mode="Markdown", **kwargs)
    if reply_markup:
        params["reply_markup"] = reply_markup
    await api("sendMessage", **params)

async def answer_callback(callback_id, text=None):
    await api("answerCallbackQuery", callback_query_id=callback_id, text=text)

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

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────

def quiz_action_keyboard(quiz_id, user_id):
    """Кнопки действий для викторины: Решить / Отправить в группу / Поделиться"""
    return {
        "inline_keyboard": [
            [{"text": "▶️ Решить тест", "callback_data": f"solve:{user_id}:{quiz_id}"}],
            [{"text": "👥 Отправить в группу", "callback_data": f"togroup:{user_id}:{quiz_id}"}],
            [{"text": "🔗 Поделиться тестом", "switch_inline_query": f"quiz:{user_id}:{quiz_id}"}]
        ]
    }

def quizzes_keyboard(quizzes, user_id, action="show"):
    """Список викторин кнопками"""
    buttons = []
    for i, q in enumerate(quizzes):
        label = f"📝 {q['title']} ({len(q['questions'])} вопр.)"
        buttons.append([{"text": label, "callback_data": f"{action}:{user_id}:{i}"}])
    return {"inline_keyboard": buttons}

def save_or_more_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "➕ Добавить ещё вопрос", "callback_data": "more:add"}],
            [{"text": "💾 Сохранить викторину", "callback_data": "more:save"}]
        ]
    }

# ─── HANDLERS ────────────────────────────────────────────────────────────────

async def handle_update(update, data):
    if "poll_answer" in update:
        await handle_poll_answer(update["poll_answer"], data)
        return

    if "callback_query" in update:
        await handle_callback(update["callback_query"], data)
        return

    if "message" not in update:
        return

    msg = update["message"]
    chat_id = msg["chat"]["id"]
    user_id = str(msg["from"]["id"])
    text = msg.get("text", "").strip()

    if "poll" in msg:
        await handle_incoming_poll(msg, user_id, chat_id, data)
        return

    states = data.setdefault("user_state", {})
    state = states.get(user_id, {})
    current = state.get("step", "idle")

    # ── Команды ──
    if text in ("/start", "/start@" + await get_bot_username()) or text.startswith("/start "):
        states[user_id] = {"step": "idle"}
        save_data(data)
        await send(chat_id,
            "👋 Привет! Я бот для викторин.\n\n"
            "📋 *Команды:*\n"
            "/newquiz — создать викторину\n"
            "/myquizzes — мои викторины\n"
            "/startquiz — запустить викторину\n"
            "/deletequiz — удалить викторину\n"
            "/stop — остановить текущий тест\n"
            "/help — помощь"
        )
        return

    if text == "/help":
        await send(chat_id,
            "📖 *Как пользоваться:*\n\n"
            "1️⃣ /newquiz — создать викторину вручную\n"
            "📎 Или просто отправь опрос-*Викторину* прямо сюда!\n\n"
            "2️⃣ /myquizzes — список твоих викторин\n"
            "3️⃣ /startquiz — запустить викторину\n"
            "4️⃣ /deletequiz — удалить викторину\n"
            "5️⃣ /stop — остановить текущий тест\n\n"
            "✅ Правильный ответ всегда сохраняется корректно!"
        )
        return

    if text == "/stop":
        if current == "in_quiz":
            states[user_id] = {"step": "idle"}
            save_data(data)
            await send(chat_id, "⛔ Тест остановлен.")
        else:
            await send(chat_id, "ℹ️ Сейчас нет активного теста.")
        return

    if text == "/newquiz":
        states[user_id] = {"step": "wait_title", "questions": []}
        save_data(data)
        await send(chat_id, "📝 *Создание викторины*\n\nВведи *название* викторины:\n_(или /cancel для отмены)_")
        return

    if text == "/myquizzes":
        await show_my_quizzes(chat_id, user_id, data)
        return

    if text == "/startquiz":
        await show_startquiz(chat_id, user_id, data)
        return

    if text == "/deletequiz":
        quizzes = data.get("quizzes", {}).get(user_id, [])
        if not quizzes:
            await send(chat_id, "📭 Нет викторин.")
        else:
            await send(chat_id, "🗑 *Какую викторину удалить?*",
                reply_markup=quizzes_keyboard(quizzes, user_id, action="delete"))
        return

    if text == "/settitle":
        if not state.get("questions"):
            await send(chat_id, "❌ Сначала отправь хотя бы один опрос-викторину!")
        else:
            state["step"] = "wait_title_for_poll"
            save_data(data)
            await send(chat_id, "✏️ Введи название для викторины:")
        return

    if text == "/cancel":
        states[user_id] = {"step": "idle"}
        save_data(data)
        await send(chat_id, "❌ Отменено.")
        return

    # ── Состояния ──
    if current == "wait_title":
        if not text:
            await send(chat_id, "❌ Название не может быть пустым:")
            return
        state["title"] = text
        state["step"] = "wait_question"
        save_data(data)
        await send(chat_id, f"✅ Название: *{text}*\n\nВведи текст *первого вопроса*:")
        return

    if current == "wait_title_for_poll":
        state["title"] = text
        state["step"] = "wait_more"
        save_data(data)
        await send(chat_id,
            f"✅ Название: *{text}*\n\nОтправь ещё опрос или нажми кнопку:",
            reply_markup=save_or_more_keyboard()
        )
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
            "correct_answer": correct_text
        })
        q_num = len(state["questions"])
        state["step"] = "wait_more"
        save_data(data)
        await send(chat_id,
            f"✅ Вопрос {q_num} добавлен!\nПравильный ответ: *{correct_text}*\n\nЧто дальше?",
            reply_markup=save_or_more_keyboard()
        )
        return

    if current == "wait_more":
        # Текстовый ввод в этом состоянии игнорируем — есть кнопки
        await send(chat_id, "Используй кнопки выше 👆 или отправь ещё опрос.", reply_markup=save_or_more_keyboard())
        return

async def show_my_quizzes(chat_id, user_id, data):
    quizzes = data.get("quizzes", {}).get(user_id, [])
    if not quizzes:
        await send(chat_id, "📭 Нет викторин. Создай с /newquiz")
        return
    await send(chat_id, "📚 *Твои викторины:* Выбери одну:",
        reply_markup=quizzes_keyboard(quizzes, user_id, action="show"))

async def show_startquiz(chat_id, user_id, data):
    quizzes = data.get("quizzes", {}).get(user_id, [])
    if not quizzes:
        await send(chat_id, "📭 Нет викторин. Создай с /newquiz")
        return
    await send(chat_id, "🎯 *Выбери викторину для запуска:*",
        reply_markup=quizzes_keyboard(quizzes, user_id, action="solve"))

# ─── CALLBACK HANDLER ─────────────────────────────────────────────────────────

async def handle_callback(cb, data):
    callback_id = cb["id"]
    chat_id = cb["message"]["chat"]["id"]
    caller_id = str(cb["from"]["id"])
    cb_data = cb.get("data", "")

    await answer_callback(callback_id)

    parts = cb_data.split(":")

    # Кнопки добавить/сохранить при создании
    if cb_data == "more:add":
        states = data.setdefault("user_state", {})
        state = states.get(caller_id, {})
        state["step"] = "wait_question"
        save_data(data)
        await send(chat_id, "✏️ Введи текст следующего вопроса:")
        return

    if cb_data == "more:save":
        states = data.setdefault("user_state", {})
        state = states.get(caller_id, {})
        await save_quiz(chat_id, caller_id, state, data)
        return

    if len(parts) < 3:
        return

    action, owner_id, quiz_idx_str = parts[0], parts[1], parts[2]

    try:
        quiz_idx = int(quiz_idx_str)
    except ValueError:
        return

    quizzes = data.get("quizzes", {}).get(owner_id, [])
    if quiz_idx < 0 or quiz_idx >= len(quizzes):
        await send(chat_id, "❌ Викторина не найдена.")
        return
    quiz = quizzes[quiz_idx]

    if action == "show":
        # Показать карточку викторины с кнопками
        await send(chat_id,
            f"📋 *{quiz['title']}*\nВопросов: {len(quiz['questions'])}\n\nЧто хочешь сделать?",
            reply_markup=quiz_action_keyboard(quiz_idx, owner_id)
        )
        return

    if action == "solve":
        # Запустить тест
        states = data.setdefault("user_state", {})
        states[caller_id] = {
            "step": "in_quiz",
            "quiz": quiz,
            "q_index": 0,
            "score": 0
        }
        save_data(data)
        await send(chat_id, f"🚀 Начинаем *«{quiz['title']}»*!\nВопросов: {len(quiz['questions'])}\n\n_/stop — остановить тест_")
        await send_quiz_question(chat_id, caller_id, data)
        return

    if action == "togroup":
        # Инструкция как отправить в группу
        bot_info = await api("getMe")
        username = bot_info.get("result", {}).get("username", "бот")
        await send(chat_id,
            f"👥 *Отправить тест в группу:*\n\n"
            f"1. Добавь @{username} в свою группу\n"
            f"2. В группе напиши команду:\n"
            f"`/startquiz`\n"
            f"3. Выбери викторину *«{quiz['title']}»*\n\n"
            f"Все участники группы смогут отвечать на вопросы! 🎉"
        )
        return

    if action == "delete":
        quizzes.pop(quiz_idx)
        data["quizzes"][owner_id] = quizzes
        save_data(data)
        await send(chat_id, f"🗑 Викторина *«{quiz['title']}»* удалена!")
        return

async def save_quiz(chat_id, user_id, state, data):
    questions = state.get("questions", [])
    if not questions:
        await send(chat_id, "❌ Нет вопросов! Добавь хотя бы один.")
        return
    quizzes = data.setdefault("quizzes", {}).setdefault(user_id, [])
    quiz = {
        "id": f"{user_id}_{len(quizzes)}",
        "title": state.get("title", "Викторина"),
        "questions": questions
    }
    quizzes.append(quiz)
    data["user_state"][user_id] = {"step": "idle"}
    save_data(data)
    await send(chat_id,
        f"🎉 Викторина *«{quiz['title']}»* сохранена!\n"
        f"Вопросов: {len(quiz['questions'])}\n\nЧто хочешь сделать?",
        reply_markup=quiz_action_keyboard(len(quizzes) - 1, user_id)
    )

# ─── INCOMING POLL ────────────────────────────────────────────────────────────

async def handle_incoming_poll(msg, user_id, chat_id, data):
    poll = msg["poll"]
    question_text = poll.get("question", "").strip()
    options = [o["text"] for o in poll.get("options", [])]
    correct_option_id = poll.get("correct_option_id")
    poll_type = poll.get("type", "regular")

    states = data.setdefault("user_state", {})
    state = states.get(user_id, {})
    current_step = state.get("step", "idle")

    if poll_type != "quiz" or correct_option_id is None:
        await send(chat_id,
            "⚠️ Это обычный опрос без правильного ответа.\n\n"
            "Создай опрос типа *Викторина* и отметь правильный ответ ✅\n\n"
            "Как: скрепка 📎 → Опрос → включи *Викторина* → выбери правильный ответ."
        )
        return

    correct_text = options[correct_option_id]
    question = {
        "question": question_text,
        "options": options,
        "correct_answer": correct_text
    }

    if current_step == "idle":
        state = {"step": "wait_more", "title": "Викторина", "questions": [question]}
        states[user_id] = state
    else:
        state.setdefault("questions", []).append(question)
        state["step"] = "wait_more"

    save_data(data)
    q_num = len(state["questions"])
    await send(chat_id,
        f"✅ Вопрос {q_num} добавлен!\n"
        f"❓ *{question_text}*\n"
        f"Правильный ответ: *{correct_text}*\n\n"
        f"Отправь ещё опрос или:",
        reply_markup=save_or_more_keyboard()
    )

# ─── QUIZ QUESTION ────────────────────────────────────────────────────────────

async def send_quiz_question(chat_id, user_id, data):
    state = data["user_state"].get(user_id, {})
    if state.get("step") != "in_quiz":
        return

    quiz = state["quiz"]
    idx = state["q_index"]
    total = len(quiz["questions"])

    if idx >= total:
        score = state.get("score", 0)
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
    correct_index = options.index(correct_text)

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

# ─── UTILS ────────────────────────────────────────────────────────────────────

_bot_username = None
async def get_bot_username():
    global _bot_username
    if not _bot_username:
        info = await api("getMe")
        _bot_username = info.get("result", {}).get("username", "")
    return _bot_username

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

async def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан!")
    logger.info("Bot started!")
    await get_bot_username()
    offset = 0
    while True:
        try:
            result = await api("getUpdates", offset=offset, timeout=30,
                allowed_updates=["message", "poll_answer", "poll", "callback_query"])
            if result.get("ok"):
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    data = load_data()
                    try:
                        await handle_update(update, data)
                    except Exception as e:
                        logger.error(f"Error: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
