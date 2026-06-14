import os
import json
import random
import asyncio
import logging
import httpx
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
DATA_FILE = "quizzes.json"

_bot_username = None
_bot_id = None

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"quizzes": {}, "user_state": {}, "active_polls": {}, "group_sessions": {}, "stats": {}}

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
    return await api("sendMessage", **params)

async def answer_callback(callback_id, text=None):
    await api("answerCallbackQuery", callback_query_id=callback_id, text=text)

async def get_bot_username():
    global _bot_username, _bot_id
    if not _bot_username:
        info = await api("getMe")
        _bot_username = info.get("result", {}).get("username", "")
        _bot_id = info.get("result", {}).get("id")
    return _bot_username

async def setup_commands():
    await api("setMyCommands", commands=[
        {"command": "newquiz",    "description": "📝 Создать новый тест"},
        {"command": "myquizzes",  "description": "📚 Мои тесты"},
        {"command": "startquiz",  "description": "▶️ Решить тест"},
        {"command": "deletequiz", "description": "🗑 Удалить тест"},
        {"command": "stop",       "description": "⛔ Остановить текущий тест"},
        {"command": "help",       "description": "❓ Помощь"},
    ])

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "📝 Создать тест"}, {"text": "📚 Мои тесты"}],
            [{"text": "▶️ Решить тест"},  {"text": "🗑 Удалить тест"}],
            [{"text": "⛔ Стоп"},          {"text": "❓ Помощь"}]
        ],
        "resize_keyboard": True,
        "persistent": True
    }

def quiz_list_keyboard(quizzes, user_id, action="show"):
    buttons = []
    for i, q in enumerate(quizzes):
        label = f"📝 {q['title']} ({len(q['questions'])} вопр.)"
        buttons.append([{"text": label, "callback_data": f"{action}:{user_id}:{i}"}])
    return {"inline_keyboard": buttons}

def quiz_card_keyboard(quiz_idx, owner_id, is_owner=True):
    """Кнопки карточки теста — как в @QuizBot"""
    buttons = [
        [{"text": "▶️ Пройти тест", "callback_data": f"solve:{owner_id}:{quiz_idx}"}],
        [{"text": "👥 Отправить в группу", "switch_inline_query_chosen_chat": {
            "query": f"quiz_{owner_id}_{quiz_idx}",
            "allow_group_chats": True,
            "allow_channel_posts": False,
            "allow_bot_chats": False,
            "allow_user_chats": False
        }}],
        [{"text": "🔗 Поделиться", "switch_inline_query_chosen_chat": {
            "query": f"quiz_{owner_id}_{quiz_idx}",
            "allow_group_chats": True,
            "allow_user_chats": True,
            "allow_bot_chats": False,
            "allow_channel_posts": True
        }}],
    ]
    if is_owner:
        buttons.append([{"text": "📊 Статистика", "callback_data": f"stats:{owner_id}:{quiz_idx}"}])
    return {"inline_keyboard": buttons}

def group_join_keyboard(owner_id, quiz_idx, session_id):
    return {"inline_keyboard": [[
        {"text": "✋ Участвовать", "callback_data": f"join:{owner_id}:{quiz_idx}:{session_id}"}
    ]]}

def save_or_more_keyboard():
    return {"inline_keyboard": [
        [{"text": "➕ Добавить ещё вопрос", "callback_data": "more:add"}],
        [{"text": "💾 Сохранить тест",       "callback_data": "more:save"}]
    ]}

# ─── QUIZ CARD ────────────────────────────────────────────────────────────────

async def send_quiz_card(chat_id, quiz, quiz_idx, owner_id, caller_id=None):
    """Отправляет красивую карточку теста"""
    is_owner = (str(caller_id) == str(owner_id)) if caller_id else True
    title = quiz["title"]
    q_count = len(quiz["questions"])
    text = (
        f"🎯 *{title}*\n"
        f"✏️ {q_count} вопросов  •  🕐 30 сек  •  🔀 все\n"
    )
    await send(chat_id, text, reply_markup=quiz_card_keyboard(quiz_idx, owner_id, is_owner=is_owner))

# ─── MAIN HANDLER ─────────────────────────────────────────────────────────────

async def handle_update(update, data):
    if "poll_answer" in update:
        await handle_poll_answer(update["poll_answer"], data)
        return
    if "callback_query" in update:
        await handle_callback(update["callback_query"], data)
        return

    if "inline_query" in update:
        await handle_inline_query(update["inline_query"], data)
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

    # Кнопки постоянного меню
    btn_map = {
        "📝 Создать тест":  "/newquiz",
        "📚 Мои тесты":     "/myquizzes",
        "▶️ Решить тест":   "/startquiz",
        "🗑 Удалить тест":  "/deletequiz",
        "⛔ Стоп":           "/stop",
        "❓ Помощь":         "/help",
    }
    if text in btn_map:
        text = btn_map[text]

    # Команда /start quiz_OWNERID_IDX — переход по ссылке поделиться
    if text.startswith("/start quiz_"):
        parts = text.replace("/start quiz_", "").split("_")
        if len(parts) == 2:
            owner_id_s, quiz_idx_s = parts
            try:
                quiz_idx = int(quiz_idx_s)
                quizzes = data.get("quizzes", {}).get(owner_id_s, [])
                if 0 <= quiz_idx < len(quizzes):
                    quiz = quizzes[quiz_idx]
                    await send_quiz_card(chat_id, quiz, quiz_idx, owner_id_s, caller_id=user_id)
                    return
            except Exception:
                pass
        await send(chat_id, "❌ Тест не найден.")
        return

    if text in ("/start", ) or text.startswith("/start "):
        states[user_id] = {"step": "idle"}
        save_data(data)
        await send(chat_id,
            "👋 Привет! Я бот для викторин.\n\nИспользуй кнопки меню внизу 👇",
            reply_markup=main_menu_keyboard()
        )
        return

    if text == "/help":
        await send(chat_id,
            "📖 *Как пользоваться:*\n\n"
            "1️⃣ /newquiz — создать тест вручную\n"
            "📎 Или отправь опрос-*Викторину* прямо сюда!\n\n"
            "2️⃣ /myquizzes — список твоих тестов\n"
            "3️⃣ /startquiz — решить тест\n"
            "4️⃣ /stop — остановить текущий тест\n\n"
            "✅ Правильный ответ всегда сохраняется корректно!"
        )
        return

    if text == "/stop":
        if current == "in_quiz":
            states[user_id] = {"step": "idle"}
            save_data(data)
            await send(chat_id, "⛔ Тест остановлен.", reply_markup=main_menu_keyboard())
        else:
            await send(chat_id, "ℹ️ Нет активного теста.")
        return

    if text == "/newquiz":
        states[user_id] = {"step": "wait_title", "questions": []}
        save_data(data)
        await send(chat_id, "📝 *Создание теста*\n\nВведи *название* теста:\n_(или /cancel для отмены)_")
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
            await send(chat_id, "📭 Нет тестов.")
        else:
            await send(chat_id, "🗑 *Какой тест удалить?*",
                reply_markup=quiz_list_keyboard(quizzes, user_id, action="delete"))
        return

    if text == "/settitle":
        if not state.get("questions"):
            await send(chat_id, "❌ Сначала отправь хотя бы один опрос-викторину!")
        else:
            state["step"] = "wait_title_for_poll"
            save_data(data)
            await send(chat_id, "✏️ Введи название для теста:")
        return

    if text == "/cancel":
        states[user_id] = {"step": "idle"}
        save_data(data)
        await send(chat_id, "❌ Отменено.", reply_markup=main_menu_keyboard())
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
            f"✅ Название: *{text}*\n\nОтправь ещё опрос или:",
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
            await send(chat_id, "❌ Нужно минимум 2 варианта:")
            return
        if len(options) > 10:
            await send(chat_id, "❌ Максимум 10 вариантов:")
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
        await send(chat_id, "Используй кнопки 👆 или отправь ещё опрос.", reply_markup=save_or_more_keyboard())
        return

async def show_my_quizzes(chat_id, user_id, data):
    quizzes = data.get("quizzes", {}).get(user_id, [])
    if not quizzes:
        await send(chat_id, "📭 Нет тестов. Создай с /newquiz")
        return
    await send(chat_id, "📚 *Твои тесты:* Выбери один:",
        reply_markup=quiz_list_keyboard(quizzes, user_id, action="show"))

async def show_startquiz(chat_id, user_id, data):
    quizzes = data.get("quizzes", {}).get(user_id, [])
    if not quizzes:
        await send(chat_id, "📭 Нет тестов. Создай с /newquiz")
        return
    await send(chat_id, "🎯 *Выбери тест:*",
        reply_markup=quiz_list_keyboard(quizzes, user_id, action="show"))

# ─── INLINE QUERY ────────────────────────────────────────────────────────────

async def handle_inline_query(inline_query, data):
    """Когда пользователь выбирает чат для отправки теста"""
    query_id = inline_query["id"]
    query_text = inline_query.get("query", "").strip()
    user_id = str(inline_query["from"]["id"])

    if not query_text.startswith("quiz_"):
        await api("answerInlineQuery", inline_query_id=query_id, results=[], cache_time=1)
        return

    parts = query_text.replace("quiz_", "").split("_")
    if len(parts) < 2:
        await api("answerInlineQuery", inline_query_id=query_id, results=[], cache_time=1)
        return

    owner_id = parts[0]
    quiz_idx_str = parts[1]
    try:
        quiz_idx = int(quiz_idx_str)
    except ValueError:
        await api("answerInlineQuery", inline_query_id=query_id, results=[], cache_time=1)
        return

    quizzes = data.get("quizzes", {}).get(owner_id, [])
    if quiz_idx < 0 or quiz_idx >= len(quizzes):
        await api("answerInlineQuery", inline_query_id=query_id, results=[], cache_time=1)
        return

    quiz = quizzes[quiz_idx]
    bot_un = _bot_username or "bot"
    share_url = f"https://t.me/{bot_un}?start=quiz_{owner_id}_{quiz_idx}"
    q_count = len(quiz["questions"])
    title = quiz["title"]

    # Карточка которая отправится в выбранный чат
    result = {
        "type": "article",
        "id": f"quiz_{owner_id}_{quiz_idx}",
        "title": f"🎯 {title}",
        "description": f"{q_count} вопросов • 30 сек на ответ",
        "thumbnail_url": "https://telegram.org/img/t_logo.png",
        "input_message_content": {
            "message_text": "\U0001f3af *" + title + "*\n\u270f\ufe0f " + str(q_count) + " \u0432\u043e\u043f\u0440\u043e\u0441\u043e\u0432  \u2022  \U0001f550 30 \u0441\u0435\u043a\n\n\u041d\u0430\u0436\u043c\u0438 \u043a\u043d\u043e\u043f\u043a\u0443 \u0447\u0442\u043e\u0431\u044b \u043f\u0440\u043e\u0439\u0442\u0438 \u0442\u0435\u0441\u0442! \U0001f447",
            "parse_mode": "Markdown"
        },
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "▶️ Пройти тест", "url": share_url}],
                [{"text": "👥 Начать в этой группе", "url": share_url}]
            ]
        }
    }

    await api("answerInlineQuery",
        inline_query_id=query_id,
        results=[result],
        cache_time=10
    )

# ─── CALLBACK ─────────────────────────────────────────────────────────────────

async def handle_callback(cb, data):
    callback_id = cb["id"]
    chat_id = cb["message"]["chat"]["id"]
    caller_id = str(cb["from"]["id"])
    first_name = cb["from"].get("first_name", "Участник")
    cb_data = cb.get("data", "")
    parts = cb_data.split(":")

    await answer_callback(callback_id)

    if cb_data == "more:add":
        state = data.setdefault("user_state", {}).get(caller_id, {})
        state["step"] = "wait_question"
        data["user_state"][caller_id] = state
        save_data(data)
        await send(chat_id, "✏️ Введи текст следующего вопроса:")
        return

    if cb_data == "more:save":
        state = data.setdefault("user_state", {}).get(caller_id, {})
        await save_quiz_from_state(chat_id, caller_id, state, data)
        return

    if len(parts) < 3:
        return

    action = parts[0]
    owner_id = parts[1]
    quiz_idx_str = parts[2]

    try:
        quiz_idx = int(quiz_idx_str)
    except ValueError:
        return

    quizzes = data.get("quizzes", {}).get(owner_id, [])
    if quiz_idx < 0 or quiz_idx >= len(quizzes):
        await send(chat_id, "❌ Тест не найден.")
        return
    quiz = quizzes[quiz_idx]

    if action == "show":
        await send_quiz_card(chat_id, quiz, quiz_idx, owner_id, caller_id=caller_id)
        return

    if action == "solve":
        # Запуск теста в личке
        is_group = cb["message"]["chat"]["type"] in ("group", "supergroup")
        if is_group:
            # В группе — создаём сессию ожидания участников
            session_id = str(int(time.time()))
            sessions = data.setdefault("group_sessions", {})
            session_key = f"{chat_id}_{session_id}"
            sessions[session_key] = {
                "chat_id": chat_id,
                "owner_id": owner_id,
                "quiz_idx": quiz_idx,
                "quiz": quiz,
                "participants": [{"id": caller_id, "name": first_name}],
                "started": False,
                "q_index": 0,
                "scores": {caller_id: 0}
            }
            save_data(data)
            await send(chat_id,
                f"🎯 *{quiz['title']}*\n"
                f"✏️ {len(quiz['questions'])} вопросов\n\n"
                f"*{first_name}* хочет начать тест!\n"
                f"Нужно минимум *2 участника*.\n\n"
                f"Участники: 1 — {first_name}\n\n"
                f"Нажми кнопку чтобы присоединиться 👇",
                reply_markup=group_join_keyboard(owner_id, quiz_idx, session_id)
            )
        else:
            # В личке — запускаем сразу
            states = data.setdefault("user_state", {})
            states[caller_id] = {
                "step": "in_quiz",
                "quiz": quiz,
                "q_index": 0,
                "score": 0,
                "owner_id": owner_id,
                "quiz_idx": quiz_idx
            }
            save_data(data)
            await send(chat_id,
                f"🚀 *«{quiz['title']}»*\n"
                f"Вопросов: {len(quiz['questions'])}  •  30 сек на ответ\n\n"
                f"_/stop — остановить тест_"
            )
            await send_quiz_question(chat_id, caller_id, data)
        return

    if action == "join":
        # Присоединение к групповой сессии
        if len(parts) < 4:
            return
        session_id = parts[3]
        session_key = f"{chat_id}_{session_id}"
        sessions = data.setdefault("group_sessions", {})
        session = sessions.get(session_key)
        if not session:
            await answer_callback(callback_id, "Сессия не найдена!")
            return
        if session.get("started"):
            await answer_callback(callback_id, "Тест уже начался!")
            return

        # Проверяем не присоединился ли уже
        participants = session["participants"]
        ids = [p["id"] for p in participants]
        if caller_id not in ids:
            participants.append({"id": caller_id, "name": first_name})
            session["scores"][caller_id] = 0

        count = len(participants)
        names = ", ".join(p["name"] for p in participants)
        save_data(data)

        if count >= 2:
            # Достаточно участников — запускаем!
            session["started"] = True
            save_data(data)
            await send(chat_id,
                f"✅ Участники: {names}\n\n🚀 Тест начинается! Вопросов: {len(session['quiz']['questions'])}"
            )
            await asyncio.sleep(2)
            await send_group_quiz_question(chat_id, session_key, data)
        else:
            await send(chat_id,
                f"🎯 *{session['quiz']['title']}*\n\n"
                f"Участники ({count}): {names}\n\n"
                f"Ждём ещё участников... (нужно минимум 2)",
                reply_markup=group_join_keyboard(owner_id, quiz_idx, session_id)
            )
        return

    if action == "togroup":
        bot_un = _bot_username or "bot"
        share_url = f"https://t.me/{bot_un}?start=quiz_{owner_id}_{quiz_idx}"
        await send(chat_id,
            f"👥 *Отправить тест в группу:*\n\n"
            f"1. Добавь @{bot_un} в группу\n"
            f"2. Отправь эту ссылку в группу:\n"
            f"`{share_url}`\n\n"
            f"Или поделись через кнопку 🔗 Поделиться"
        )
        return

    if action == "stats":
        if caller_id != owner_id:
            await send(chat_id, "❌ Статистику видит только автор теста.")
            return
        stats = data.get("stats", {}).get(f"{owner_id}_{quiz_idx}", [])
        if not stats:
            await send(chat_id, f"📊 *Статистика: {quiz['title']}*\n\nПока никто не проходил этот тест.")
            return
        total_q = len(quiz["questions"])
        lines = [f"📊 *Статистика: {quiz['title']}*\nВсего прошли: {len(stats)} чел.\n"]
        for i, entry in enumerate(stats[-20:], 1):  # последние 20
            name = entry.get("name", "Аноним")
            score = entry.get("score", 0)
            pct = int(score / total_q * 100) if total_q else 0
            lines.append(f"{i}. {name}: {score}/{total_q} ({pct}%)")
        await send(chat_id, "\n".join(lines))
        return

    if action == "delete":
        if caller_id != owner_id:
            await send(chat_id, "❌ Удалять может только автор.")
            return
        deleted_title = quiz["title"]
        quizzes.pop(quiz_idx)
        data["quizzes"][owner_id] = quizzes
        save_data(data)
        await send(chat_id, f"🗑 Тест *«{deleted_title}»* удалён!")
        return

async def save_quiz_from_state(chat_id, user_id, state, data):
    questions = state.get("questions", [])
    if not questions:
        await send(chat_id, "❌ Нет вопросов! Добавь хотя бы один.")
        return
    quizzes = data.setdefault("quizzes", {}).setdefault(user_id, [])
    quiz_idx = len(quizzes)
    quiz = {
        "id": f"{user_id}_{quiz_idx}",
        "title": state.get("title", "Викторина"),
        "questions": questions
    }
    quizzes.append(quiz)
    data["user_state"][user_id] = {"step": "idle"}
    save_data(data)
    await send(chat_id,
        f"🎉 Тест *«{quiz['title']}»* сохранён!\nВопросов: {len(quiz['questions'])}",
        reply_markup=quiz_card_keyboard(quiz_idx, user_id, is_owner=True)
    )

# ─── ЛИЧНЫЙ ТЕСТ ──────────────────────────────────────────────────────────────

async def send_quiz_question(chat_id, user_id, data):
    state = data["user_state"].get(user_id, {})
    if state.get("step") != "in_quiz":
        return

    quiz = state["quiz"]
    idx = state["q_index"]
    total = len(quiz["questions"])

    if idx >= total:
        score = state.get("score", 0)
        owner_id = state.get("owner_id", user_id)
        quiz_idx = state.get("quiz_idx", 0)
        user_name = state.get("user_name", "Участник")

        # Сохраняем статистику
        stat_key = f"{owner_id}_{quiz_idx}"
        stats = data.setdefault("stats", {}).setdefault(stat_key, [])
        stats.append({"name": user_name, "score": score, "ts": int(time.time())})

        data["user_state"][user_id] = {"step": "idle"}
        save_data(data)
        pct = int(score / total * 100) if total else 0
        await send(chat_id,
            f"🏁 *Тест завершён!*\n\n"
            f"Результат: *{score}/{total}* ({pct}%) ✅\n"
            f"Спасибо за участие! 🎉",
            reply_markup=main_menu_keyboard()
        )
        return

    q = quiz["questions"][idx]
    options = q["options"].copy()
    correct_text = q["correct_answer"]
    random.shuffle(options)
    correct_index = options.index(correct_text)

    result = await api(
        "sendPoll",
        chat_id=chat_id,
        question=f"❓ Вопрос {idx+1}/{total}: {q['question']}",
        options=options,
        type="quiz",
        correct_option_id=correct_index,
        is_anonymous=False,
        open_period=30
    )

    if result.get("ok"):
        poll_id = result["result"]["poll"]["id"]
        data.setdefault("active_polls", {})[poll_id] = {
            "type": "personal",
            "chat_id": chat_id,
            "user_id": user_id
        }
        save_data(data)

async def handle_poll_answer(poll_answer, data):
    poll_id = poll_answer["poll_id"]
    poll_info = data.get("active_polls", {}).get(poll_id)
    if not poll_info:
        return

    poll_type = poll_info.get("type", "personal")

    if poll_type == "personal":
        user_id = poll_info["user_id"]
        chat_id = poll_info["chat_id"]
        state = data.get("user_state", {}).get(user_id, {})
        if state.get("step") != "in_quiz":
            return
        # Сохраняем имя пользователя
        if "user_name" not in state:
            state["user_name"] = poll_answer.get("user", {}).get("first_name", "Участник")
        del data["active_polls"][poll_id]
        state["q_index"] += 1
        save_data(data)
        await asyncio.sleep(2)
        await send_quiz_question(chat_id, user_id, data)

    elif poll_type == "group":
        session_key = poll_info["session_key"]
        answerer_id = str(poll_answer["user"]["id"])
        sessions = data.get("group_sessions", {})
        session = sessions.get(session_key)
        if not session:
            return
        # Отмечаем что этот участник ответил
        answered = poll_info.setdefault("answered", [])
        if answerer_id not in answered:
            answered.append(answerer_id)
        save_data(data)

# ─── ГРУППОВОЙ ТЕСТ ───────────────────────────────────────────────────────────

async def send_group_quiz_question(chat_id, session_key, data):
    sessions = data.get("group_sessions", {})
    session = sessions.get(session_key)
    if not session or not session.get("started"):
        return

    quiz = session["quiz"]
    idx = session["q_index"]
    total = len(quiz["questions"])

    if idx >= total:
        # Конец — показываем результаты
        scores = session.get("scores", {})
        participants = session["participants"]
        lines = [f"🏁 *Тест завершён! «{quiz['title']}»*\n\n📊 *Результаты:*"]
        sorted_p = sorted(participants, key=lambda p: scores.get(p["id"], 0), reverse=True)
        for i, p in enumerate(sorted_p, 1):
            score = scores.get(p["id"], 0)
            pct = int(score / total * 100)
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {p['name']}: {score}/{total} ({pct}%)")

        # Сохраняем статистику для каждого участника
        owner_id = session["owner_id"]
        quiz_idx = session["quiz_idx"]
        stat_key = f"{owner_id}_{quiz_idx}"
        stats = data.setdefault("stats", {}).setdefault(stat_key, [])
        for p in participants:
            stats.append({
                "name": p["name"],
                "score": scores.get(p["id"], 0),
                "ts": int(time.time())
            })

        del sessions[session_key]
        save_data(data)
        await send(chat_id, "\n".join(lines))
        return

    q = quiz["questions"][idx]
    options = q["options"].copy()
    correct_text = q["correct_answer"]
    random.shuffle(options)
    correct_index = options.index(correct_text)

    result = await api(
        "sendPoll",
        chat_id=chat_id,
        question=f"❓ Вопрос {idx+1}/{total}: {q['question']}",
        options=options,
        type="quiz",
        correct_option_id=correct_index,
        is_anonymous=False,
        open_period=30  # Всегда 30 сек таймер
    )

    if result.get("ok"):
        poll_id = result["result"]["poll"]["id"]
        poll_msg_id = result["result"]["message_id"]
        data.setdefault("active_polls", {})[poll_id] = {
            "type": "group",
            "session_key": session_key,
            "chat_id": chat_id,
            "poll_msg_id": poll_msg_id,
            "answered": []
        }
        session["current_poll_id"] = poll_id
        session["q_index"] += 1
        save_data(data)

        # Ждём 30 секунд (таймер вопроса), потом следующий
        await asyncio.sleep(32)

        # Обновляем данные после сна
        data = load_data()
        sessions = data.get("group_sessions", {})
        session = sessions.get(session_key)
        if not session:
            return

        # Закрываем текущий опрос
        try:
            await api("stopPoll", chat_id=chat_id, message_id=poll_msg_id)
        except Exception:
            pass

        await asyncio.sleep(2)
        await send_group_quiz_question(chat_id, session_key, data)

# ─── ВХОДЯЩИЙ ОПРОС ───────────────────────────────────────────────────────────

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
    question = {"question": question_text, "options": options, "correct_answer": correct_text}

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

# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан!")
    logger.info("Bot started!")
    await get_bot_username()
    await setup_commands()
    offset = 0
    while True:
        try:
            result = await api("getUpdates", offset=offset, timeout=30,
                allowed_updates=["message", "poll_answer", "poll", "callback_query", "inline_query"])
            if result.get("ok"):
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    data = load_data()
                    try:
                        asyncio.create_task(handle_update(update, data))
                    except Exception as e:
                        logger.error(f"Error: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
