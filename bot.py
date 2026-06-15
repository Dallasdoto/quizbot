import os
import json
import asyncio
import logging
import httpx

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_FILE = "quizzes.json"

# Хранилище сессий и опросов
USER_STATES = {}       # Создание тестов: user_id -> state data
ACTIVE_SESSIONS = {}   # Активные игры в группах: chat_id -> session data
ACTIVE_POLLS = {}      # Быстрый поиск: poll_id -> chat_id
BOT_USERNAME = "quizbot"

# Инициализация HTTP-клиента
client = httpx.AsyncClient()

# --- РАБОТА С БД ---
def load_quizzes():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка загрузки БД: {e}")
            return {}
    return {}

def save_quizzes(data):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"Ошибка сохранения БД: {e}")

QUIZZES = load_quizzes()

# --- ОТПРАВКА ЗАПРОСОВ К TELEGRAM API ---
async def api_request(method: str, data: dict = None):
    url = f"{API_URL}/{method}"
    try:
        response = await client.post(url, json=data, timeout=30.0)
        res_json = response.json()
        if not res_json.get("ok"):
            logging.error(f"Ошибка API ({method}): {res_json}")
        return res_json
    except Exception as e:
        logging.error(f"Исключение при запросе {method}: {e}")
        return {"ok": False}

async def fetch_bot_username():
    global BOT_USERNAME
    res = await api_request("getMe")
    if res.get("ok"):
        BOT_USERNAME = res["result"].get("username", "quizbot")
        logging.info(f"Бот запущен под именем @{BOT_USERNAME}")

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "➕ Создать тест"}, {"text": "📚 Мои тесты"}],
            [{"text": "❓ Помощь"}]
        ],
        "resize_keyboard": True
    }

# --- ЛОГИКА ВИКТОРИНЫ В ГРУППЕ ---
async def start_joining_phase(chat_id, quiz_id, quiz_data):
    # Отмена старой сессии, если была
    if chat_id in ACTIVE_SESSIONS:
        old_session = ACTIVE_SESSIONS[chat_id]
        if old_session.get("timer_task"):
            old_session["timer_task"].cancel()

    ACTIVE_SESSIONS[chat_id] = {
        "quiz_id": quiz_id,
        "title": quiz_data["title"],
        "questions": quiz_data["questions"],
        "current_index": 0,
        "ready_players": set(),
        "scores": {},
        "unanswered_count": 0,
        "poll_answers_received": 0,
        "active_poll_id": None,
        "status": "JOINING",
        "timer_task": None,
        "intro_message_id": None
    }

    num_q = len(quiz_data["questions"])
    intro_text = (
        f"🎲 <b>Приготовьтесь пройти тест «{quiz_data['title']}»</b>\n\n"
        f"✒️ <b>{num_q} вопросов</b>\n"
        f"⏱ <b>30 секунд на вопрос</b>\n"
        f"📰 <b>Ответы видны участникам группы и автору теста</b>\n\n"
        f"🏁 Вопросы появятся, когда хотя бы 2 человека будут готовы отвечать. "
        f"Чтобы остановить тест, отправьте /stop"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✋ Я готов! (0)", "callback_data": f"join_{quiz_id}"},
                {"text": "▶️ Начать сейчас", "callback_data": f"force_{quiz_id}"}
            ]
        ]
    }

    res = await api_request("sendMessage", {
        "chat_id": chat_id,
        "text": intro_text,
        "parse_mode": "HTML",
        "reply_markup": keyboard
    })
    
    if res.get("ok"):
        ACTIVE_SESSIONS[chat_id]["intro_message_id"] = res["result"]["message_id"]

async def send_next_question(chat_id):
    session = ACTIVE_SESSIONS.get(chat_id)
    if not session or session["status"] != "RUNNING":
        return

    # Если вопросы закончились
    if session["current_index"] >= len(session["questions"]):
        await finish_quiz(chat_id)
        return

    q_idx = session["current_index"]
    q = session["questions"][q_idx]
    session["poll_answers_received"] = 0

    # Отправляем опрос с выключенной анонимностью, чтобы собирать статистику
    poll_data = {
        "chat_id": chat_id,
        "question": f"[{q_idx + 1}/{len(session['questions'])}] {q['question']}",
        "options": q["options"],
        "type": "quiz",
        "correct_option_id": q["correct_option_id"],
        "is_anonymous": False,
        "open_period": 30
    }
    if q.get("explanation"):
        poll_data["explanation"] = q["explanation"]

    res = await api_request("sendPoll", poll_data)
    if res.get("ok"):
        poll_id = res["result"]["poll"]["id"]
        session["active_poll_id"] = poll_id
        ACTIVE_POLLS[poll_id] = chat_id
        # Запуск 30-секундного таймера ожидания
        session["timer_task"] = asyncio.create_task(question_timer(chat_id, q_idx))
    else:
        # При ошибке пробуем пропустить вопрос
        session["current_index"] += 1
        await send_next_question(chat_id)

async def question_timer(chat_id, question_index):
    await asyncio.sleep(30)
    await handle_question_timeout(chat_id, question_index)

async def handle_question_timeout(chat_id, question_index):
    session = ACTIVE_SESSIONS.get(chat_id)
    if not session or session["status"] != "RUNNING" or session["current_index"] != question_index:
        return

    # Проверка на активность
    if session["poll_answers_received"] == 0:
        session["unanswered_count"] += 1
    else:
        session["unanswered_count"] = 0

    # Если никто не ответил 2 раза подряд — ставим на паузу
    if session["unanswered_count"] >= 2:
        session["status"] = "PAUSED"
        pause_keyboard = {
            "inline_keyboard": [[{"text": "▶️ Продолжить тест", "callback_data": f"resume_{chat_id}"}]]
        }
        await api_request("sendMessage", {
            "chat_id": chat_id,
            "text": "⏸ <b>Тест приостановлен</b>, так как никто не отвечает на вопросы.\nНажмите кнопку ниже, чтобы продолжить.",
            "parse_mode": "HTML",
            "reply_markup": pause_keyboard
        })
        return

    # Переход к следующему вопросу
    session["current_index"] += 1
    await send_next_question(chat_id)

async def finish_quiz(chat_id):
    session = ACTIVE_SESSIONS.get(chat_id)
    if not session:
        return

    title = session["title"]
    scores = session["scores"]
    total_q = len(session["questions"])

    # Сортировка результатов
    sorted_scores = sorted(scores.items(), key=lambda x: (x[1]["correct"], -x[1]["total"]), reverse=True)

    result_text = f"🏁 <b>Тест «{title}» завершен!</b>\n\n📊 <b>Результаты:</b>\n"
    if not sorted_scores:
        result_text += "Никто не принял участие в викторине 😢"
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, (user_id, data) in enumerate(sorted_scores):
            medal = medals[i] if i < 3 else "🔹"
            percent = int((data["correct"] / total_q) * 100) if total_q > 0 else 0
            result_text += f"{medal} <b>{data['name']}</b> — {data['correct']}/{total_q} ({percent}%)\n"

    await api_request("sendMessage", {
        "chat_id": chat_id,
        "text": result_text,
        "parse_mode": "HTML"
    })

    # Очистка сессии
    if session.get("timer_task"):
        session["timer_task"].cancel()
    ACTIVE_SESSIONS.pop(chat_id, None)

# --- ОБРАБОТКА ОБНОВЛЕНИЙ (UPDATES) ---
async def handle_update(update):
    # 1. Ответы на опросы (Сбор статистики)
    if "poll_answer" in update:
        poll_answer = update["poll_answer"]
        poll_id = poll_answer["poll_id"]
        user = poll_answer["user"]
        user_id = user["id"]
        selected_options = poll_answer["option_ids"]

        if poll_id in ACTIVE_POLLS:
            chat_id = ACTIVE_POLLS[poll_id]
            session = ACTIVE_SESSIONS.get(chat_id)
            if session and session["status"] == "RUNNING":
                session["poll_answers_received"] += 1
                curr_idx = session["current_index"]
                
                if curr_idx < len(session["questions"]):
                    correct_id = session["questions"][curr_idx]["correct_option_id"]
                    
                    user_name = user.get("first_name", "Пользователь")
                    if user.get("last_name"):
                        user_name += f" {user['last_name']}"

                    if user_id not in session["scores"]:
                        session["scores"][user_id] = {"name": user_name, "correct": 0, "total": 0}

                    session["scores"][user_id]["total"] += 1
                    if selected_options and selected_options[0] == correct_id:
                        session["scores"][user_id]["correct"] += 1
        return

    # 2. Нажатия на кнопки (Callback Queries)
    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        data = cb["data"]
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        user_name = cb["from"].get("first_name", "Пользователь")

        # Кнопка готовности
        if data.startswith("join_"):
            quiz_id = data.split("_")[1]
            session = ACTIVE_SESSIONS.get(chat_id)
            if session and session["status"] == "JOINING":
                session["ready_players"].add(user_id)
                players_count = len(session["ready_players"])

                # Обновляем кнопку с новым количеством
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": f"✋ Я готов! ({players_count})", "callback_data": f"join_{quiz_id}"},
                            {"text": "▶️ Начать сейчас", "callback_data": f"force_{quiz_id}"}
                        ]
                    ]
                }
                await api_request("editMessageReplyMarkup", {
                    "chat_id": chat_id,
                    "message_id": session["intro_message_id"],
                    "reply_markup": keyboard
                })

                # Автостарт при 2 участниках
                if players_count >= 2:
                    session["status"] = "RUNNING"
                    await api_request("sendMessage", {"chat_id": chat_id, "text": "🚀 Достаточно игроков! Начинаем тест..."})
                    await asyncio.sleep(2)
                    await send_next_question(chat_id)

            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Принудительный старт викторины
        elif data.startswith("force_"):
            session = ACTIVE_SESSIONS.get(chat_id)
            if session and session["status"] == "JOINING":
                session["status"] = "RUNNING"
                await api_request("sendMessage", {"chat_id": chat_id, "text": "🚀 Викторина запускается вручную..."})
                await asyncio.sleep(1.5)
                await send_next_question(chat_id)
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Возобновление после паузы
        elif data.startswith("resume_"):
            target_chat_id = int(data.split("_")[1])
            session = ACTIVE_SESSIONS.get(target_chat_id)
            if session and session["status"] == "PAUSED":
                session["status"] = "RUNNING"
                session["unanswered_count"] = 0
                await api_request("sendMessage", {"chat_id": target_chat_id, "text": "▶️ Викторина возобновлена! Подготовка к вопросу..."})
                await asyncio.sleep(2)
                await send_next_question(target_chat_id)
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Просмотр параметров теста в ЛС
        elif data.startswith("view_"):
            quiz_id = data.split("_")[1]
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                num_q = len(quiz["questions"])
                info_text = (
                    f"📊 <b>Тест: «{quiz['title']}»</b>\n"
                    f"Количество вопросов: {num_q}\n\n"
                    f"Используйте кнопки ниже для управления:"
                )
                
                # Ссылка на автодобавление и старт в любой группе
                group_url = f"https://t.me/{BOT_USERNAME}?startgroup=start_{quiz_id}"
                
                inline_kbd = {
                    "inline_keyboard": [
                        [{"text": "👥 Отправить в группу", "url": group_url}],
                        [{"text": "🗑 Удалить тест", "callback_data": f"delete_{quiz_id}"}]
                    ]
                }
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": info_text,
                    "parse_mode": "HTML",
                    "reply_markup": inline_kbd
                })
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Удаление теста
        elif data.startswith("delete_"):
            quiz_id = data.split("_")[1]
            if quiz_id in QUIZZES:
                QUIZZES.pop(quiz_id)
                save_quizzes(QUIZZES)
                await api_request("sendMessage", {"chat_id": chat_id, "text": "✅ Тест успешно удален."})
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

    # 3. Текстовые сообщения и опросы
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        chat_type = msg["chat"]["type"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")

        # --- СЦЕНАРИЙ ДЛЯ ГРУПП ---
        if chat_type in ["group", "supergroup"]:
            # Команда остановки теста
            if text.startswith("/stop"):
                if chat_id in ACTIVE_SESSIONS:
                    await api_request("sendMessage", {"chat_id": chat_id, "text": "⏹ Тест остановлен администратором."})
                    await finish_quiz(chat_id)
                else:
                    await api_request("sendMessage", {"chat_id": chat_id, "text": "В этом чате сейчас нет активных тестов."})
                return

            # Логика глубокой ссылки запуска бота /start start_ID
            if text.startswith("/start start_") or text.startswith(f"/start@{BOT_USERNAME} start_"):
                parts = text.split(" ")
                if len(parts) > 1 and parts[1].startswith("start_"):
                    quiz_id = parts[1].replace("start_", "")
                    if quiz_id in QUIZZES:
                        await start_joining_phase(chat_id, quiz_id, QUIZZES[quiz_id])
                    else:
                        await api_request("sendMessage", {"chat_id": chat_id, "text": "❌ Ошибка: Тест не найден в базе данных бота."})
                return
            return

        # --- СЦЕНАРИЙ ДЛЯ ЛИЧНЫХ СООБЩЕНИЙ (ЛС) ---
        # Обработка добавления Опросов при создании теста (Пакетный импорт)
        if user_id in USER_STATES and USER_STATES[user_id]["state"] == "ADDING_QUESTIONS":
            # Нажатие кнопки "Сохранить викторину"
            if text == "💾 Сохранить викторину" or text == "2":
                quiz_data = USER_STATES[user_id]["quiz_data"]
                if not quiz_data["questions"]:
                    await api_request("sendMessage", {
                        "chat_id": chat_id,
                        "text": "❌ Вы не добавили ни одного вопроса! Отправьте хотя бы один опрос.",
                        "reply_markup": get_main_keyboard()
                    })
                    return
                
                # Сохраняем в базу данных
                quiz_id = quiz_data["quiz_id"]
                QUIZZES[quiz_id] = {
                    "title": quiz_data["title"],
                    "creator_id": user_id,
                    "questions": quiz_data["questions"]
                }
                save_quizzes(QUIZZES)
                USER_STATES.pop(user_id)

                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"🎉 <b>Викторина «{quiz_data['title']}» успешно создана!</b>\nВсего вопросов: {len(quiz_data['questions'])}",
                    "parse_mode": "HTML",
                    "reply_markup": get_main_keyboard()
                })
                return

            # Если пользователь пересылает или отправляет опрос напрямую
            if "poll" in msg:
                poll = msg["poll"]
                # Извлекаем данные опроса
                question = poll["question"]
                options = [opt["text"] for opt in poll["options"]]
                correct_id = poll.get("correct_option_id", 0)
                explanation = poll.get("explanation", "")

                quiz_data = USER_STATES[user_id]["quiz_data"]
                quiz_data["questions"].append({
                    "question": question,
                    "options": options,
                    "correct_option_id": correct_id,
                    "explanation": explanation
                })

                total_added = len(quiz_data["questions"])
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"✅ Вопрос #{total_added} добавлен: <i>«{question}»</i>",
                    "parse_mode": "HTML"
                })
                return

        # Обычные текстовые команды
        if text == "/start":
            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": "👋 Привет! Я полностью исправленный бот для проведения викторин, работающий без сбоев.\nИспользуйте кнопки меню для управления.",
                "reply_markup": get_main_keyboard()
            })
            return

        elif text in ["➕ Создать тест", "/newquiz"]:
            USER_STATES[user_id] = {"state": "AWAITING_TITLE"}
            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": "📝 Введите название вашей новой викторины:"
            })
            return

        elif USER_STATES.get(user_id, {}).get("state") == "AWAITING_TITLE":
            # Сохраняем название и переключаем в режим добавления вопросов
            title = text.strip()
            quiz_id = str(int(asyncio.get_event_loop().time() * 1000)) # Уникальный ID
            
            USER_STATES[user_id] = {
                "state": "ADDING_QUESTIONS",
                "quiz_data": {
                    "quiz_id": quiz_id,
                    "title": title,
                    "questions": []
                }
            }

            creation_keyboard = {
                "keyboard": [[{"text": "💾 Сохранить викторину"}]],
                "resize_keyboard": True
            }

            instruction = (
                f"🌟 <b>Викторина «{title}» начата!</b>\n\n"
                f"📥 <b>Пакетный импорт включен:</b>\n"
                f"Вы можете переслать мне сразу <b>много опросов (викторин)</b> из любого другого чата, группы или канала. "
                f"Я последовательно добавлю каждый из них в ваш тест.\n\n"
                f"Как только завершите отправку всех опросов, нажмите кнопку <b>«💾 Сохранить викторину»</b> ниже."
            )
            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": instruction,
                "parse_mode": "HTML",
                "reply_markup": creation_keyboard
            })
            return

        elif text in ["📚 Мои тесты", "/myquizzes"]:
            user_quizzes = {qid: q for qid, q in QUIZZES.items() if q.get("creator_id") == user_id}
            if not user_quizzes:
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": "📭 У вас пока нет созданных тестов. Нажмите «➕ Создать тест», чтобы начать."
                })
                return

            list_text = "📚 <b>Ваши сохраненные викторины:</b>\nНажмите на кнопку теста, чтобы управлять им:"
            inline_kbd = {"inline_keyboard": []}
            for qid, q in user_quizzes.items():
                inline_kbd["inline_keyboard"].append([{"text": q["title"], "callback_data": f"view_{qid}"}])

            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": list_text,
                "parse_mode": "HTML",
                "reply_markup": inline_kbd
            })
            return

        elif text in ["❓ Помощь", "/help"]:
            help_text = (
                "📖 <b>Инструкция по использованию:</b>\n\n"
                "1. Нажмите <b>«➕ Создать тест»</b>, укажите его имя.\n"
                "2. Перешлите боту опросы в формате «Викторина» (можно пересылать пакетно — сразу много штук).\n"
                "3. Нажмите кнопку <b>«💾 Сохранить»</b>.\n"
                "4. Перейдите в <b>«📚 Мои тесты»</b>, выберите созданный тест и нажмите <b>«👥 Отправить в группу»</b>.\n"
                "5. В чате группы появится интерактивная карточка. Игрокам нужно нажать <b>«✋ Я готов!»</b>.\n\n"
                "⏱ Вопросы будут автоматически сменять друг друга каждые 30 секунд. Если игроки пропустят 2 вопроса подряд, викторина автоматически встанет на паузу."
            )
            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": help_text,
                "parse_mode": "HTML"
            })
            return

# --- ГЛАВНЫЙ ЦИКЛ ОПРОСА TELEGRAM (LONG POLLING) ---
async def main():
    await fetch_bot_username()
    offset = 0
    logging.info("Polling запущен...")
    while True:
        try:
            res = await api_request("getUpdates", {"offset": offset, "timeout": 20})
            if res.get("ok"):
                for update in res.get("result", []):
                    offset = update["update_id"] + 1
                    # Обрабатываем асинхронно каждое обновление
                    asyncio.create_task(handle_update(update))
        except Exception as e:
            logging.error(f"Ошибка в цикле запросов: {e}")
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")
