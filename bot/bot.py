import os
import logging
import asyncio
import traceback
import html
import json
import tempfile
import pydub
import tools
from pathlib import Path
from datetime import datetime
import azure.cognitiveservices.speech as speechsdk

import PyPDF2


import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode, ChatAction

import config
import database
import openai_utils


# setup
db = database.Database()
logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.info('Start')
user_semaphores = {}
speech_config = speechsdk.SpeechConfig(subscription=config.azure_tts_key, region=config.azure_tts_region)
speech_config.speech_synthesis_voice_name = config.azure_tts_voice
speech_config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Ogg16Khz16BitMonoOpus)
speech_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config)


HELP_MESSAGE = """Commands:
⚪ /retry – Regenerate last bot answer
⚪ /new – Start new dialog
⚪ /mode – Select chat mode
⚪ /balance – Show balance
⚪ /help – Show help
"""


def split_text_into_chunks(text, chunk_size):
  for i in range(0, len(text), chunk_size):
    yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User) -> int:
  if not db.check_if_user_exists(user.id):
    db.add_new_user(
        user.id,
        update.message.chat_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    db.start_new_dialog(user.id)

  if db.get_user_attribute(user.id, "current_dialog_id") is None:
    db.start_new_dialog(user.id)

  if user.id not in user_semaphores:
    user_semaphores[user.id] = asyncio.Semaphore(1)
  db.set_user_attribute(user.id, "last_interaction", datetime.now())
  return user.id


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)

  if user_semaphores[user_id].locked():
    text = "⏳ Please <b>wait</b> for a reply to the previous message"
    await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
    return True
  else:
    return False


async def start_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)

  db.start_new_dialog(user_id)

  reply_text = "Hi! Ich bin <b>Botty</b>  🤖\n\n"
  reply_text += HELP_MESSAGE

  reply_text += "\nUnd los... frag mich was!"

  await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def help_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)

  await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def speek_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)
  if await is_previous_message_not_answered_yet(update, context):
    return

  dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
  if len(dialog_messages) == 0:
    await update.message.reply_text("No message to retry 🤷‍♂️")
    return

  last_dialog_message = dialog_messages.pop()
  logger.info(last_dialog_message)
  result = speech_synthesizer.speak_text_async(last_dialog_message["bot"]).get()
  await update.message.reply_voice(result.audio_data)


async def retry_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)
  if await is_previous_message_not_answered_yet(update, context):
    return

  dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
  if len(dialog_messages) == 0:
    await update.message.reply_text("No message to retry 🤷‍♂️")
    return

  last_dialog_message = dialog_messages.pop()
  db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context

  await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)


async def stream_response(gen, update: Update, context: CallbackContext, parse_mode):
   # send message to user
  prev_answer = ""
  i = -1
  async for gen_item in gen:
    i += 1

    status = gen_item[0]
    if status == "not_finished":
      status, answer = gen_item
    elif status == "finished":
      status, answer, n_used_tokens, n_first_dialog_messages_removed = gen_item
    else:
      raise ValueError(f"Streaming status {status} is unknown")

    answer = answer[:4096]  # telegram message limit
    if i == 0:  # send first message (then it'll be edited if message streaming is enabled)
      try:
        sent_message = await update.message.reply_text(answer, parse_mode=parse_mode)
      except telegram.error.BadRequest as e:
        if str(e).startswith("Message must be non-empty"):  # first answer chunk from openai was empty
          i = -1  # try again to send first message
          continue
        elif len(answer) <= 0:
          i = -1
          continue
        else:
          sent_message = await update.message.reply_text(answer)
    else:  # edit sent message
      # update only when 100 new symbols are ready
      if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
        continue

      try:
        await context.bot.edit_message_text(answer, chat_id=sent_message.chat_id, message_id=sent_message.message_id, parse_mode=parse_mode)
      except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
          continue
        else:
          await context.bot.edit_message_text(answer, chat_id=sent_message.chat_id, message_id=sent_message.message_id)

      await asyncio.sleep(0.01)  # wait a bit to avoid flooding

    prev_answer = answer

  return answer, n_used_tokens, n_first_dialog_messages_removed


async def handle_yt(update: Update, context: CallbackContext,url):
  await update.message.reply_text("Analyzing video...")
  description, transcript = tools.yt(url)
  logger.info(description)
  logger.info(tools.tokens(description))
  logger.info(tools.tokens(transcript))
  await reset_dialog_handle(update, context)
  transcript = tools.summarize2(transcript, 10000)
  return f'''{description}
    Analysiere folgendes Video. Fasse die Beschreibung, oder das  Transscript falls es keine Beschreibung gibt,
    in einem Absatz mit maximal 40 Wörter zusammen.

    Beschreibung: 
    """
    {description}
    """

    Transscript: 
    """
    {transcript}
    """
    '''

async def handle_txt(update: Update, context: CallbackContext):
  await reset_dialog_handle(update, context)
  await update.message.reply_text("Analyzing text...")
  new_file = await update.message.effective_attachment.get_file()
  await new_file.download_to_drive('tmp.txt')
  with open('tmp.txt', 'r') as txt_file:
    txt=txt_file.read()
    txt = tools.summarize2(txt, 10000)
    return f'''
        Analysiere folgenden Text. Fasse es in einem Absatz mit maximal 40 Wörter zusammen.

        """
        {txt}
        """
        '''

async def handle_url_pdf(url: str):
  import requests
  import tempfile
  response = requests.get(url)
  with tempfile.TemporaryFile('w+b') as f:
    f.write(response.content)
    return await handle_file_pdf(f)

async def handle_doc_pdf(update: Update, context: CallbackContext):
  await reset_dialog_handle(update, context)
  await update.message.reply_text("Analyzing pdf...")
  new_file = await update.message.effective_attachment.get_file()
  await new_file.download_to_drive('file.pdf')
  # await update.message.document.get_file().download_to_drive('file.pdf');
  with open('file.pdf', 'rb') as pdf_file:
    return await handle_file_pdf(pdf_file)

async def handle_file_pdf(pdf_file):
  read_pdf = PyPDF2.PdfReader(pdf_file)
  text_file = ''
  number_of_pages = len(read_pdf.pages)
  for page_number in range(number_of_pages):   # use xrange in Py2
    page = read_pdf.pages[page_number]
    page_content = page.extract_text() 
    text_file += page_content
  text_file = tools.summarize2(text_file, 10000)
  return f'''
      Analysiere folgendes PDF. Erstelle ein Inhaltsverzeichnus und eine Zusammenfassung .

      """
      {text_file}
      """
      '''



async def handle_doc_pd2(update: Update, context: CallbackContext):
  text_file = ''
  await reset_dialog_handle(update, context)
  await update.message.reply_text("Analyzing pdf...")
  new_file = await update.message.effective_attachment.get_file()
  await new_file.download_to_drive('file.pdf')
  # await update.message.document.get_file().download_to_drive('file.pdf');
  with open('file.pdf', 'rb') as pdf_file:
    read_pdf = PyPDF2.PdfReader(pdf_file)

    number_of_pages = len(read_pdf.pages)
    for page_number in range(number_of_pages):   # use xrange in Py2
      page = read_pdf.pages[page_number]
      page_content = page.extract_text()
      text_file += page_content
    text_file = tools.summarize(text_file, 10000)
    return f'''
        Analysiere folgendes PDF. Fasse es in einem Absatz mit maximal 40 Wörter zusammen.

        """
        {text_file}
        """
        '''


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True, tts=False):
  logger.info(update)
  # check if message is edited
  if update.edited_message is not None:
    await edited_message_handle(update, context)
    return

  user_id = await register_user_if_not_exists(update, context, update.message.from_user)
  if await is_previous_message_not_answered_yet(update, context):
    return

  chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

  async with user_semaphores[user_id]:
    # new dialog timeout
    if use_new_dialog_timeout:
      if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
        db.start_new_dialog(user_id)
        await update.message.reply_text(f"Starting new dialog due to timeout (<b>{openai_utils.CHAT_MODES[chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)

    # send typing action
    await update.message.chat.send_action(action="typing")

    try:
      if update.message.document is not None:
        db.start_new_dialog(user_id)
        fn = update.message.document.file_name
        if fn.endswith('.pdf'):
          message = await handle_doc_pdf(update, context)
        elif fn.endswith('.md'):
          message = await handle_txt(update, context)
        else:
          message = "Unbekanntes Format"

      else:
        message = message or update.message.text
        logger.info(message)
        if message.startswith('https://'):
          db.start_new_dialog(user_id)
          if message.startswith('https://www.youtube.com/watch?v=') or message.startswith('https://youtu.be/'):
            message = await handle_yt(update, context,message)
          elif message.startswith('https://') and message.endswith('.pdf'):
            message = await handle_url_pdf(message)

      dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
      parse_mode = {
          "html": ParseMode.HTML,
          "markdown": ParseMode.MARKDOWN
      }[openai_utils.CHAT_MODES[chat_mode]["parse_mode"]]

      chatgpt_instance = openai_utils.ChatGPT(use_chatgpt_api=config.use_chatgpt_api)

      gen = chatgpt_instance.send_message_stream(message, dialog_messages=dialog_messages, chat_mode=chat_mode)
      answer, n_used_tokens, n_first_dialog_messages_removed = await stream_response(gen, update, context, parse_mode)

      # TTS
      if tts and len(answer) > 0:
        logger.info('TTS')
        result = speech_synthesizer.speak_text_async(answer).get()
        await update.message.reply_voice(result.audio_data)

      # update user data
      new_dialog_message = {"user": message, "bot": answer, "date": datetime.now()}
      db.set_dialog_messages(
          user_id,
          db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
          dialog_id=None
      )

      db.set_user_attribute(user_id, "n_used_tokens", n_used_tokens + db.get_user_attribute(user_id, "n_used_tokens"))
    except Exception as e:
      print(e)
      error_text = f"Something went wrong during completion. Reason: {e}"
      await update.message.reply_text(error_text)
      return ""

    # send message if some messages were removed from the context
    if n_first_dialog_messages_removed > 0:
      if n_first_dialog_messages_removed == 1:
        text = "✍️ <i>Note:</i> Your current dialog is too long, so your <b>first message</b> was removed from the context.\n Send /new command to start new dialog"
      else:
        text = f"✍️ <i>Note:</i> Your current dialog is too long, so <b>{n_first_dialog_messages_removed} first messages</b> were removed from the context.\n Send /new command to start new dialog"
      await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def voice_message_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)
  if await is_previous_message_not_answered_yet(update, context):
    return

  voice = update.message.voice
  with tempfile.TemporaryDirectory() as tmp_dir:
    tmp_dir = Path(tmp_dir)
    voice_ogg_path = tmp_dir / "voice.ogg"

    # download
    voice_file = await context.bot.get_file(voice.file_id)
    await voice_file.download_to_drive(voice_ogg_path)

    # convert to mp3
    voice_mp3_path = tmp_dir / "voice.mp3"
    pydub.AudioSegment.from_file(voice_ogg_path).export(voice_mp3_path, format="mp3")

    # transcribe
    with open(voice_mp3_path, "rb") as f:
      transcribed_text = await openai_utils.transcribe_audio(f)

  text = f"🎤: <i>{transcribed_text}</i>"
  await update.message.reply_text(text, parse_mode=ParseMode.HTML)
  await message_handle(update, context, message=transcribed_text, tts=True)
  # calculate spent dollars
  n_spent_dollars = voice.duration * (config.whisper_price_per_1_min / 60)

  # normalize dollars to tokens (it's very convenient to measure everything in a single unit)
  price_per_1000_tokens = config.chatgpt_price_per_1000_tokens if config.use_chatgpt_api else config.gpt_price_per_1000_tokens
  n_used_tokens = int(n_spent_dollars / (price_per_1000_tokens / 1000))
  db.set_user_attribute(user_id, "n_used_tokens", n_used_tokens + db.get_user_attribute(user_id, "n_used_tokens"))


async def reset_dialog_handle(update: Update, context: CallbackContext):
  user_id = update.message.from_user.id
  db.set_user_attribute(user_id, "last_interaction", datetime.now())
  db.start_new_dialog(user_id)


async def new_dialog_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)
  if await is_previous_message_not_answered_yet(update, context):
    return

  db.start_new_dialog(user_id)
  await update.message.reply_text("Starting new dialog ✅")

  chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
  await update.message.reply_text(f"{openai_utils.CHAT_MODES[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def show_chat_modes_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)
  if await is_previous_message_not_answered_yet(update, context):
    return

  keyboard = []
  for chat_mode, chat_mode_dict in openai_utils.CHAT_MODES.items():
    keyboard.append([InlineKeyboardButton(chat_mode_dict["name"], callback_data=f"set_chat_mode|{chat_mode}")])
  reply_markup = InlineKeyboardMarkup(keyboard)

  await update.message.reply_text("Select chat mode:", reply_markup=reply_markup)


async def set_chat_mode_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)

  query = update.callback_query
  await query.answer()

  chat_mode = query.data.split("|")[1]

  db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
  db.start_new_dialog(user_id)

  await query.edit_message_text(f"{openai_utils.CHAT_MODES[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def show_balance_handle(update: Update, context: CallbackContext):
  user_id = await register_user_if_not_exists(update, context, update.message.from_user)

  n_used_tokens = db.get_user_attribute(user_id, "n_used_tokens")

  price_per_1000_tokens = config.chatgpt_price_per_1000_tokens if config.use_chatgpt_api else config.gpt_price_per_1000_tokens
  n_spent_dollars = n_used_tokens * (price_per_1000_tokens / 1000)

  text = f"You spent <b>{n_spent_dollars:.03f}$</b>\n"
  text += f"You used <b>{n_used_tokens}</b> tokens\n\n"

  text += "🏷️ Prices\n"
  text += f"<i>- ChatGPT: {price_per_1000_tokens}$ per 1000 tokens\n"
  text += f"- Whisper (voice recognition): {config.whisper_price_per_1_min}$ per 1 minute</i>"

  await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def edited_message_handle(update: Update, context: CallbackContext):
  text = "🥲 Unfortunately, message <b>editing</b> is not supported"
  await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:

  logger.error(msg="Exception while handling an update:", exc_info=context.error)

  try:
    # collect error message
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )
    if update:
      # split text into multiple messages due to 4096 character limit
      for message_chunk in split_text_into_chunks(message, 4096):
        try:
          await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
        except telegram.error.BadRequest:
          # answer has invalid characters, so we send it without parse_mode
          await context.bot.send_message(update.effective_chat.id, message_chunk)
  except:
    await context.bot.send_message(update.effective_chat.id, "Some error in error handler")


async def post_init(application: Application):
  await application.bot.set_my_commands([
      BotCommand("/new", "Start new dialog"),
      BotCommand("/mode", "Select chat mode"),
      BotCommand("/retry", "Re-generate response for previous query"),
      BotCommand("/speek", "Re-generate voice for previous query"),
      BotCommand("/balance", "Show balance"),
      BotCommand("/help", "Show help message"),
  ])


def run_bot() -> None:
  application = (
      ApplicationBuilder()
      .token(config.telegram_token)
      .concurrent_updates(True)
      .rate_limiter(AIORateLimiter(max_retries=5))
      .post_init(post_init)
      .build()
  )

  # add handlers
  user_filter = filters.ALL
  if len(config.allowed_telegram_usernames) > 0:
    usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
    user_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
    user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids)

  application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
  application.add_handler(CommandHandler("help", help_handle, filters=user_filter))

  application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
  application.add_handler(MessageHandler(filters.Document.PDF & ~filters.COMMAND & user_filter, message_handle))
  application.add_handler(MessageHandler(filters.Document.FileExtension("md") & ~filters.COMMAND & user_filter, message_handle))

  
  application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
  application.add_handler(CommandHandler("speek", speek_handle, filters=user_filter))
  application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))

  application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

  application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
  application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

  application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))

  application.add_error_handler(error_handle)

  # start the bot
  application.run_polling()


if __name__ == "__main__":
  run_bot()
