import os
import json
import asyncio
from datetime import datetime
from typing import Dict, List, Set
import httpx

# Загружаем токен
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Хранилище данных
quizzes: Dict[str, dict] = {}
user_quizzes: Dict[int, List[str]] = {}
active_group_tests: Dict[str, dict] = {}
temp_quiz_data: Dict[int, dict] = {}

async def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient() as client:
        await client.post(f"{API_URL}/sendMessage", json=payload)

async def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient() as client:
        await client.post(f"{API_URL}/editMessageText", json=payload)

async def answer_callback(callback_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    async with httpx.AsyncClient() as client:
        await client.post(f"{API_URL}/answerCallbackQuery", json=payload)

async def send_message_with_response(chat_id, text, reply_markup=None, parse_mode="HTML"):
    async with httpx.AsyncClient() as client:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        resp = await client.post(f"{API_URL}/sendMessage", json=payload)
        if resp.status_code == 200:
            return resp.json().get("result")
    return None

# ---------- КОМАНДЫ ----------
async def start_command(chat_id, user_id, username):
    """Старое приветственное сообщение как на скриншоте"""
    text = f"""Привет! Я бот для викторин.

- Команды:
  /newquiz — создать викторину
  /myquizzes — мои викторины
  /startquiz — запустить викторину
  /deletequiz — удалить викторину
  /stop — остановить текущий тест
  /help — помощь"""
    
    # Отправляем без клавиатуры (как на скриншоте)
    await send_message(chat_id, text)

async def newquiz_command(chat_id, user_id):
    temp_quiz_data[user_id] = {
        "title": None,
        "questions": [],
        "awaiting_title": True,
        "awaiting_questions": False
    }
    await send_message(chat_id, "📝 Давай создадим новую викторину!\n\nПришли название для теста (например: «Биоэтика 1-10»):")

async def myquizzes_command(chat_id, user_id):
    user_quiz_ids = user_quizzes.get(user_id, [])
    if not user_quiz_ids:
        await send_message(chat_id, "У вас пока нет созданных тестов. Используйте /newquiz")
        return
    
    keyboard = {"inline_keyboard": []}
    for quiz_id in user_quiz_ids[:10]:
        quiz = quizzes.get(quiz_id)
        if quiz:
            keyboard["inline_keyboard"].append([{"text": f"📌 {quiz['title']}", "callback_data": f"manage_{quiz_id}"}])
    
    if not keyboard["inline_keyboard"]:
        await send_message(chat_id, "Тесты не найдены.")
    else:
        await send_message(chat_id, "📋 Ваши тесты (нажмите для управления):", keyboard)

async def help_command(chat_id):
    text = """🤖 <b>Помощь по боту</b>

<b>Как создать тест:</b>
1. Напиши /newquiz
2. Введи название
3. Отправляй <b>опросы (викторины)</b> через скрепку ➕
   — Включи режим «Викторина»
   — Отметь правильный ответ
   — Отправь боту
4. Можно отправить сколько угодно опросов
5. Когда закончишь — напиши «Готово» или «2»

<b>Команды:</b>
/newquiz — создать тест
/myquizzes — мои тесты
/stop — остановить текущий тест

<b>Групповой режим:</b>
• Отправь тест в группу через карточку
• 2+ участников нажимают «Участвовать»
• На вопрос даётся 30 секунд"""
    await send_message(chat_id, text)

async def stop_command(chat_id, user_id):
    for test_id, test in active_group_tests.items():
        if test.get("user_id") == user_id:
            test["active"] = False
            await send_message(chat_id, "⏹ Тест остановлен.")
            return
    
    for test_id, test in active_group_tests.items():
        if test.get("chat_id") == chat_id and test.get("paused", False):
            test["paused"] = False
            await send_message(chat_id, "▶ Тест продолжен!")
            await ask_question(test_id, test)
            return
    
    await send_message(chat_id, "Нет активного теста.")

async def deletequiz_command(chat_id, user_id):
    """Удаление викторины"""
    user_quiz_ids = user_quizzes.get(user_id, [])
    if not user_quiz_ids:
        await send_message(chat_id, "У вас нет созданных тестов.")
        return
    
    keyboard = {"inline_keyboard": []}
    for quiz_id in user_quiz_ids[:10]:
        quiz = quizzes.get(quiz_id)
        if quiz:
            keyboard["inline_keyboard"].append([{"text": f"🗑 {quiz['title']}", "callback_data": f"delete_{quiz_id}"}])
    
    if keyboard["inline_keyboard"]:
        keyboard["inline_keyboard"].append([{"text": "❌ Отмена", "callback_data": "cancel_delete"}])
        await send_message(chat_id, "Выберите тест для удаления:", keyboard)
    else:
        await send_message(chat_id, "Нет тестов для удаления.")

# ---------- ОБРАБОТКА ОПРОСОВ (ГЛАВНОЕ ИСПРАВЛЕНИЕ) ----------
async def handle_poll(chat_id, user_id, poll, message_obj):
    """Обработка опроса-викторины"""
    print(f"DEBUG: Received poll: {poll}")  # Для отладки в логах
    
    # Проверяем, создаёт ли пользователь тест
    if user_id not in temp_quiz_data:
        await send_message(chat_id, "❌ Сначала начни создание теста через /newquiz")
        return
    
    data = temp_quiz_data[user_id]
    
    # Если ждём название — нельзя принимать опрос
    if data.get("awaiting_title"):
        await send_message(chat_id, "❌ Сначала введи название теста!")
        return
    
    # Проверяем, что это викторина
    is_quiz = poll.get("type") == "quiz" or poll.get("is_quiz", False)
    
    if not is_quiz:
        await send_message(chat_id, "❌ Отправь <b>викторину</b>!\n\nКак создать:\n1. Нажми на скрепку ➕\n2. Выбери «Опрос»\n3. Включи переключатель <b>«Викторина»</b>\n4. Отметь правильный ответ\n5. Отправь боту", parse_mode="HTML")
        return
    
    # Извлекаем данные
    question_text = poll.get("question", "")
    options = []
    
    # Варианты ответов могут быть в разных форматах
    if "options" in poll:
        for opt in poll["options"]:
            if isinstance(opt, dict):
                options.append(opt.get("text", ""))
            else:
                options.append(str(opt))
    
    # Правильный ответ
    correct_option_id = poll.get("correct_option_id", -1)
    correct_answer = options[correct_option_id] if 0 <= correct_option_id < len(options) else None
    
    if not question_text or not options or correct_answer is None:
        await send_message(chat_id, "❌ Не удалось распознать опрос. Убедись, что это викторина с отмеченным правильным ответом.")
        return
    
    # Добавляем вопрос
    data["questions"].append({
        "text": question_text,
        "options": options,
        "correct": correct_answer
    })
    
    total = len(data["questions"])
    await send_message(chat_id, f"✅ Вопрос добавлен! (всего в тесте: {total})\n\nОтправляй следующий опрос или напиши «Готово» для сохранения.")

# ---------- ОБРАБОТКА ТЕКСТОВОГО СОЗДАНИЯ ----------
async def handle_quiz_creation(user_id, chat_id, text, message_obj=None):
    if user_id not in temp_quiz_data:
        return False
    
    data = temp_quiz_data[user_id]
    
    # Ожидаем название
    if data.get("awaiting_title"):
        data["title"] = text
        data["awaiting_title"] = False
        data["awaiting_questions"] = True
        await send_message(chat_id, f"✅ Название: «{text}»\n\nТеперь отправляй <b>опросы (викторины)</b> через скрепку ➕.\n\nКогда закончишь — напиши «Готово» или «2»", parse_mode="HTML")
        return True
    
    # Проверяем команду сохранения
    if text.lower() in ["2", "готово", "save", "done", "сохранить"]:
        if len(data["questions"]) == 0:
            await send_message(chat_id, "❌ Добавь хотя бы один вопрос (опрос)!")
            return True
        
        quiz_id = f"quiz_{int(datetime.now().timestamp())}"
        quizzes[quiz_id] = {
            "title": data["title"],
            "questions": data["questions"],
            "author_id": user_id,
            "author_name": message_obj.get("from", {}).get("username", str(user_id)) if message_obj else str(user_id),
            "created_at": datetime.now().isoformat()
        }
        
        if user_id not in user_quizzes:
            user_quizzes[user_id] = []
        user_quizzes[user_id].append(quiz_id)
        
        del temp_quiz_data[user_id]
        await send_message(chat_id, f"✅ Тест «{data['title']}» создан! Добавлено вопросов: {len(data['questions'])}\n\nИспользуй /myquizzes чтобы управлять.")
        return True
    
    return False

# ---------- ГРУППОВОЙ ТЕСТ ----------
async def start_group_quiz(chat_id, quiz_id, user_id):
    quiz = quizzes.get(quiz_id)
    if not quiz:
        await send_message(chat_id, "❌ Тест не найден")
        return
    
    test_id = f"{chat_id}_{quiz_id}"
    
    if test_id in active_group_tests and active_group_tests[test_id].get("active"):
        await send_message(chat_id, "⚠ Тест уже активен в этом чате!")
        return
    
    active_group_tests[test_id] = {
        "chat_id": chat_id,
        "quiz_id": quiz_id,
        "quiz_title": quiz["title"],
        "questions": quiz["questions"],
        "current_q": 0,
        "participants": {},
        "waiting_for_join": True,
        "active": True,
        "no_answer_count": 0,
        "paused": False,
        "user_id": user_id
    }
    
    keyboard = {"inline_keyboard": [[{"text": "✅ Участвовать", "callback_data": f"join_quiz_{test_id}"}]]}
    msg_text = f"🎯 <b>{quiz['title']}</b>\n\nВикторина начинается!\nНажмите «Участвовать», чтобы принять участие.\n\n👥 Для старта нужно минимум 2 участника."
    
    msg = await send_message_with_response(chat_id, msg_text, keyboard)
    if msg:
        active_group_tests[test_id]["join_message_id"] = msg.get("message_id")
        asyncio.create_task(auto_start_quiz(test_id))

async def auto_start_quiz(test_id):
    await asyncio.sleep(30)
    test = active_group_tests.get(test_id)
    if test and test.get("waiting_for_join") and len(test["participants"]) >= 2:
        test["waiting_for_join"] = False
        await send_message(test["chat_id"], "🎬 Начинаем викторину!")
        await ask_question(test_id, test)
    elif test and test.get("waiting_for_join"):
        await send_message(test["chat_id"], "❌ Недостаточно участников. Тест отменён.")
        del active_group_tests[test_id]

async def ask_question(test_id, test):
    if not test.get("active") or test.get("paused"):
        return
    
    q_idx = test["current_q"]
    questions = test["questions"]
    
    if q_idx >= len(questions):
        await end_group_quiz(test_id)
        return
    
    q = questions[q_idx]
    
    keyboard = {"inline_keyboard": []}
    row = []
    for i, opt in enumerate(q["options"]):
        row.append({"text": f"{chr(65+i)}. {opt[:30]}", "callback_data": f"answer_{test_id}_{i}"})
        if len(row) == 2:
            keyboard["inline_keyboard"].append(row)
            row = []
    if row:
        keyboard["inline_keyboard"].append(row)
    
    text = f"📌 <b>{test['quiz_title']}</b>\nВопрос {q_idx+1}/{len(questions)}\n\n{q['text']}\n\n⏱ Время на ответ: 30 секунд"
    
    msg = await send_message_with_response(test["chat_id"], text, keyboard)
    if msg:
        test["current_message_id"] = msg.get("message_id")
        test["answered_users"] = set()
        test["waiting_for_answer"] = True
        test["timer"] = asyncio.create_task(question_timeout(test_id, test))

async def question_timeout(test_id, test):
    await asyncio.sleep(30)
    if not test.get("waiting_for_answer"):
        return
    
    test["waiting_for_answer"] = False
    
    if len(test.get("answered_users", set())) == 0:
        test["no_answer_count"] = test.get("no_answer_count", 0) + 1
        await send_message(test["chat_id"], f"⏰ Никто не ответил на вопрос {test['current_q']+1}!")
        
        if test["no_answer_count"] >= 2:
            test["paused"] = True
            test["active"] = False
            keyboard = {"inline_keyboard": [[{"text": "▶ Продолжить тест", "callback_data": f"resume_{test_id}"}]]}
            await send_message(test["chat_id"], "⏸ Тест приостановлен, так как никто не отвечает.\nНажмите «Продолжить», чтобы возобновить.", keyboard)
            return
    else:
        test["no_answer_count"] = 0
    
    test["current_q"] += 1
    test["waiting_for_answer"] = False
    if test["current_q"] < len(test["questions"]):
        await ask_question(test_id, test)
    else:
        await end_group_quiz(test_id)

async def end_group_quiz(test_id):
    test = active_group_tests.get(test_id)
    if not test:
        return
    
    test["active"] = False
    
    stats = []
    for uid, data in test["participants"].items():
        stats.append((data.get("username", str(uid)), data.get("score", 0), len(test["questions"])))
    
    stats.sort(key=lambda x: x[1], reverse=True)
    
    results_text = f"🏆 <b>Результаты викторины «{test['quiz_title']}»</b>\n\n"
    for i, (name, score, total) in enumerate(stats, 1):
        percent = int(score * 100 / total) if total > 0 else 0
        results_text += f"{i}. {name} — {score}/{total} ({percent}%)\n"
    
    await send_message(test["chat_id"], results_text)
    del active_group_tests[test_id]

async def send_quiz_card(chat_id, quiz_id, quiz_title):
    keyboard = {
        "inline_keyboard": [
            [{"text": "📝 Пройти тест", "callback_data": f"start_quiz_{quiz_id}"}],
            [{"text": "📊 Статистика", "callback_data": f"stats_{quiz_id}"}]
        ]
    }
    text = f"📚 <b>{quiz_title}</b>\n\n🎯 Количество вопросов: {len(quizzes[quiz_id]['questions'])}"
    await send_message(chat_id, text, keyboard)

# ---------- CALLBACK ОБРАБОТЧИКИ ----------
async def handle_callback(callback_data, callback_id, message, user_id, username):
    
    if callback_data.startswith("start_quiz_"):
        quiz_id = callback_data.replace("start_quiz_", "")
        if quiz_id in quizzes:
            await start_group_quiz(message["chat"]["id"], quiz_id, user_id)
            await answer_callback(callback_id)
        else:
            await answer_callback(callback_id, "Тест не найден", True)
        return
    
    if callback_data.startswith("join_quiz_"):
        test_id = callback_data.replace("join_quiz_", "")
        test = active_group_tests.get(test_id)
        if test and test.get("waiting_for_join"):
            if str(user_id) not in test["participants"]:
                test["participants"][str(user_id)] = {"username": username, "score": 0}
                await answer_callback(callback_id, f"✅ {username}, вы участвуете!")
                if len(test["participants"]) >= 2:
                    test["waiting_for_join"] = False
                    await send_message(test["chat_id"], "🎬 Начинаем викторину!")
                    await ask_question(test_id, test)
            else:
                await answer_callback(callback_id, "Вы уже участвуете!")
        else:
            await answer_callback(callback_id, "Набор уже завершён", True)
        return
    
    if callback_data.startswith("answer_"):
        parts = callback_data.split("_")
        if len(parts) >= 4:
            test_id = f"{parts[1]}_{parts[2]}"
            opt_idx = int(parts[3])
            test = active_group_tests.get(test_id)
            if test and test.get("waiting_for_answer"):
                if str(user_id) not in test.get("answered_users", set()):
                    test["answered_users"].add(str(user_id))
                    q_idx = test["current_q"]
                    if q_idx < len(test["questions"]):
                        q = test["questions"][q_idx]
                        is_correct = (q["options"][opt_idx] == q["correct"])
                        if is_correct and str(user_id) in test["participants"]:
                            test["participants"][str(user_id)]["score"] += 1
                            await answer_callback(callback_id, "✅ Верно!")
                        elif not is_correct:
                            await answer_callback(callback_id, f"❌ Неверно. Правильный ответ: {q['correct'][:50]}")
                        else:
                            await answer_callback(callback_id, "❌ Вы не участвуете")
                    else:
                        await answer_callback(callback_id, "Ошибка", True)
                else:
                    await answer_callback(callback_id, "Вы уже отвечали", True)
            else:
                await answer_callback(callback_id, "Время истекло", True)
        return
    
    if callback_data.startswith("resume_"):
        test_id = callback_data.replace("resume_", "")
        test = active_group_tests.get(test_id)
        if test and test.get("paused"):
            test["paused"] = False
            test["active"] = True
            await answer_callback(callback_id, "▶ Тест продолжен!")
            await ask_question(test_id, test)
        else:
            await answer_callback(callback_id, "Тест не на паузе", True)
        return
    
    if callback_data.startswith("manage_"):
        quiz_id = callback_data.replace("manage_", "")
        quiz = quizzes.get(quiz_id)
        if quiz:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "📝 Пройти тест", "callback_data": f"start_quiz_{quiz_id}"}],
                    [{"text": "📤 Отправить в группу", "callback_data": f"send_to_group_{quiz_id}"}],
                    [{"text": "🗑 Удалить", "callback_data": f"delete_{quiz_id}"}]
                ]
            }
            await edit_message(message["chat"]["id"], message["message_id"], f"📌 <b>{quiz['title']}</b>\n\nВопросов: {len(quiz['questions'])}", keyboard)
            await answer_callback(callback_id)
        return
    
    if callback_data.startswith("send_to_group_"):
        quiz_id = callback_data.replace("send_to_group_", "")
        quiz = quizzes.get(quiz_id)
        if quiz:
            await send_quiz_card(message["chat"]["id"], quiz_id, quiz["title"])
            await answer_callback(callback_id, "✅ Карточка теста отправлена!")
        return
    
    if callback_data.startswith("delete_"):
        quiz_id = callback_data.replace("delete_", "")
        if quiz_id in quizzes:
            del quizzes[quiz_id]
            if user_id in user_quizzes:
                user_quizzes[user_id] = [qid for qid in user_quizzes[user_id] if qid != quiz_id]
            await answer_callback(callback_id, "✅ Тест удалён", False)
            await edit_message(message["chat"]["id"], message["message_id"], "🗑 Тест удалён")
        return
    
    if callback_data == "cancel_delete":
        await answer_callback(callback_id, "❌ Удаление отменено")
        await edit_message(message["chat"]["id"], message["message_id"], "Удаление отменено.")
        return

# ---------- ГЛАВНЫЙ ЦИКЛ ----------
async def main():
    print("🤖 Bot started! Waiting for updates...")
    
    offset = 0
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{API_URL}/getUpdates", params={"offset": offset, "timeout": 30})
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            
                            # Обработка сообщений
                            if "message" in update:
                                msg = update["message"]
                                chat_id = msg["chat"]["id"]
                                user_id = msg["from"]["id"]
                                username = msg["from"].get("username", msg["from"].get("first_name", "User"))
                                text = msg.get("text", "")
                                
                                # ВАЖНО: проверяем наличие poll в сообщении
                                if "poll" in msg:
                                    await handle_poll(chat_id, user_id, msg["poll"], msg)
                                    continue
                                
                                # Команды
                                if text == "/start":
                                    await start_command(chat_id, user_id, username)
                                elif text == "/newquiz":
                                    await newquiz_command(chat_id, user_id)
                                elif text == "/myquizzes":
                                    await myquizzes_command(chat_id, user_id)
                                elif text == "/deletequiz":
                                    await deletequiz_command(chat_id, user_id)
                                elif text == "/stop":
                                    await stop_command(chat_id, user_id)
                                elif text == "/help":
                                    await help_command(chat_id)
                                elif user_id in temp_quiz_data:
                                    handled = await handle_quiz_creation(user_id, chat_id, text, msg)
                                    if not handled:
                                        await send_message(chat_id, "✏️ Отправь опрос (викторину) через скрепку ➕\nИли напиши «Готово» для сохранения теста.")
                                else:
                                    await send_message(chat_id, "Неизвестная команда. Используй /help")
                            
                            # Обработка callback'ов
                            elif "callback_query" in update:
                                cb = update["callback_query"]
                                await handle_callback(
                                    cb["data"],
                                    cb["id"],
                                    cb["message"],
                                    cb["from"]["id"],
                                    cb["from"].get("username", "User")
                                )
                else:
                    print(f"API error: {resp.status_code}")
        except Exception as e:
            print(f"Error: {e}")
        
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
