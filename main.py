import asyncio
import threading
import re
import os
import random
import time
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

# Storage
users_db = set()
user_states = {}
active_tasks = {}
session_cooldowns = {} 
sessions_in_use = set() # Track sessions currently running a task

global_loop = asyncio.new_event_loop()

# --- SESSION ROTATION LOGIC ---

def get_available_session():
    """Finds a session file that isn't on cooldown and isn't currently in use."""
    if not os.path.exists(SESSIONS_DIR):
        os.makedirs(SESSIONS_DIR)
        return None

    session_files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    if not session_files:
        return None

    current_time = time.time()
    for s_file in session_files:
        full_path = os.path.abspath(os.path.join(SESSIONS_DIR, s_file))
        
        # 1. Check if session is currently active/running
        if full_path in sessions_in_use:
            continue
            
        # 2. Check 5-minute cooldown (300 seconds)
        last_used = session_cooldowns.get(full_path, 0)
        if current_time - last_used > 300:
            return full_path
            
    return None

# --- BOT UI ---

def main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("💫Start"), types.KeyboardButton("❌Cancel"))
    if user_id == ADMIN_ID:
        markup.add(types.KeyboardButton("👤Statics"))
    return markup

@bot.message_handler(commands=['start'])
def welcome(message):
    users_db.add(message.from_user.id)
    bot.send_message(message.chat.id, "Welcome!", reply_markup=main_menu(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == "❌Cancel")
def cancel_request(message):
    chat_id = message.chat.id
    if chat_id in active_tasks:
        active_tasks[chat_id].cancel()
        # Cleanup is handled in the 'finally' block of run_automation
        bot.send_message(chat_id, "❌ Request cancelled.")
    else:
        bot.send_message(chat_id, "No active request found.")

@bot.message_handler(func=lambda m: m.text == "💫Start")
def ask_link(message):
    chat_id = message.chat.id
    
    # Check if user already has a running task
    if chat_id in active_tasks:
        bot.send_message(chat_id, "⚠️ Please cancel your current running request for creating a new file download request.")
        return
    
    # Check if any worker (session) is available
    available_session = get_available_session()
    if not available_session:
        bot.send_message(chat_id, "All workers are busy try after 10mintues")
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
        bot.send_message(chat_id, "All workers are busy try after 10mintues")
        return

    # Lock session and start task
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
    # Telethon needs path without .session extension
    session_base = session_path.replace('.session', '')
    client = TelegramClient(session_base, API_ID, API_HASH)
    
    try:
        await client.start()
        bot_entity = await client.get_input_entity(target_bot)
        
        # Phase 1: Verify
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

        # Phase 2: Nudge
        await client(StartBotRequest(bot=bot_entity, peer=bot_entity, start_param=param))
        
        # Phase 3: Monitor & Download
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
        # Cleanup: 1. Disconnect Client, 2. Free user task, 3. Set session cooldown
        await client.disconnect()
        if chat_id in active_tasks:
            del active_tasks[chat_id]
        
        if session_path in sessions_in_use:
            sessions_in_use.remove(session_path)
            
        session_cooldowns[session_path] = time.time() # Start 5m cooldown now

# --- RUN ---

def run_telebot():
    bot.polling(non_stop=True, skip_pending=True)

if __name__ == "__main__":
    print("Multi-Worker System Online.")
    threading.Thread(target=run_telebot, daemon=True).start()
    asyncio.set_event_loop(global_loop)
    global_loop.run_forever()
    
