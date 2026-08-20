import os
import json
import logging
import io
import requests
import qrcode
import threading
import asyncio
from datetime import datetime, timedelta
from flask import Flask
from openai import OpenAI
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from telegram import Bot

# Enable logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATIONS ---
MASTER_TOKEN = "8810526009:AAExxwKEKFCAoT7uk0kpPArpaBD7jNzXHVU"
ADMIN_IDS = ["8975949736"]  # Permanent Admin User ID

# KwikUPI Gateway Credentials (Configured Live)
KWIKUPI_API_KEY = "pk_live_f4ItCmj2Os4L6SoOfCWEmq44"
KWIKUPI_SECRET = "Sk_live_ple3JKmPOzNz2G3dvY1qz9pMO07hgGOb9SKrgKc8toZLUBzA"

# OpenRouter Configuration (Reads securely from Environment Variables)
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

DB_FILE = "user_bots.json"
PREMIUM_DB_FILE = "premium_db.json"
PENDING_PAYMENTS_FILE = "pending_payments.json"

GET_NAME, GET_TOKEN, GET_PROMPT, EDIT_PROMPT, WAITING_PAYMENT_PROOF = range(5)

# Dictionary to hold running child bot background threads
active_child_threads = {}

# --- DATABASE HELPERS ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_premium_db():
    if not os.path.exists(PREMIUM_DB_FILE):
        return {}
    with open(PREMIUM_DB_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_premium_db(data):
    with open(PREMIUM_DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_pending_db():
    if not os.path.exists(PENDING_PAYMENTS_FILE):
        return {}
    with open(PENDING_PAYMENTS_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_pending_db(data):
    with open(PENDING_PAYMENTS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def is_user_premium(user_id: str) -> bool:
    if str(user_id) in ADMIN_IDS:
        return True
    db = load_premium_db()
    if str(user_id) not in db:
        return False
    expiry_str = db[str(user_id)].get("expiry")
    if expiry_str == "lifetime":
        return True
    try:
        expiry_date = datetime.fromisoformat(expiry_str)
        return datetime.now() < expiry_date
    except Exception:
        return False

PREMIUM_PLANS = {
    "plan_1d": {"name": "1 Day", "days": 1, "price": 10.00},
    "plan_7d": {"name": "7 Days", "days": 7, "price": 30.00},
    "plan_28d": {"name": "28 Days", "days": 28, "price": 50.00},
    "plan_84d": {"name": "84 Days", "days": 84, "price": 100.00},
    "plan_168d": {"name": "168 Days", "days": 168, "price": 150.00},
    "plan_365d": {"name": "365 Days", "days": 365, "price": 250.00},
    "plan_lifetime": {"name": "Permanent (Lifetime)", "days": 99999, "price": 499.00},
}

def get_main_keyboard(user_id: str):
    is_prem = is_user_premium(str(user_id))
    prem_label = "⭐ Premium Active" if is_prem else "⭐ Maker Premium Plans"
    keyboard = [
        ["My Bots", "Create New Bot"],
        ["🔄 Change Bot Prompt", prem_label],
        ["💎 Premium Benefits", "Account"],
        ["Help"]
    ]
    if str(user_id) in ADMIN_IDS:
        keyboard.append(["🛠️ Admin Control Panel"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# --- FLASK SERVER (Required for Render Web Services) ---
app = Flask(__name__)

@app.route('/')
def index():
    return "Master Bot & Child Bots are running smoothly on Render!", 200

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# --- DYNAMIC CHILD BOT RUNNER (Thread-Safe Isolation) ---
def run_child_bot_process(bot_token: str, prompt_text: str, owner_id: str):
    """Runs a child bot instance inside its own dedicated thread and event loop safely."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        child_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        )

        child_app = Application.builder().token(bot_token).build()

        async def child_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_text = update.message.text
            try:
                response = child_client.chat.completions.create(
                    model="deepseek/deepseek-chat",
                    messages=[
                        {"role": "system", "content": f"You are a helpful telegram bot operating under these instructions: {prompt_text}. The owner/admin is user ID {owner_id}."},
                        {"role": "user", "content": user_text}
                    ],
                    temperature=0.7
                )
                reply = response.choices[0].message.content
                await update.message.reply_text(reply)
            except Exception as e:
                logger.error(f"Child bot AI error: {e}")
                await update.message.reply_text("⚠️ Sorry, I encountered an error connecting to the AI provider.")

        child_app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text(f"🤖 Bot is active!\n\nInstructions: {prompt_text}")))
        child_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, child_message))

        # stop_signals=None prevents thread crash on set_wakeup_fd, drop_pending_updates avoids conflict errors
        child_app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            stop_signals=None
        )
    except Exception as e:
        logger.error(f"Child bot thread crashed: {e}")

def spawn_child_bot(bot_token, prompt_text, owner_id, bot_key):
    """Spawns or restarts a child bot on an independent background thread."""
    t = threading.Thread(target=run_child_bot_process, args=(bot_token, prompt_text, owner_id), daemon=True)
    t.start()
    active_child_threads[bot_key] = t

# --- TELEGRAM MASTER BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    await update.message.reply_text(
        "🤖 **Welcome to Sandeep's Bot Builder Panel!**\n\n"
        "Choose an option below to manage your bots or explore premium features:",
        reply_markup=get_main_keyboard(user_id)
    )

async def build_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    db = load_db()
    user_bots_count = len(db.get(user_id, {}))
    if user_bots_count >= 1 and not is_user_premium(user_id):
        await update.message.reply_text(
            "🔒 **Free Tier Limit Reached!**\n\n"
            "You already have a deployed bot. Upgrade to **Maker Premium** to deploy unlimited custom bots!",
            reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Let's build your new bot! What would you like to name it?\n*(Type /cancel to abort)*",
        reply_markup=ReplyKeyboardRemove()
    )
    return GET_NAME

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    user_id = str(update.effective_user.id)

    if text == "🛠️ Admin Control Panel":
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ Unauthorized.")
            return ConversationHandler.END
        
        pending_db = load_pending_db()
        await update.message.reply_text(
            f"🛠️ **Admin Control Panel**\n\n"
            f"• Pending Verifications: {len(pending_db)}\n\n"
            f"Select an action below:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 View Pending Approvals", callback_data="admin_view_pending")]
            ])
        )
        return ConversationHandler.END

    if text == "Create New Bot":
        return await build_command(update, context)
    
    elif text == "🔄 Change Bot Prompt":
        db = load_db()
        user_bots = db.get(user_id, {})
        if not user_bots:
            await update.message.reply_text("You don't have any active bots to change instructions for yet!", reply_markup=get_main_keyboard(user_id))
            return ConversationHandler.END
        
        keyboard = []
        for bname in user_bots.keys():
            keyboard.append([InlineKeyboardButton(f"✏️ Re-prompt {bname}", callback_data=f"editbot_{bname}")])
        keyboard.append([InlineKeyboardButton("🔙 Cancel", callback_data="cancel_action")])
        
        await update.message.reply_text("Select which bot you want to update instructions for:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    elif text == "My Bots":
        db = load_db()
        user_bots = db.get(user_id, {})

        if not user_bots:
            await update.message.reply_text(
                "📂 You don't have any deployed bots yet. Click **Create New Bot** or use `/build` to start!",
                reply_markup=get_main_keyboard(user_id)
            )
        else:
            keyboard = []
            for bot_name, info in user_bots.items():
                keyboard.append([
                    InlineKeyboardButton(f"✏️ Edit {bot_name}", callback_data=f"editbot_{bot_name}"),
                    InlineKeyboardButton(f"❌ Delete {bot_name}", callback_data=f"del_{bot_name}")
                ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "📂 **Your Deployed Bots:**\nManage your bots below:",
                reply_markup=reply_markup
            )
        return ConversationHandler.END

    elif text == "💎 Premium Benefits":
        await update.message.reply_text(
            "💎 **Why Upgrade to Maker Premium?**\n\n"
            "Unlock unlimited child bots hosted reliably 24/7 on Render.",
            reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    elif "Premium" in text:
        keyboard = [
            [InlineKeyboardButton("💎 1 Day — ₹10", callback_data="buy_plan_1d")],
            [InlineKeyboardButton("💎 7 Days — ₹30", callback_data="buy_plan_7d")],
            [InlineKeyboardButton("💎 28 Days — ₹50", callback_data="buy_plan_28d")],
            [InlineKeyboardButton("💎 84 Days — ₹100", callback_data="buy_plan_84d")],
            [InlineKeyboardButton("💎 168 Days — ₹150", callback_data="buy_plan_168d")],
            [InlineKeyboardButton("💎 365 Days — ₹250", callback_data="buy_plan_365d")],
            [InlineKeyboardButton("⭐ Permanent (Lifetime) — ₹499", callback_data="buy_plan_lifetime")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        prem_status = "Active ✅" if is_user_premium(user_id) else "Inactive ❌"
        await update.message.reply_text(
            f"⭐ **MAKER PREMIUM PLANS**\n"
            f"📢 Your ID: `{user_id}`\n"
            f"Status: {prem_status}\n\n"
            f"Select your plan below:",
            reply_markup=reply_markup
        )
        return ConversationHandler.END

    elif text == "Account":
        user = update.effective_user
        prem_status = "Active ⭐" if is_user_premium(user_id) else "Free User"
        await update.message.reply_text(
            f"👤 **Account Info**\n\n"
            f"• Name: {user.first_name}\n"
            f"• ID: `{user.id}`\n"
            f"• Tier: {prem_status}",
            reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    elif text == "Help":
        await update.message.reply_text(
            "ℹ️ **Help Desk**\n\n"
            "Click **Create New Bot** or use `/build`, provide your token from @BotFather, and write your prompt.",
            reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    return ConversationHandler.END

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    data = query.data

    if data == "cancel_action":
        await query.message.edit_text("Action cancelled.", reply_markup=None)
        await query.message.reply_text("Choose an option:", reply_markup=get_main_keyboard(user_id))
        return ConversationHandler.END

    elif data == "admin_view_pending":
        if user_id not in ADMIN_IDS:
            return ConversationHandler.END
        pending_db = load_pending_db()
        if not pending_db:
            await query.message.reply_text("📂 No pending payment verifications.")
            return ConversationHandler.END
        
        for order_id, info in list(pending_db.items()):
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
                 InlineKeyboardButton("❌ Reject", callback_data=f"reject_{order_id}")]
            ])
            await query.message.reply_text(
                f"🔔 **Pending Payment**\n"
                f"• Order ID: `{order_id}`\n"
                f"• User ID: `{info['user_id']}`\n"
                f"• Plan: {info['plan_name']} (₹{info['price']})\n"
                f"• Proof: {info.get('proof', 'None')}",
                reply_markup=kb
            )
        return ConversationHandler.END

    elif data.startswith("approve_"):
        if user_id not in ADMIN_IDS:
            return ConversationHandler.END
        order_id = data.replace("approve_", "")
        pending_db = load_pending_db()
        if order_id in pending_db:
            info = pending_db[order_id]
            target_user_id = info["user_id"]
            plan_key = info["plan_key"]
            plan_info = PREMIUM_PLANS[plan_key]

            prem_db = load_premium_db()
            if plan_info["days"] > 10000:
                expiry = "lifetime"
            else:
                current_expiry = datetime.now()
                if target_user_id in prem_db and prem_db[target_user_id].get("expiry") not in [None, "lifetime"]:
                    try:
                        current_expiry = max(datetime.now(), datetime.fromisoformat(prem_db[target_user_id]["expiry"]))
                    except:
                        pass
                expiry = (current_expiry + timedelta(days=plan_info["days"])).isoformat()

            prem_db[target_user_id] = {"expiry": expiry, "plan": plan_info["name"]}
            save_premium_db(prem_db)

            del pending_db[order_id]
            save_pending_db(pending_db)

            await query.edit_message_text(text=f"✅ Approved order `{order_id}` for user `{target_user_id}`.")
            try:
                await context.bot.send_message(
                    chat_id=int(target_user_id),
                    text=f"🎉 **Payment Approved!** Your **{plan_info['name']}** Maker Premium plan is now active.",
                    reply_markup=get_main_keyboard(target_user_id)
                )
            except Exception as e:
                logger.error(f"Failed to notify user {target_user_id}: {e}")
        return ConversationHandler.END

    elif data.startswith("reject_"):
        if user_id not in ADMIN_IDS:
            return ConversationHandler.END
        order_id = data.replace("reject_", "")
        pending_db = load_pending_db()
        if order_id in pending_db:
            target_user_id = pending_db[order_id]["user_id"]
            del pending_db[order_id]
            save_pending_db(pending_db)
            await query.edit_message_text(text=f"❌ Rejected order `{order_id}`.")
            try:
                await context.bot.send_message(
                    chat_id=int(target_user_id),
                    text="❌ **Payment Verification Rejected.** Please try again.",
                    reply_markup=get_main_keyboard(target_user_id)
                )
            except Exception:
                pass
        return ConversationHandler.END

    elif data.startswith("editbot_") or data.startswith("reprompt_"):
        prefix = "editbot_" if data.startswith("editbot_") else "reprompt_"
        bot_name = data.replace(prefix, "")
        context.user_data["editing_bot_name"] = bot_name
        await query.message.reply_text(
            f"✏️ Send the new updated instructions/features for your child bot **{bot_name}**:\n*(Type /cancel to abort)*",
            reply_markup=ReplyKeyboardRemove()
        )
        return EDIT_PROMPT

    elif data.startswith("del_"):
        bot_name = data.replace("del_", "")
        db = load_db()

        if user_id in db and bot_name in db[user_id]:
            bot_key = f"{user_id}_{bot_name}"
            if bot_key in active_child_threads:
                del active_child_threads[bot_key]

            del db[user_id][bot_name]
            save_db(db)

            await query.edit_message_text(text=f"🗑️ Successfully deleted child bot **{bot_name}**.")
        else:
            await query.edit_message_text(text="⚠️ Bot not found or already deleted.")
        return ConversationHandler.END

    elif data.startswith("buy_plan_"):
        plan_key = data.replace("buy_", "")
        if plan_key in PREMIUM_PLANS:
            plan = PREMIUM_PLANS[plan_key]
            order_id = f"ORD_{user_id}_{int(datetime.now().timestamp())}"

            payment_url = None
            try:
                headers = {
                    "X-API-KEY": KWIKUPI_API_KEY,
                    "X-API-SECRET": KWIKUPI_SECRET,
                    "Content-Type": "application/json"
                }
                payload = {
                    "amount": float(plan["price"]),
                    "order_id": order_id,
                    "customer_name": query.from_user.first_name or "User",
                    "customer_email": f"{user_id}@telegram.org"
                }
                res = requests.post("https://kwikupi.com/api/create-payment", json=payload, headers=headers, timeout=10)
                if res.status_code == 200:
                    res_data = res.json()
                    payment_url = res_data.get("payment_url") or res_data.get("upi_uri") or res_data.get("payment_page")
            except Exception as e:
                logger.error(f"KwikUPI API call failed: {e}")

            if not payment_url:
                payment_url = f"upi://pay?pa=merchant@kwikupi&pn=SandeepBotBuilder&am={plan['price']}&cu=INR&tn={order_id}"

            img = qrcode.make(payment_url)
            bio = io.BytesIO()
            img.save(bio, format="PNG")
            bio.seek(0)

            context.user_data["pending_order"] = {
                "order_id": order_id,
                "plan_key": plan_key,
                "plan_name": plan["name"],
                "price": plan["price"]
            }

            await query.message.delete()
            await context.bot.send_photo(
                chat_id=int(user_id),
                photo=bio,
                caption=f"💳 **Payment Gateway**\n\n"
                        f"• Plan: {plan['name']}\n"
                        f"• Amount: **₹{plan['price']}**\n"
                        f"• Order ID: `{order_id}`\n\n"
                        f"1. Scan the QR code.\n"
                        f"2. Pay.\n"
                        f"3. Submit transaction ID/proof below:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 Submit Payment Proof / ID", callback_data="submit_proof")]
                ])
            )
        return ConversationHandler.END

    elif data == "submit_proof":
        await query.message.reply_text(
            "📝 Please reply with your **Transaction ID / UPI Reference Number** or notes for verification:\n*(Type /cancel to abort)*"
        )
        return WAITING_PAYMENT_PROOF

    return ConversationHandler.END

async def receive_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    proof_text = update.message.text or "(Photo/Document sent)"
    order_info = context.user_data.get("pending_order")

    if not order_info:
        await update.message.reply_text("⚠️ No active order found. Please select a plan again.", reply_markup=get_main_keyboard(user_id))
        return ConversationHandler.END

    order_id = order_info["order_id"]
    pending_db = load_pending_db()
    pending_db[order_id] = {
        "user_id": user_id,
        "plan_key": order_info["plan_key"],
        "plan_name": order_info["plan_name"],
        "price": order_info["price"],
        "proof": proof_text
    }
    save_pending_db(pending_db)

    await update.message.reply_text(
        "✅ **Payment Proof Submitted Successfully!**\n\n"
        "Your request has been forwarded to the admin for verification.",
        reply_markup=get_main_keyboard(user_id)
    )

    for admin_id in ADMIN_IDS:
        try:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
                 InlineKeyboardButton("❌ Reject", callback_data=f"reject_{order_id}")]
            ])
            await context.bot.send_message(
                chat_id=int(admin_id),
                text=f"🔔 **New Payment Proof!**\n"
                     f"• User ID: `{user_id}`\n"
                     f"• Order ID: `{order_id}`\n"
                     f"• Plan: {order_info['plan_name']} (₹{order_info['price']})\n"
                     f"• Proof: {proof_text}",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

    return ConversationHandler.END

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["bot_name"] = update.message.text
    await update.message.reply_text("Great! Now, send me the **API Token** you received from @BotFather for this new child bot:\n*(Type /cancel to abort)*")
    return GET_TOKEN

async def get_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token_input = update.message.text.strip()
    try:
        test_bot = Bot(token=token_input)
        bot_info = await test_bot.get_me()
        
        context.user_data["bot_token"] = token_input
        context.user_data["bot_username"] = bot_info.username
        
        await update.message.reply_text(f"✅ **Token Verified!** Connected to @{bot_info.username}\n\n💬 **What do you want your child bot to do?** Type your instructions:\n*(Type /cancel to abort)*")
        return GET_PROMPT
    except Exception:
        await update.message.reply_text("❌ **Invalid Token!** Please send a valid token from @BotFather:\n*(Type /cancel to abort)*")
        return GET_TOKEN

async def save_updated_bot_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    bot_name = context.user_data.get("editing_bot_name")
    new_prompt = update.message.text

    db = load_db()
    if user_id not in db or bot_name not in db[user_id]:
        await update.message.reply_text("❌ Bot not found.", reply_markup=get_main_keyboard(user_id))
        return ConversationHandler.END

    db[user_id][bot_name]["prompt"] = new_prompt
    save_db(db)

    bot_token = db[user_id][bot_name]["token"]
    bot_key = f"{user_id}_{bot_name}"
    spawn_child_bot(bot_token, new_prompt, user_id, bot_key)

    await update.message.reply_text(f"🚀 Successfully updated child bot **{bot_name}** prompt!", reply_markup=get_main_keyboard(user_id))
    return ConversationHandler.END

async def deploy_ai_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    bot_name = context.user_data["bot_name"]
    bot_token = context.user_data["bot_token"]
    bot_username = context.user_data["bot_username"]
    user_prompt = update.message.text

    db = load_db()
    if user_id not in db:
        db[user_id] = {}
    
    db[user_id][bot_name] = {
        "username": bot_username,
        "token": bot_token,
        "prompt": user_prompt
    }
    save_db(db)

    bot_key = f"{user_id}_{bot_name}"
    spawn_child_bot(bot_token, user_prompt, user_id, bot_key)

    await update.message.reply_text(f"🚀 Success! Your custom child bot @{bot_username} is now live 24/7 on Render!", reply_markup=get_main_keyboard(user_id))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    await update.message.reply_text("Action cancelled.", reply_markup=get_main_keyboard(user_id))
    return ConversationHandler.END

def restore_all_bots():
    """Restores and starts all user child bots from database on startup."""
    db = load_db()
    count = 0
    for user_id, bots in db.items():
        for bot_name, info in bots.items():
            token = info.get("token")
            prompt = info.get("prompt")
            if token and prompt:
                bot_key = f"{user_id}_{bot_name}"
                spawn_child_bot(token, prompt, user_id, bot_key)
                count += 1
                logger.info(f"Restored child bot: @{info.get('username', bot_name)}")
    logger.info(f"Successfully restored {count} child bots.")

def main() -> None:
    # Start Flask Web Server for Render
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Background Flask web server started for Render...")

    # Restore existing child bots in isolated background threads
    restore_all_bots()

    application = Application.builder().token(MASTER_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("build", build_command),
            MessageHandler(filters.Regex("^(My Bots|Create New Bot|🔄 Change Bot Prompt|⭐ Maker Premium Plans|⭐ Premium Active|💎 Premium Benefits|Account|Help|🛠️ Admin Control Panel)$"), handle_menu),
            CallbackQueryHandler(handle_callbacks, pattern="^(editbot_|reprompt_)")
        ],
        states={
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_token)],
            GET_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_ai_bot)],
            EDIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_updated_bot_prompt)],
            WAITING_PAYMENT_PROOF: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_payment_proof)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callbacks))

    print("Master bot is running...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
