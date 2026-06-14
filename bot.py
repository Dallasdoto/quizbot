import os
import json
import random
import logging
from telegram import Update, Poll
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    PollAnswerHandler, ContextTypes, filters, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
DATA_FILE = "quizzes.json"

# Conversation states
(QUIZ_TITLE, QUESTION_TEXT, ANSWER_OPTIONS, CORRECT_ANSWER, ADD_MORE) = range(5)

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"quizzes": {}, "active_polls": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для викторин.\n\n"
        "📋 *Команды:*\n"
        "/newquiz — создать новую викторину\n"
        "/myquizzes — список моих викторин\n"
        "/startquiz — запустить викторину\n"
        "/deletequiz — удалить викторину\n"
        "/help — помощь\n\n"
        "Начни с /newquiz 🚀",
        parse_mode="Markdown"
    )

# ─── /help ────────────────────────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться ботом:*\n\n"
        "1️⃣ /newquiz — создать викторину\n"
        "   • Введи название викторины\n"
        "   • Добавь вопросы один за другим\n"
        "   • Для каждого вопроса укажи варианты и правильный ответ\n\n"
        "2️⃣ /myquizzes — посмотреть все викторины\n\n"
        "3️⃣ /startquiz — запустить викторину в чате\n\n"
        "4️⃣ /deletequiz — удалить викторину\n\n"
        "✅ Правильный ответ всегда сохраняется корректно!",
        parse_mode="Markdown"
    )

# ─── CREATE QUIZ ──────────────────────────────────────────────────────────────
async def new_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["questions"] = []
    await update.message.reply_text(
        "📝 *Создание новой викторины*\n\n"
        "Введи *название* викторины:\n"
        "(или /cancel для отмены)",
        parse_mode="Markdown"
    )
    return QUIZ_TITLE

async def get_quiz_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("❌ Название не может быть пустым. Попробуй ещё раз:")
        return QUIZ_TITLE
    context.user_data["title"] = title
    await update.message.reply_text(
        f"✅ Название: *{title}*\n\n"
        f"Теперь введи *текст первого вопроса*:",
        parse_mode="Markdown"
    )
    return QUESTION_TEXT

async def get_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["current_question"] = update.message.text.strip()
    context.user_data["current_options"] = []
    await update.message.reply_text(
        "✏️ Введи *варианты ответов* — каждый с новой строки.\n\n"
        "Например:\n"
        "Париж\n"
        "Лондон\n"
        "Берлин\n\n"
        "_(минимум 2, максимум 10 вариантов)_",
        parse_mode="Markdown"
    )
    return ANSWER_OPTIONS

async def get_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    options = [line.strip() for line in raw.split("\n") if line.strip()]
    if len(options) < 2:
        await update.message.reply_text("❌ Нужно минимум 2 варианта. Введи варианты заново:")
        return ANSWER_OPTIONS
    if len(options) > 10:
        await update.message.reply_text("❌ Максимум 10 вариантов. Введи заново:")
        return ANSWER_OPTIONS
    context.user_data["current_options"] = options
    numbered = "\n".join(f"{i+1}. {opt}" for i, opt in enumerate(options))
    await update.message.reply_text(
        f"📋 *Варианты ответов:*\n{numbered}\n\n"
        f"Введи *номер правильного ответа* (1–{len(options)}):",
        parse_mode="Markdown"
    )
    return CORRECT_ANSWER

async def get_correct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    options = context.user_data["current_options"]
    try:
        num = int(text)
        if num < 1 or num > len(options):
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"❌ Введи число от 1 до {len(options)}:")
        return CORRECT_ANSWER

    correct_text = options[num - 1]
    question = {
        "question": context.user_data["current_question"],
        "options": options,
        "correct_answer": correct_text  # Сохраняем ТЕКСТ правильного ответа
    }
    context.user_data["questions"].append(question)
    q_count = len(context.user_data["questions"])
    await update.message.reply_text(
        f"✅ Вопрос {q_count} добавлен!\n"
        f"Правильный ответ: *{correct_text}*\n\n"
        f"Что дальше?",
        parse_mode="Markdown",
        reply_markup=_add_more_keyboard()
    )
    return ADD_MORE

def _add_more_keyboard():
    from telegram import ReplyKeyboardMarkup
    return ReplyKeyboardMarkup(
        [["➕ Добавить вопрос", "💾 Сохранить викторину"]],
        resize_keyboard=True, one_time_keyboard=True
    )

async def add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import ReplyKeyboardRemove
    text = update.message.text.strip()
    if "Добавить" in text:
        await update.message.reply_text(
            "✏️ Введи текст следующего вопроса:",
            reply_markup=ReplyKeyboardRemove()
        )
        return QUESTION_TEXT
    else:
        # Save quiz
        data = load_data()
        user_id = str(update.effective_user.id)
        if user_id not in data["quizzes"]:
            data["quizzes"][user_id] = []
        quiz = {
            "id": f"{user_id}_{len(data['quizzes'][user_id])}",
            "title": context.user_data["title"],
            "questions": context.user_data["questions"]
        }
        data["quizzes"][user_id].append(quiz)
        save_data(data)
        q_count = len(quiz["questions"])
        await update.message.reply_text(
            f"🎉 Викторина *«{quiz['title']}»* сохранена!\n"
            f"📊 Вопросов: {q_count}\n\n"
            f"Запусти её командой /startquiz",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data.clear()
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import ReplyKeyboardRemove
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Отменено.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# ─── LIST QUIZZES ─────────────────────────────────────────────────────────────
async def my_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = str(update.effective_user.id)
    quizzes = data["quizzes"].get(user_id, [])
    if not quizzes:
        await update.message.reply_text("📭 У тебя пока нет викторин. Создай с помощью /newquiz")
        return
    text = "📚 *Твои викторины:*\n\n"
    for i, q in enumerate(quizzes):
        text += f"{i+1}. *{q['title']}* — {len(q['questions'])} вопр.\n"
    text += "\nЗапусти командой /startquiz"
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── START QUIZ ───────────────────────────────────────────────────────────────
async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = str(update.effective_user.id)
    quizzes = data["quizzes"].get(user_id, [])
    if not quizzes:
        await update.message.reply_text("📭 Нет викторин. Создай с /newquiz")
        return
    text = "🎯 *Выбери викторину* — напиши её номер:\n\n"
    for i, q in enumerate(quizzes):
        text += f"{i+1}. {q['title']} ({len(q['questions'])} вопр.)\n"
    await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data["awaiting_quiz_choice"] = True
    context.user_data["quizzes"] = quizzes

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_quiz_choice"):
        return
    text = update.message.text.strip()
    quizzes = context.user_data.get("quizzes", [])
    try:
        num = int(text)
        if num < 1 or num > len(quizzes):
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"❌ Введи число от 1 до {len(quizzes)}:")
        return
    context.user_data["awaiting_quiz_choice"] = False
    quiz = quizzes[num - 1]
    context.user_data["active_quiz"] = quiz
    context.user_data["current_q_index"] = 0
    context.user_data["score"] = 0
    await update.message.reply_text(
        f"🚀 Начинаем викторину *«{quiz['title']}»*!\n"
        f"Всего вопросов: {len(quiz['questions'])}",
        parse_mode="Markdown"
    )
    await send_next_question(update, context)

async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz = context.user_data.get("active_quiz")
    idx = context.user_data.get("current_q_index", 0)
    if idx >= len(quiz["questions"]):
        await finish_quiz(update, context)
        return
    q = quiz["questions"][idx]
    options = q["options"].copy()
    correct_text = q["correct_answer"]

    # Перемешиваем варианты
    random.shuffle(options)

    # Находим индекс правильного ответа ПОСЛЕ перемешивания
    correct_index = options.index(correct_text)

    chat_id = update.effective_chat.id
    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"❓ Вопрос {idx+1}/{len(quiz['questions'])}: {q['question']}",
        options=options,
        type=Poll.QUIZ,
        correct_option_id=correct_index,  # Всегда актуальный индекс
        is_anonymous=False,
        open_period=30
    )
    data = load_data()
    data["active_polls"][msg.poll.id] = {
        "chat_id": chat_id,
        "user_id": str(update.effective_user.id),
        "question_index": idx
    }
    save_data(data)
    context.user_data["current_poll_id"] = msg.poll.id

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id
    data = load_data()
    poll_info = data["active_polls"].get(poll_id)
    if not poll_info:
        return
    # Move to next question after answer
    context.user_data["current_q_index"] = context.user_data.get("current_q_index", 0) + 1
    del data["active_polls"][poll_id]
    save_data(data)
    # We need the update object for chat — get chat_id from poll_info
    # Send next question via bot directly
    quiz = context.user_data.get("active_quiz")
    if quiz:
        idx = context.user_data["current_q_index"]
        if idx >= len(quiz["questions"]):
            score = context.user_data.get("score", 0)
            total = len(quiz["questions"])
            await context.bot.send_message(
                chat_id=poll_info["chat_id"],
                text=f"🏁 *Викторина завершена!*\n\n"
                     f"📊 Результат: {score}/{total}\n"
                     f"Спасибо за участие! 🎉",
                parse_mode="Markdown"
            )
            context.user_data.clear()
        else:
            q = quiz["questions"][idx]
            options = q["options"].copy()
            correct_text = q["correct_answer"]
            random.shuffle(options)
            correct_index = options.index(correct_text)
            msg = await context.bot.send_poll(
                chat_id=poll_info["chat_id"],
                question=f"❓ Вопрос {idx+1}/{total}: {q['question']}",
                options=options,
                type=Poll.QUIZ,
                correct_option_id=correct_index,
                is_anonymous=False,
                open_period=30
            )
            data = load_data()
            data["active_polls"][msg.poll.id] = {
                "chat_id": poll_info["chat_id"],
                "user_id": poll_info["user_id"],
                "question_index": idx
            }
            save_data(data)

async def finish_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz = context.user_data.get("active_quiz", {})
    total = len(quiz.get("questions", []))
    await update.message.reply_text(
        f"🏁 *Викторина завершена!*\n\n"
        f"Вопросов было: {total}\n"
        f"Спасибо за участие! 🎉",
        parse_mode="Markdown"
    )
    context.user_data.clear()

# ─── DELETE QUIZ ──────────────────────────────────────────────────────────────
async def delete_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = str(update.effective_user.id)
    quizzes = data["quizzes"].get(user_id, [])
    if not quizzes:
        await update.message.reply_text("📭 Нет викторин для удаления.")
        return
    text = "🗑 *Какую викторину удалить?* Напиши номер:\n\n"
    for i, q in enumerate(quizzes):
        text += f"{i+1}. {q['title']}\n"
    await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data["awaiting_delete_choice"] = True
    context.user_data["quizzes_for_delete"] = quizzes

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("newquiz", new_quiz)],
        states={
            QUIZ_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_quiz_title)],
            QUESTION_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_question)],
            ANSWER_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_options)],
            CORRECT_ANSWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_correct)],
            ADD_MORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_more)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myquizzes", my_quizzes))
    app.add_handler(CommandHandler("startquiz", start_quiz))
    app.add_handler(CommandHandler("deletequiz", delete_quiz))
    app.add_handler(conv)
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
