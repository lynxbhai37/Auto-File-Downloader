import asyncio
import threading
import re
import os
import random
import time
import shutil # Added for directory cleanup
from telebot import types
from telethon import TelegramClient, functions
from telethon.tl.functions.messages import StartBotRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import MessageActionChatAddUser

from bot import bot

# Hardcoded Credentials
ADMIN_ID = 7580107255
API_ID = 23639069
API_HASH = "501927ad16760b4e32b1fd76db662250"
SESSIONS_DIR = "./sessions"

# Ensure directory exists
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

# Storage
users_db = set()
user_states = {}
active_tasks = {}
session_cooldowns = {} 
sessions_in_use = set() 

global_loop = asyncio.new_event_loop()

# --- SESSION ROTATION LOGIC ---

def get_available_session():
    """Finds a session file that isn't on cooldown and isn't currently in use."""
    session_files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    if not session_files:
        return None

    current_time = time.time()
    for s_file in session_files:
        full_path = os.path.abspath(os.path.join(SESSIONS_DIR, s_file))
        if full_path in sessions_in_use:
            continue
        last_used = session_cooldowns.get(full_path, 0)
        if current_time - last_used > 300:
            return full_path
    return None

# --- ADMIN KEYBOARDS ---

def admin_session_manager():
    markup = types.InlineKeyboardMarkup()
    btn_upload = types.InlineKeyboardButton("📤 Upload Session", callback_data="admin_upload_session")
    btn_delete = types.InlineKeyboardButton("🗑️ Delete All Sessions", callback_data="admin_delete_all")
    markup.add(btn_upload, btn_delete)
    return markup

# --- BOT UI ---

def main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("💫Start"), types.KeyboardButton("❌Cancel"))
    return markup

@bot.message_handler(commands=['start'])
def welcome(message):
    users_db.add(message.from_user.id)
    first_name = message.from_user.first_name
    welcome_text = f"Welcome dear {first_name}!"
    
    bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu(message.from_user.id))
    
    # If Admin, show the Session Manager inline buttons
    if message.from_user.id == ADMIN_ID:
        bot.send_message(message.chat.id, "🛠️ **Session Manager**", parse_mode="Markdown", reply_markup=admin_session_manager())

# --- ADMIN CALLBACK HANDLERS ---

@bot.callback_query_handler(func=lambda call: call.data == "admin_upload_session")
def trigger_upload(call):
    if call.from_user.id != ADMIN_ID: return
    user_states[call.message.chat.id] = "ADMIN_UPLOADING"
    bot.send_message(call.message.chat.id, "Please send the `.session` file now.")

@bot.callback_query_handler(func=lambda call: call.data == "admin_delete_all")
def delete_sessions(call):
    if call.from_user.id != ADMIN_ID: return
    try:
        count = 0
        for filename in os.listdir(SESSIONS_DIR):
            file_path = os.path.join(SESSIONS_DIR, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
                count += 1
        bot.answer_callback_query(call.id, f"Deleted {count} sessions.")
        bot.send_message(call.message.chat.id, f"✅ Successfully deleted {count} session files.")
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Error: {str(e)}")

# --- DOCUMENT HANDLER (FOR UPLOADING SESSIONS) ---

@bot.message_handler(content_types=['document'], func=lambda m: user_states.get(m.chat.id) == "ADMIN_UPLOADING")
def handle_session_upload(message):
    if message.from_user.id != ADMIN_ID: return
    
    if not message.document.file_name.endswith(".session"):
        bot.send_message(message.chat.id, "❌ Invalid file. Please upload a `.session` file.")
        return

    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    
    file_path = os.path.join(SESSIONS_DIR, message.document.file_name)
    with open(file_path, 'wb') as new_file:
        new_file.write(downloaded_file)
    
    user_states[message.chat.id] = None
    bot.send_message(message.chat.id, f"✅ Session `{message.document.file_name}` uploaded successfully!", parse_mode="Markdown")

# --- USER ACTIONS ---

@bot.message_handler(func=lambda m: m.text == "❌Cancel")
def cancel_request(message):
    chat_id = message.chat.id
    if chat_id in active_tasks:
        active_tasks[chat_id].cancel()
        bot.send_message(chat_id, "❌ Request cancelled.")
    else:
        bot.send_message(chat_id, "No active request found.")

@bot.message_handler(func=lambda m: m.text == "💫Start")
def ask_link(message):
    chat_id = message.chat.id
    if chat_id in active_tasks:
        bot.send_message(chat_id, "⚠️ Please cancel your current running request first.")
        return
    
    available_session = get_available_session()
    if not available_session:
        bot.send_message(chat_id, "All workers are busy try after 10 minutes.")
        return

    user_states[chat_id] = "WAITING_FOR_LINK"
    bot.send_message(chat_id, "Enter Your Bot Link ~")

@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "WAITING_FOR_LINK")
def handle_link(message):
    chat_id = message.chat.id
    link = message.text
    match = re.search(r"t\.me/([\w_]+)\?start=([\w-]+)", link)
    
    if not match:
        bot.send_message(chat_id, "Invalid link format.")
        return

    session_path = get_available_session()
    if not session_path:
        bot.send_message(chat_id, "All workers are busy try after 10 minutes.")
        return

    sessions_in_use.add(session_path)
    bot_username, start_param = match.group(1), match.group(2)
    user_states[chat_id] = None
    
    bot.send_message(chat_id, "Worker Assigned. Processing... Please wait.")
    
    task = asyncio.run_coroutine_threadsafe(
        run_automation(chat_id, bot_username, start_param, session_path), 
        global_loop
    )
    active_tasks[chat_id] = task

# --- AUTOMATION LOGIC ---

async def run_automation(chat_id, target_bot, param, session_path):
    session_base = session_path.replace('.session', '')
    client = TelegramClient(session_base, API_ID, API_HASH)
    
    try:
        await client.start()
        bot_entity = await client.get_input_entity(target_bot)
        
        await client(StartBotRequest(bot=bot_entity, peer=bot_entity, start_param=param))
        await asyncio.sleep(5) 

        messages = await client.get_messages(target_bot, limit=1)
        if messages and messages[0].reply_markup:
            msg = messages[0]
            for row in msg.reply_markup.rows:
                for button in row.buttons:
                    try:
                        if hasattr(button, 'url') and button.url:
                            url = button.url
                            if "t.me/joinchat/" in url or "t.me/+" in url:
                                await client(ImportChatInviteRequest(url.split('/')[-1].replace('+', '')))
                            elif "t.me/" in url:
                                await client(JoinChannelRequest(url.split('/')[-1]))
                            await asyncio.sleep(2)
                        elif hasattr(button, 'data'):
                            await msg.click(data=button.data)
                            await asyncio.sleep(2)
                    except: continue

        bot.send_message(chat_id, "Verification complete. Monitoring for file...")
        await client(StartBotRequest(bot=bot_entity, peer=bot_entity, start_param=param))
        
        found = False
        for _ in range(45): 
            await asyncio.sleep(2)
            new_msgs = await client.get_messages(target_bot, limit=5)
            for m in new_msgs:
                if m.media and not isinstance(m.action, MessageActionChatAddUser):
                    bot.send_message(chat_id, "File found! Downloading...")
                    rand_digit = random.randint(10, 99)
                    new_name = f"SeikaFileDownloader{rand_digit}.py"
                    path = await client.download_media(m, file=new_name)
                    with open(path, 'rb') as f:
                        bot.send_document(chat_id, f, caption="Here Ur File\n👨🏻‍💻Developer ~ @Seikaxlynx")
                    if os.path.exists(path): os.remove(path)
                    found = True
                    break
            if found: break
        
        if not found:
            bot.send_message(chat_id, "File not received. Session ended.")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        bot.send_message(chat_id, f"Error: {str(e)}")
    finally:
        await client.disconnect()
        if chat_id in active_tasks:
            del active_tasks[chat_id]
        if session_path in sessions_in_use:
            sessions_in_use.remove(session_path)
        session_cooldowns[session_path] = time.time()

# --- RUN ---

def run_telebot():
    bot.polling(non_stop=True, skip_pending=True)

if __name__ == "__main__":
    print("Multi-Worker System Online.")
    threading.Thread(target=run_telebot, daemon=True).start()
    asyncio.set_event_loop(global_loop)
    global_loop.run_forever()
