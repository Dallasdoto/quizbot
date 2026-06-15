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
    # Применяем режим перемешивания
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

    # Отменяем предыдущий запущенный таймер если он есть
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
        "open_period": duration  # Запуск колеса таймера у опроса
    }
    if q.get("explanation"):
        poll_data["explanation"] = q["explanation"]

    res = await api_request("sendPoll", poll_data)
    if res.get("ok"):
        poll_id = res["result"]["poll"]["id"]
        session["active_poll_id"] = poll_id
        ACTIVE_POLLS[poll_id] = {"chat_id": user_id, "type": "pm"}
        # Запуск асинхронного таймера на переключение вопроса
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

    # Автопауза при бездействии соло-игрока (2 пропуска подряд)
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
        "active_poll_id": N
