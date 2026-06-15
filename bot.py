import os
import json
import asyncio
import logging
from datetime import datetime
import random
import urllib.parse
import httpx

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_FILE = "quizzes.json"

# Хранилища состояний в реальном времени
USER_STATES = {}         # Добавление тестов: user_id -> state data
ACTIVE_SESSIONS = {}     # Игры в группах: chat_id -> session data
ACTIVE_PM_SESSIONS = {}  # Одиночные игры в ЛС: user_id -> session data
ACTIVE_POLLS = {}        # Быстрый поиск: poll_id -> {"chat_id": id, "type": "group" / "pm"}
BOT_USERNAME = "quizbot"

client = httpx.AsyncClient()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def escape_html(text: str) -> str:
    """Экранирует специальные HTML-символы для предотвращения ошибок парсинга Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def get_voters_text(count: int) -> str:
    """Возвращает грамматически корректное склонение фразы ответивших людей."""
    if count == 0:
        return "Тест пока никто не проходил"
    
    last_digit = count % 10
    last_two = count % 100
    
    if last_two in [11, 12, 13, 14]:
        word = "человек"
        verb = "ответили"
    elif last_digit == 1:
        word = "человек"
        verb = "ответил"
    elif last_digit in [2, 3, 4]:
        word = "человека"
        verb = "ответили"
    else:
        word = "человек"
        verb = "ответили"
        
    return f"{count} {word} {verb}"

def shuffle_question(q):
    """Перемешивает варианты ответов внутри вопроса, сохраняя правильный индекс."""
    options = list(q["options"])
    orig_correct_id = q["correct_option_id"]
    orig_correct_text = options[orig_correct_id] if orig_correct_id < len(options) else ""
    
    random.shuffle(options)
    
    new_correct_id = 0
    if orig_correct_text in options:
        new_correct_id = options.index(orig_correct_text)
        
    return {
        "question": q["question"],
        "options": options,
        "correct_option_id": new_correct_id,
        "explanation": q.get("explanation", "")
    }

def apply_shuffle(questions, shuffle_mode):
    """Применяет выбранный режим перемешивания к списку вопросов."""
    questions_copy = [dict(q) for q in questions]
    
    if shuffle_mode == "none":
        return questions_copy
        
    if shuffle_mode == "questions":
        random.shuffle(questions_copy)
        return questions_copy
        
    if shuffle_mode == "options":
        return [shuffle_question(q) for q in questions_copy]
        
    if shuffle_mode == "all":
        shuffled_qs = [shuffle_question(q) for q in questions_copy]
        random.shuffle(shuffled_qs)
        return shuffled_qs
        
    return questions_copy

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
        logging.info(f"Бот запущен как @{BOT_USERNAME}")

# --- КЛАВИАТУРА И ЛОГИКА ПАГИНАЦИИ «МОИ ТЕСТЫ» ---
def get_pagination_keyboard(current_page: int, total_pages: int):
    keyboard_buttons = []
    
    if current_page > 2:
        keyboard_buttons.append({"text": "« 1", "callback_data": "myquizzes_page_1"})
        
    if current_page > 1:
        keyboard_buttons.append({"text": f"‹ {current_page - 1}", "callback_data": f"myquizzes_page_{current_page - 1}"})
        
    keyboard_buttons.append({"text": f"· {current_page} ·" if total_pages > 1 else f"{current_page}", "callback_data": f"myquizzes_page_{current_page}"})
    
    if current_page < total_pages:
        keyboard_buttons.append({"text": f"{current_page + 1}", "callback_data": f"myquizzes_page_{current_page + 1}"})
        
    if current_page < total_pages - 1:
        keyboard_buttons.append({"text": f"· {total_pages} ·", "callback_data": f"myquizzes_page_{total_pages}"})
        
    inline_keyboard = []
    if total_pages > 1:
        inline_keyboard.append(keyboard_buttons)
        
    inline_keyboard.append([{"text": "Создать новый тест", "callback_data": "cmd_newquiz"}])
    return {"inline_keyboard": inline_keyboard}

async def show_my_quizzes(chat_id, user_id, page=1, edit_message_id=None):
    user_quizzes = {qid: q for qid, q in QUIZZES.items() if str(q.get("creator_id")) == str(user_id)}
    quizzes_list = list(user_quizzes.items())
    
    if not quizzes_list:
        no_test_text = "📭 У вас пока нет созданных тестов. Нажмите кнопку ниже, чтобы создать свой первый тест!"
        keyboard = {
            "inline_keyboard": [[{"text": "Создать новый тест", "callback_data": "cmd_newquiz"}]]
        }
        if edit_message_id:
            await api_request("editMessageText", {
                "chat_id": chat_id,
                "message_id": edit_message_id,
                "text": no_test_text,
                "reply_markup": keyboard
            })
        else:
            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": no_test_text,
                "reply_markup": keyboard
            })
        return

    PAGE_SIZE = 3
    import math
    total_pages = math.ceil(len(quizzes_list) / PAGE_SIZE)
    
    if page < 1: page = 1
    if page > total_pages: page = total_pages
    
    start_idx = (page - 1) * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    page_items = quizzes_list[start_idx:end_idx]
    
    text_lines = ["<b>Ваши тесты</b>\n"]
    for idx, (qid, q) in enumerate(page_items):
        global_idx = start_idx + idx + 1
        num_q = len(q["questions"])
        first_attempts = q.get("first_attempts", {})
        voters_count = len(first_attempts)
        voters_text = get_voters_text(voters_count)
        
        duration = q.get("duration", 30)
        shuffle_mode = q.get("shuffle_mode", "all")
        mode_icons = {"all": "все", "options": "варианты", "questions": "тесты", "none": "нет"}
        mode_text = mode_icons.get(shuffle_mode, "все")

        escaped_title = escape_html(q['title'])
        text_lines.append(
            f"{global_idx}. <b>{escaped_title}</b>  <i>{voters_text}</i>\n"
            f"✒️ {num_q} вопросов  ·  ⏱ {duration} сек  ·  🔀 {mode_text}\n"
            f"/view_{qid}\n"
        )
    
    full_text = "\n".join(text_lines)
    keyboard = get_pagination_keyboard(page, total_pages)
    
    if edit_message_id:
        await api_request("editMessageText", {
            "chat_id": chat_id,
            "message_id": edit_message_id,
            "text": full_text,
            "parse_mode": "HTML",
            "reply_markup": keyboard
        })
    else:
        await api_request("sendMessage", {
            "chat_id": chat_id,
            "text": full_text,
            "parse_mode": "HTML",
            "reply_markup": keyboard
        })

# --- ОДИНОЧНАЯ ИГРА В ЛС (С ТАЙМЕРОМ И АВТОПАУЗОЙ) ---
async def start_pm_quiz(user_id, user_name, quiz_id, quiz_data):
    shuffle_mode = quiz_data.get("shuffle_mode", "all")
    shuffled_questions = apply_shuffle(quiz_data["questions"], shuffle_mode)
    duration = quiz_data.get("duration", 30)
    
    ACTIVE_PM_SESSIONS[user_id] = {
        "quiz_id": quiz_id,
        "title": quiz_data["title"],
        "questions": shuffled_questions,
        "current_index": 0,
        "scores": {"correct": 0, "total": 0},
        "user_name": user_name,
        "duration": duration,
        "unanswered_count": 0,
        "poll_answers_received": 0,
        "active_poll_id": None,
        "status": "RUNNING",
        "timer_task": None
    }
    escaped_title = escape_html(quiz_data['title'])
    await api_request("sendMessage", {
        "chat_id": user_id,
        "text": f"🚀 Начинаем прохождение теста <b>«{escaped_title}»</b> соло. Отвечайте на вопросы ниже!",
        "parse_mode": "HTML"
    })
    await send_next_pm_question(user_id)

async def send_next_pm_question(user_id):
    session = ACTIVE_PM_SESSIONS.get(user_id)
    if not session or session["status"] != "RUNNING":
        return

    if session.get("timer_task"):
        session["timer_task"].cancel()

    if session["current_index"] >= len(session["questions"]):
        await finish_pm_quiz(user_id)
        return

    q_idx = session["current_index"]
    q = session["questions"][q_idx]
    session["poll_answers_received"] = 0
    duration = session.get("duration", 30)

    poll_data = {
        "chat_id": user_id,
        "question": f"[{q_idx + 1}/{len(session['questions'])}] {q['question']}",
        "options": q["options"],
        "type": "quiz",
        "correct_option_id": q["correct_option_id"],
        "is_anonymous": False,
        "open_period": duration
    }
    if q.get("explanation"):
        poll_data["explanation"] = q["explanation"]

    res = await api_request("sendPoll", poll_data)
    if res.get("ok"):
        poll_id = res["result"]["poll"]["id"]
        session["active_poll_id"] = poll_id
        ACTIVE_POLLS[poll_id] = {"chat_id": user_id, "type": "pm"}
        session["timer_task"] = asyncio.create_task(pm_question_timer(user_id, q_idx))

async def pm_question_timer(user_id, question_index):
    session = ACTIVE_PM_SESSIONS.get(user_id)
    duration = session.get("duration", 30) if session else 30
    await asyncio.sleep(duration)
    await handle_pm_question_timeout(user_id, question_index)

async def handle_pm_question_timeout(user_id, question_index):
    session = ACTIVE_PM_SESSIONS.get(user_id)
    if not session or session["status"] != "RUNNING" or session["current_index"] != question_index:
        return

    if session["poll_answers_received"] == 0:
        session["unanswered_count"] += 1
    else:
        session["unanswered_count"] = 0

    if session["unanswered_count"] >= 2:
        session["status"] = "PAUSED"
        pause_keyboard = {
            "inline_keyboard": [[{"text": "▶️ Продолжить тест", "callback_data": f"resume_pm_{user_id}"}]]
        }
        await api_request("sendMessage", {
            "chat_id": user_id,
            "text": "⏸ <b>Тест приостановлен, так как вы бездействовали некоторое время.</b>\nНажмите кнопку ниже, чтобы продолжить.",
            "parse_mode": "HTML",
            "reply_markup": pause_keyboard
        })
        return

    session["current_index"] += 1
    await send_next_pm_question(user_id)

async def finish_pm_quiz(user_id):
    session = ACTIVE_PM_SESSIONS.get(user_id)
    if not session:
        return

    quiz_id = session["quiz_id"]
    title = session["title"]
    correct = session["scores"]["correct"]
    total = session["scores"]["total"]
    percent = int((correct / total) * 100) if total > 0 else 0

    quiz = QUIZZES.get(quiz_id)
    if quiz:
        if "first_attempts" not in quiz:
            quiz["first_attempts"] = {}
        str_user_id = str(user_id)
        if str_user_id not in quiz["first_attempts"]:
            quiz["first_attempts"][str_user_id] = {
                "name": session["user_name"],
                "correct": correct,
                "total": total,
                "date": datetime.now().strftime("%d.%m.%Y %H:%M")
            }
            save_quizzes(QUIZZES)

    escaped_title = escape_html(title)
    result_text = (
        f"🏁 <b>Вы завершили тест «{escaped_title}»!</b>\n\n"
        f"🎯 Ваш результат: <b>{correct}/{total}</b> ({percent}%)\n"
    )
    await api_request("sendMessage", {
        "chat_id": user_id,
        "text": result_text,
        "parse_mode": "HTML"
    })
    
    if session.get("timer_task"):
        session["timer_task"].cancel()
    ACTIVE_PM_SESSIONS.pop(user_id, None)

# --- ЛОГИКА ВИКТОРИНЫ В ГРУППЕ ---
async def start_joining_phase(chat_id, quiz_id, quiz_data):
    if chat_id in ACTIVE_SESSIONS:
        old_session = ACTIVE_SESSIONS[chat_id]
        if old_session.get("timer_task"):
            old_session["timer_task"].cancel()

    shuffle_mode = quiz_data.get("shuffle_mode", "all")
    shuffled_questions = apply_shuffle(quiz_data["questions"], shuffle_mode)
    duration = quiz_data.get("duration", 30)

    ACTIVE_SESSIONS[chat_id] = {
        "quiz_id": quiz_id,
        "title": quiz_data["title"],
        "questions": shuffled_questions,
        "current_index": 0,
        "ready_players": set(),
        "scores": {},
        "unanswered_count": 0,
        "poll_answers_received": 0,
        "active_poll_id": None,
        "status": "JOINING",
        "timer_task": None,
        "intro_message_id": None,
        "duration": duration
    }

    num_q = len(quiz_data["questions"])
    escaped_title = escape_html(quiz_data['title'])
    intro_text = (
        f"🎲 <b>Приготовьтесь пройти тест «{escaped_title}»</b>\n\n"
        f"✒️ <b>{num_q} вопросов</b>\n"
        f"⏱ <b>{duration} секунд на вопрос</b>\n"
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

    if session["current_index"] >= len(session["questions"]):
        await finish_quiz(chat_id)
        return

    q_idx = session["current_index"]
    q = session["questions"][q_idx]
    session["poll_answers_received"] = 0
    duration = session.get("duration", 30)

    poll_data = {
        "chat_id": chat_id,
        "question": f"[{q_idx + 1}/{len(session['questions'])}] {q['question']}",
        "options": q["options"],
        "type": "quiz",
        "correct_option_id": q["correct_option_id"],
        "is_anonymous": False,
        "open_period": duration  
    }
    if q.get("explanation"):
        poll_data["explanation"] = q["explanation"]

    res = await api_request("sendPoll", poll_data)
    if res.get("ok"):
        poll_id = res["result"]["poll"]["id"]
        session["active_poll_id"] = poll_id
        ACTIVE_POLLS[poll_id] = {"chat_id": chat_id, "type": "group"}
        session["timer_task"] = asyncio.create_task(question_timer(chat_id, q_idx))
    else:
        session["current_index"] += 1
        await send_next_question(chat_id)

async def question_timer(chat_id, question_index):
    session = ACTIVE_SESSIONS.get(chat_id)
    duration = session.get("duration", 30) if session else 30
    await asyncio.sleep(duration)
    await handle_question_timeout(chat_id, question_index)

async def handle_question_timeout(chat_id, question_index):
    session = ACTIVE_SESSIONS.get(chat_id)
    if not session or session["status"] != "RUNNING" or session["current_index"] != question_index:
        return

    if session["poll_answers_received"] == 0:
        session["unanswered_count"] += 1
    else:
        session["unanswered_count"] = 0

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

    session["current_index"] += 1
    await send_next_question(chat_id)

async def finish_quiz(chat_id):
    session = ACTIVE_SESSIONS.get(chat_id)
    if not session:
        return

    quiz_id = session["quiz_id"]
    title = session["title"]
    scores = session["scores"]
    total_q = len(session["questions"])

    sorted_scores = sorted(scores.items(), key=lambda x: (x[1]["correct"], -x[1]["total"]), reverse=True)

    escaped_title = escape_html(title)
    result_text = f"🏁 <b>Тест «{escaped_title}» завершен!</b>\n\n📊 <b>Результаты:</b>\n"
    if not sorted_scores:
        result_text += "Никто не принял участие в викторине 😢"
    else:
        medals = ["🥇", "🥈", "🥉"]
        quiz = QUIZZES.get(quiz_id)
        if quiz and "first_attempts" not in quiz:
            quiz["first_attempts"] = {}

        for i, (user_id, data) in enumerate(sorted_scores):
            medal = medals[i] if i < 3 else "🔹"
            percent = int((data["correct"] / total_q) * 100) if total_q > 0 else 0
            escaped_name = escape_html(data["name"])
            result_text += f"{medal} <b>{escaped_name}</b> — {data['correct']}/{total_q} ({percent}%)\n"

            if quiz:
                str_u_id = str(user_id)
                if str_u_id not in quiz["first_attempts"]:
                    quiz["first_attempts"][str_u_id] = {
                        "name": data["name"],
                        "correct": data["correct"],
                        "total": total_q,
                        "date": datetime.now().strftime("%d.%m.%Y %H:%M")
                    }
        
        if quiz:
            save_quizzes(QUIZZES)

    await api_request("sendMessage", {
        "chat_id": chat_id,
        "text": result_text,
        "parse_mode": "HTML"
    })

    if session.get("timer_task"):
        session["timer_task"].cancel()
    ACTIVE_SESSIONS.pop(chat_id, None)

# --- ГЛАВНЫЙ СТАРТОВЫЙ ТЕКСТ (СКРИНШОТ 3) ---
async def send_start_message(chat_id):
    start_text = "С помощью этого бота Вы можете создать тест из нескольких вопросов с правильными ответами."
    start_keyboard = {
        "inline_keyboard": [
            [{"text": "Создать новый тест", "callback_data": "cmd_newquiz"}],
            [{"text": "Мои тесты", "callback_data": "cmd_myquizzes"}],
            [{"text": "Язык: Русский", "callback_data": "cmd_lang"}]
        ]
    }
    
    res_del = await api_request("sendMessage", {
        "chat_id": chat_id,
        "text": "⚙️",
        "reply_markup": {"remove_keyboard": True}
    })
    if res_del.get("ok"):
        msg_id = res_del["result"]["message_id"]
        await api_request("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        
    await api_request("sendMessage", {
        "chat_id": chat_id,
        "text": start_text,
        "parse_mode": "HTML",
        "reply_markup": start_keyboard
    })

# --- ОБРАБОТКА ОБНОВЛЕНИЙ ---
async def handle_update(update):
    # --- ОБРАБОТКА МГНОВЕННОГО INLINE MODE (СКРИНШОТЫ 1 и 2) ---
    if "inline_query" in update:
        inline_query = update["inline_query"]
        iq_id = inline_query["id"]
        user_id = inline_query["from"]["id"]
        query = inline_query.get("query", "").strip()
        
        results = []
        
        # Если юзер нажал "Поделиться" и пришел запрос вида start_ID
        if query.startswith("start_"):
            quiz_id = query.replace("start_", "", 1)
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                num_q = len(quiz["questions"])
                duration = quiz.get("duration", 30)
                escaped_title = escape_html(quiz['title'])
                
                message_text = (
                    f"🎲 <b>Тест «{escaped_title}»</b>\n"
                    f"✒️ {num_q} вопросов  ·  ⏱ {duration} сек"
                )
                
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "Пройти тест", "url": f"https://t.me/{BOT_USERNAME}?start=start_{quiz_id}"}],
                        [{"text": "Отправить в группу", "url": f"https://t.me/{BOT_USERNAME}?startgroup=start_{quiz_id}"}],
                        [{"text": "Поделиться", "switch_inline_query": f"start_{quiz_id}"}]
                    ]
                }
                
                results.append({
                    "type": "article",
                    "id": quiz_id,
                    "title": f"Тест «{quiz['title']}»",
                    "description": f"✒️ {num_q} вопросов  ·  ⏱ {duration} сек",
                    "input_message_content": {
                        "message_text": message_text,
                        "parse_mode": "HTML"
                    },
                    "reply_markup": reply_markup
                })
        else:
            # Обычный вызов бота по юзернейму @bot_username в любом чате (Скриншот 2)
            user_quizzes = {qid: q for qid, q in QUIZZES.items() if str(q.get("creator_id")) == str(user_id)}
            
            # Фильтр поиска по названию теста, если юзер ввёл текст
            filtered_quizzes = []
            for qid, q in user_quizzes.items():
                if not query or query.lower() in q["title"].lower():
                    filtered_quizzes.append((qid, q))
                    
            # 1. Ссылка-кнопка «Создать новый тест» в самый верх
            results.append({
                "type": "article",
                "id": "create_new_quiz_inline",
                "title": "Создать новый тест",
                "description": "Запустить процесс создания нового теста в боте",
                "input_message_content": {
                    "message_text": f"➕ Нажмите на ссылку ниже, чтобы создать новый тест в боте:\n\nhttps://t.me/{BOT_USERNAME}?start=newquiz",
                    "parse_mode": "HTML"
                }
            })
            
            # 2. Вывод результатов тестов
            for qid, q in filtered_quizzes:
                num_q = len(q["questions"])
                duration = q.get("duration", 30)
                escaped_title = escape_html(q['title'])
                
                message_text = (
                    f"🎲 <b>Тест «{escaped_title}»</b>\n"
                    f"✒️ {num_q} вопросов  ·  ⏱ {duration} сек"
                )
                
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "Пройти тест", "url": f"https://t.me/{BOT_USERNAME}?start=start_{qid}"}],
                        [{"text": "Отправить в группу", "url": f"https://t.me/{BOT_USERNAME}?startgroup=start_{qid}"}],
                        [{"text": "Поделиться", "switch_inline_query": f"start_{qid}"}]
                    ]
                }
                
                results.append({
                    "type": "article",
                    "id": qid,
                    "title": q["title"],
                    "description": f"{num_q} вопросов · {duration} сек",
                    "input_message_content": {
                        "message_text": message_text,
                        "parse_mode": "HTML"
                    },
                    "reply_markup": reply_markup
                })
                
        await api_request("answerInlineQuery", {
            "inline_query_id": iq_id,
            "results": results,
            "cache_time": 0,
            "is_personal": True
        })
        return

    if "poll_answer" in update:
        poll_answer = update["poll_answer"]
        poll_id = poll_answer["poll_id"]
        user = poll_answer["user"]
        user_id = user["id"]
        selected_options = poll_answer["option_ids"]

        if poll_id in ACTIVE_POLLS:
            meta = ACTIVE_POLLS[poll_id]
            chat_id = meta["chat_id"]
            game_type = meta["type"]

            if game_type == "pm":
                session = ACTIVE_PM_SESSIONS.get(chat_id)
                if session and session["status"] == "RUNNING":
                    if session.get("timer_task"):
                        session["timer_task"].cancel()
                    
                    session["poll_answers_received"] = 1
                    session["unanswered_count"] = 0

                    curr_idx = session["current_index"]
                    if curr_idx < len(session["questions"]):
                        correct_id = session["questions"][curr_idx]["correct_option_id"]
                        
                        session["scores"]["total"] += 1
                        if selected_options and selected_options[0] == correct_id:
                            session["scores"]["correct"] += 1
                        
                        session["current_index"] += 1
                        await send_next_pm_question(chat_id)

            elif game_type == "group":
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

    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        data = cb["data"]
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        user_name = cb["from"].get("first_name", "Игрок")

        # Стартовые команды
        if data == "cmd_newquiz":
            USER_STATES[user_id] = {"state": "AWAITING_TITLE"}
            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": "📝 Введите название вашей новой викторины:"
            })
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data == "cmd_myquizzes":
            await show_my_quizzes(chat_id, user_id, page=1)
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data.startswith("myquizzes_page_"):
            page_num = int(data.replace("myquizzes_page_", "", 1))
            await show_my_quizzes(chat_id, user_id, page=page_num, edit_message_id=cb["message"]["message_id"])
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data == "cmd_lang":
            await api_request("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "Выбран русский язык 🇷🇺",
                "show_alert": True
            })
            return

        # Присоединиться к тесту в группе
        elif data.startswith("join_"):
            quiz_id = data.replace("join_", "", 1)
            session = ACTIVE_SESSIONS.get(chat_id)
            if session and session["status"] == "JOINING":
                session["ready_players"].add(user_id)
                players_count = len(session["ready_players"])

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

                if players_count >= 2:
                    session["status"] = "RUNNING"
                    await api_request("sendMessage", {"chat_id": chat_id, "text": "🚀 Достаточно игроков! Начинаем тест..."})
                    await asyncio.sleep(2)
                    await send_next_question(chat_id)

            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data.startswith("force_"):
            session = ACTIVE_SESSIONS.get(chat_id)
            if session and session["status"] == "JOINING":
                session["status"] = "RUNNING"
                await api_request("sendMessage", {"chat_id": chat_id, "text": "🚀 Викторина запускается..."})
                await asyncio.sleep(1.5)
                await send_next_question(chat_id)
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Возобновление групповой игры
        elif data.startswith("resume_"):
            target_chat_id = int(data.replace("resume_", "", 1))
            session = ACTIVE_SESSIONS.get(target_chat_id)
            if session and session["status"] == "PAUSED":
                session["status"] = "RUNNING"
                session["unanswered_count"] = 0
                await api_request("sendMessage", {"chat_id": target_chat_id, "text": "▶️ Викторина возобновлена! Подготовка к вопросу..."})
                await asyncio.sleep(2)
                await send_next_question(target_chat_id)
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Возобновление соло-игры в ЛС
        elif data.startswith("resume_pm_"):
            target_user_id = int(data.replace("resume_pm_", "", 1))
            session = ACTIVE_PM_SESSIONS.get(target_user_id)
            if session and session["status"] == "PAUSED":
                session["status"] = "RUNNING"
                session["unanswered_count"] = 0
                await api_request("sendMessage", {
                    "chat_id": target_user_id,
                    "text": "▶️ Тест возобновлен! Следующий вопрос..."
                })
                await asyncio.sleep(2)
                await send_next_pm_question(target_user_id)
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data.startswith("start_pm_"):
            quiz_id = data.replace("start_pm_", "", 1)
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                await start_pm_quiz(chat_id, user_name, quiz_id, quiz)
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Настройка таймера при сохранении
        elif data.startswith("save_dur_"):
            duration = int(data.replace("save_dur_", "", 1))
            
            mode_kbd = {
                "inline_keyboard": [
                    [{"text": "🔀 Перемешать всё", "callback_data": f"save_mode_all_{duration}"}],
                    [{"text": "🔀 Только варианты", "callback_data": f"save_mode_options_{duration}"}],
                    [{"text": "🔀 Только тесты", "callback_data": f"save_mode_questions_{duration}"}],
                    [{"text": "Оставить так", "callback_data": f"save_mode_none_{duration}"}]
                ]
            }
            
            await api_request("editMessageText", {
                "chat_id": chat_id,
                "message_id": cb["message"]["message_id"],
                "text": "⚙️ <b>Как выводить вопросы викторины?</b>\nВыберите режим перемешивания:",
                "parse_mode": "HTML",
                "reply_markup": mode_kbd
            })
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Сохранение викторины
        elif data.startswith("save_mode_"):
            raw_params = data.replace("save_mode_", "", 1)
            mode, duration_str = raw_params.split("_")
            duration = int(duration_str)

            quiz_data = USER_STATES[user_id]["quiz_data"]
            quiz_id = quiz_data["quiz_id"]
            
            QUIZZES[quiz_id] = {
                "title": quiz_data["title"],
                "creator_id": str(user_id),
                "questions": quiz_data["questions"],
                "duration": duration,
                "shuffle_mode": mode
            }
            save_quizzes(QUIZZES)
            USER_STATES.pop(user_id, None)

            mode_names = {
                "all": "Перемешать всё",
                "options": "Только варианты",
                "questions": "Только тесты",
                "none": "Оставить так"
            }
            mode_name = mode_names.get(mode, "Оставить так")

            await api_request("editMessageText", {
                "chat_id": chat_id,
                "message_id": cb["message"]["message_id"],
                "text": (
                    f"🎉 <b>Викторина «{escape_html(quiz_data['title'])}» успешно сохранена!</b>\n\n"
                    f"⏱ Таймер: <b>{duration} сек</b>\n"
                    f"🔀 Режим: <b>{mode_name}</b>\n\n"
                    f"Используйте команду /myquizzes для управления."
                ),
                "parse_mode": "HTML"
            })
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        # Детальная карточка викторины
        elif data.startswith("view_"):
            quiz_id = data.replace("view_", "", 1)
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                num_q = len(quiz["questions"])
                first_attempts = quiz.get("first_attempts", {})
                voters_count = len(first_attempts)
                
                sharing_link = f"t.me/{BOT_USERNAME}?start=start_{quiz_id}"
                group_url = f"https://t.me/{BOT_USERNAME}?startgroup=start_{quiz_id}"

                duration = quiz.get("duration", 30)
                shuffle_mode = quiz.get("shuffle_mode", "all")
                mode_icons = {"all": "все", "options": "варианты", "questions": "тесты", "none": "нет"}
                mode_text = mode_icons.get(shuffle_mode, "все")

                escaped_title = escape_html(quiz['title'])
                info_text = (
                    f"<b>{escaped_title}</b>   {voters_count} человек ответили\n"
                    f"✒️ {num_q} вопросов  ·  ⏱ {duration} сек  ·  🔀 {mode_text}\n\n"
                    f"<b>External sharing link:</b>\n"
                    f"{sharing_link}"
                )
                
                # Кнопка "Поделиться" теперь полностью системная нативная!
                inline_kbd = {
                    "inline_keyboard": [
                        [{"text": "Пройти тест", "callback_data": f"start_pm_{quiz_id}"}],
                        [{"text": "Отправить в группу", "url": group_url}],
                        [{"text": "Поделиться", "switch_inline_query": f"start_{quiz_id}"}],
                        [{"text": "Редактировать", "callback_data": f"edit_menu_{quiz_id}"}],
                        [{"text": "Статистика", "callback_data": f"stats_menu_{quiz_id}"}]
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

        elif data.startswith("edit_menu_"):
            quiz_id = data.replace("edit_menu_", "", 1)
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                escaped_title = escape_html(quiz['title'])
                edit_text = (
                    f"⚙️ <b>Редактирование викторины «{escaped_title}»</b>\n\n"
                    f"Выберите необходимое действие:"
                )
                inline_kbd = {
                    "inline_keyboard": [
                        [{"text": "➕ Добавить еще вопросы", "callback_data": f"edit_add_{quiz_id}"}],
                        [{"text": "🗑 Стереть вопросы и начать заново", "callback_data": f"edit_clear_{quiz_id}"}],
                        [{"text": "❌ Удалить тест полностью", "callback_data": f"delete_{quiz_id}"}],
                        [{"text": "🔙 Назад", "callback_data": f"view_{quiz_id}"}]
                    ]
                }
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": edit_text,
                    "parse_mode": "HTML",
                    "reply_markup": inline_kbd
                })
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data.startswith("edit_add_"):
            quiz_id = data.replace("edit_add_", "", 1)
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                USER_STATES[user_id] = {
                    "state": "ADDING_QUESTIONS",
                    "quiz_data": quiz
                }
                escaped_title = escape_html(quiz['title'])
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"📥 Пересылайте мне новые опросы. Они будут добавлены в конец теста <b>«{escaped_title}»</b>.\n\nКогда закончите, отправьте /done.",
                    "parse_mode": "HTML"
                })
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data.startswith("edit_clear_"):
            quiz_id = data.replace("edit_clear_", "", 1)
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                quiz["questions"] = []
                save_quizzes(QUIZZES)
                escaped_title = escape_html(quiz['title'])
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"🧹 Все вопросы в тесте «{escaped_title}» были стерты. Вы можете нажать «Добавить еще вопросы» для их повторного заполнения."
                })
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data.startswith("stats_menu_"):
            quiz_id = data.replace("stats_menu_", "", 1)
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                first_attempts = quiz.get("first_attempts", {})
                escaped_title = escape_html(quiz['title'])
                stats_text = f"📈 <b>Статистика прохождений теста «{escaped_title}» (первая попытка):</b>\n\n"
                
                if not first_attempts:
                    stats_text += "Этот тест пока никто не проходил."
                else:
                    sorted_attempts = sorted(first_attempts.values(), key=lambda x: (x["correct"], -x["total"]), reverse=True)
                    for i, player in enumerate(sorted_attempts):
                        medals = ["🥇", "🥈", "🥉"]
                        medal = medals[i] if i < 3 else "🔹"
                        percent = int((player["correct"] / player["total"]) * 100) if player["total"] > 0 else 0
                        escaped_player_name = escape_html(player["name"])
                        stats_text += f"{medal} <b>{escaped_player_name}</b> — {player['correct']}/{player['total']} ({percent}%)  ·  <i>{player.get('date', '')}</i>\n"
                
                inline_kbd = {
                    "inline_keyboard": [[{"text": "🔙 Назад", "callback_data": f"view_{quiz_id}"}]]
                }
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": stats_text,
                    "parse_mode": "HTML",
                    "reply_markup": inline_kbd
                })
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        elif data.startswith("delete_"):
            quiz_id = data.replace("delete_", "", 1)
            if quiz_id in QUIZZES:
                QUIZZES.pop(quiz_id)
                save_quizzes(QUIZZES)
                await api_request("sendMessage", {"chat_id": chat_id, "text": "✅ Тест успешно удален."})
            await api_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        chat_type = msg["chat"]["type"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")

        # --- СЦЕНАРИЙ ДЛЯ ГРУПП ---
        if chat_type in ["group", "supergroup"]:
            if text.startswith("/stop"):
                if chat_id in ACTIVE_SESSIONS:
                    await api_request("sendMessage", {"chat_id": chat_id, "text": "⏹ Тест остановлен администратором."})
                    await finish_quiz(chat_id)
                else:
                    await api_request("sendMessage", {"chat_id": chat_id, "text": "В этом чате сейчас нет активных тестов."})
                return

            if text.startswith("/start start_") or text.startswith(f"/start@{BOT_USERNAME} start_"):
                parts = text.split(" ")
                if len(parts) > 1 and parts[1].startswith("start_"):
                    quiz_id = parts[1].replace("start_", "")
                    if quiz_id in QUIZZES:
                        await start_joining_phase(chat_id, quiz_id, QUIZZES[quiz_id])
                return
            return

        # --- СЦЕНАРИЙ ДЛЯ ЛИЧНЫХ СООБЩЕНИЙ ---
        if chat_type == "private":
            if text == "/stop":
                if chat_id in ACTIVE_PM_SESSIONS:
                    session = ACTIVE_PM_SESSIONS[chat_id]
                    if session.get("timer_task"):
                        session["timer_task"].cancel()
                    ACTIVE_PM_SESSIONS.pop(chat_id, None)
                    await api_request("sendMessage", {
                        "chat_id": chat_id,
                        "text": "⏹ <b>Ваше одиночное прохождение успешно остановлено.</b>",
                        "parse_mode": "HTML"
                    })
                else:
                    await api_request("sendMessage", {
                        "chat_id": chat_id,
                        "text": "У вас нет активной соло-игры."
                    })
                return

        # Поддержка команд /view_ID
        if text.startswith("/view_"):
            quiz_id = text.replace("/view_", "", 1).split("@")[0]
            quiz = QUIZZES.get(quiz_id)
            if quiz:
                num_q = len(quiz["questions"])
                first_attempts = quiz.get("first_attempts", {})
                voters_count = len(first_attempts)
                
                sharing_link = f"t.me/{BOT_USERNAME}?start=start_{quiz_id}"
                group_url = f"https://t.me/{BOT_USERNAME}?startgroup=start_{quiz_id}"

                duration = quiz.get("duration", 30)
                shuffle_mode = quiz.get("shuffle_mode", "all")
                mode_icons = {"all": "все", "options": "варианты", "questions": "тесты", "none": "нет"}
                mode_text = mode_icons.get(shuffle_mode, "все")

                escaped_title = escape_html(quiz['title'])
                info_text = (
                    f"<b>{escaped_title}</b>   {voters_count} человек ответили\n"
                    f"✒️ {num_q} вопросов  ·  ⏱ {duration} сек  ·  🔀 {mode_text}\n\n"
                    f"<b>External sharing link:</b>\n"
                    f"{sharing_link}"
                )
                
                inline_kbd = {
                    "inline_keyboard": [
                        [{"text": "Пройти тест", "callback_data": f"start_pm_{quiz_id}"}],
                        [{"text": "Отправить в группу", "url": group_url}],
                        [{"text": "Поделиться", "switch_inline_query": f"start_{quiz_id}"}],
                        [{"text": "Редактировать", "callback_data": f"edit_menu_{quiz_id}"}],
                        [{"text": "Статистика", "callback_data": f"stats_menu_{quiz_id}"}]
                    ]
                }
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": info_text,
                    "parse_mode": "HTML",
                    "reply_markup": inline_kbd
                })
            else:
                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": "❌ Тест не найден в базе данных."
                })
            return

        if user_id in USER_STATES and USER_STATES[user_id]["state"] == "ADDING_QUESTIONS":
            if text == "/done":
                quiz_data = USER_STATES[user_id]["quiz_data"]
                if not quiz_data["questions"]:
                    await api_request("sendMessage", {
                        "chat_id": chat_id,
                        "text": "❌ В тесте нет вопросов. Добавьте хотя бы один опрос перед завершением."
                    })
                    return

                duration_kbd = {
                    "inline_keyboard": [
                        [{"text": "⏱ 10 сек", "callback_data": "save_dur_10"}],
                        [{"text": "⏱ 15 сек", "callback_data": "save_dur_15"}, {"text": "⏱ 30 сек", "callback_data": "save_dur_30"}],
                        [{"text": "⏱ 45 сек", "callback_data": "save_dur_45"}, {"text": "⏱ 60 сек", "callback_data": "save_dur_60"}]
                    ]
                }

                await api_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": "⚙️ <b>Настройка викторины:</b>\nВыберите время, отводимое на ответ на один вопрос:",
                    "parse_mode": "HTML",
                    "reply_markup": duration_kbd
                })
                return

            if "poll" in msg:
                poll = msg["poll"]
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
                    "text": f"✅ Вопрос #{total_added} добавлен: <i>«{question}»</i>\n\nОтправьте следующий вопрос или отправьте /done для окончания создания.",
                    "parse_mode": "HTML"
                })
                return

        # Текстовые команды в ЛС
        if text.startswith("/start"):
            parts = text.split(" ")
            if len(parts) > 1:
                # Если нажали "Создать новый тест" из инлайн-списка
                if parts[1] == "newquiz":
                    USER_STATES[user_id] = {"state": "AWAITING_TITLE"}
                    await api_request("sendMessage", {
                        "chat_id": chat_id,
                        "text": "📝 Введите название вашей новой викторины:"
                    })
                    return
                # Если перешли по глубокой ссылке
                elif parts[1].startswith("start_"):
                    quiz_id = parts[1].replace("start_", "")
                    quiz = QUIZZES.get(quiz_id)
                    if quiz:
                        num_q = len(quiz["questions"])
                        first_attempts = quiz.get("first_attempts", {})
                        voters_count = len(first_attempts)
                        
                        sharing_link = f"t.me/{BOT_USERNAME}?start=start_{quiz_id}"
                        group_url = f"https://t.me/{BOT_USERNAME}?startgroup=start_{quiz_id}"

                        duration = quiz.get("duration", 30)
                        shuffle_mode = quiz.get("shuffle_mode", "all")
                        mode_icons = {"all": "все", "options": "варианты", "questions": "тесты", "none": "нет"}
                        mode_text = mode_icons.get(shuffle_mode, "все")

                        escaped_title = escape_html(quiz['title'])
                        info_text = (
                            f"<b>{escaped_title}</b>   {voters_count} человек ответили\n"
                            f"✒️ {num_q} вопросов  ·  ⏱ {duration} сек  ·  🔀 {mode_text}\n\n"
                            f"<b>External sharing link:</b>\n"
                            f"{sharing_link}"
                        )
                        
                        inline_kbd = {
                            "inline_keyboard": [
                                [{"text": "Пройти тест", "callback_data": f"start_pm_{quiz_id}"}],
                                [{"text": "Отправить в группу", "url": group_url}],
                                [{"text": "Поделиться", "switch_inline_query": f"start_{quiz_id}"}],
                                [{"text": "Редактировать", "callback_data": f"edit_menu_{quiz_id}"}],
                                [{"text": "Статистика", "callback_data": f"stats_menu_{quiz_id}"}]
                            ]
                        }
                        await api_request("sendMessage", {
                            "chat_id": chat_id,
                            "text": info_text,
                            "parse_mode": "HTML",
                            "reply_markup": inline_kbd
                        })
                        return

            await send_start_message(chat_id)
            return

        elif text == "/newquiz":
            USER_STATES[user_id] = {"state": "AWAITING_TITLE"}
            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": "📝 Введите название вашей новой викторины:"
            })
            return

        elif USER_STATES.get(user_id, {}).get("state") == "AWAITING_TITLE":
            title = text.strip()
            quiz_id = str(int(asyncio.get_event_loop().time() * 1000))
            
            USER_STATES[user_id] = {
                "state": "ADDING_QUESTIONS",
                "quiz_data": {
                    "quiz_id": quiz_id,
                    "title": title,
                    "questions": []
                }
            }

            escaped_title = escape_html(title)
            instruction = (
                f"🌟 <b>Викторина «{escaped_title}» начата!</b>\n\n"
                f"📥 <b>Пакетный импорт включен:</b>\n"
                f"Вы можете переслать мне сразу <b>много опросов (викторин)</b> из любого канала.\n"
                f"Я автоматически разберу их и добавлю в тест.\n\n"
                f"Когда закончите отправку, просто отправьте сообщение с командой /done."
            )
            await api_request("sendMessage", {
                "chat_id": chat_id,
                "text": instruction,
                "parse_mode": "HTML"
            })
            return

        elif text == "/myquizzes":
            await show_my_quizzes(chat_id, user_id, page=1)
            return

        elif text == "/help":
            help_text = (
                "📖 <b>Инструкция по использованию:</b>\n\n"
                "1. Напишите /newquiz, укажите имя теста.\n"
                "2. Перешлите боту опросы в формате «Викторина» (можно пересылать пакетно — сразу много штук).\n"
                "3. Отправьте /done для окончания создания и сохранения.\n"
                "4. Перейдите в раздел /myquizzes, выберите созданный тест и нажмите кнопку <b>«Пройти тест»</b> (для соло-игры в ЛС) или <b>«Отправить в группу»</b>.\n\n"
                "Все вопросы и варианты ответов автоматически перемешиваются при каждом запуске!"
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
                    asyncio.create_task(handle_update(update))
        except Exception as e:
            logging.error(f"Ошибка в цикле запросов: {e}")
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")
