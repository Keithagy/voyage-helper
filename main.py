import logging
from datetime import datetime
from dotenv import load_dotenv
import os
from typing import Any, Callable, Coroutine, Optional

from openai import OpenAI
from telegram import (
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

SUMMARIZED_ENTRY_KEY = "summarized_entry"
# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

INPUT_SELECT, CONFIRM_OR_EDIT, EDITING_RAW = range(3)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks the user about their preferred start to the flow."""

    message = update.message
    if not message:
        return INPUT_SELECT

    await message.reply_text(
        "Hello voyager! Welcome to your energy accounting assistant. "
        "Send /cancel at any time to stop talking to me.\n\n"
        "Whenever you're ready, tell me via voice or text what you did today and how much time it took.",
    )

    return INPUT_SELECT


def new_from_voice(
    open_ai_client: OpenAI,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:
    """Handles user sending a voice message for their energy accounting entry"""

    async def decorated_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        message = update.message
        if not message:
            return INPUT_SELECT

        voice_file = message.voice
        if not voice_file:
            return INPUT_SELECT

        voice_file_path = f"voice_message-{message.id}.ogg"
        download_handle = await voice_file.get_file()
        voice_file_downloaded = await download_handle.download_to_drive(voice_file_path)
        transcript = await generate_transcript(
            str(voice_file_downloaded), open_ai_client
        )

        user = message.from_user
        if not user:
            logging.warning("SHOULD HAVE USER FOR NEW FROM VOICE")
            return INPUT_SELECT

        summary = await summarize_prose(user.username, transcript, open_ai_client)
        if not summary:
            await message.reply_text(
                "I wasn't able to summarize your input. Could you try again (or send the send the same message again, if you don't wish to change anything about it) please?"
            )
            return INPUT_SELECT
        add_summary_to_context(summary, context)

        await send_summary_confirmation_message(message, summary)
        return CONFIRM_OR_EDIT

    return decorated_handler


def new_from_text(
    open_ai_client: OpenAI,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:
    """Handles user sending a text message for their energy accounting entry"""

    async def decorated_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        message = update.message
        if not message:
            return INPUT_SELECT

        input = message.text
        if not input:
            await message.reply_text(
                "Did you mean to send that? I can't do much with an empty message."
            )
            return INPUT_SELECT

        user = message.from_user
        if not user:
            logging.warning("SHOULD HAVE USER FOR NEW FROM VOICE")
            return INPUT_SELECT

        summary = await summarize_prose(user.username, input, open_ai_client)
        if not summary:
            await message.reply_text(
                "I wasn't able to summarize your input. Could you try again (or send the send the same message again, if you don't wish to change anything about it) please?"
            )
            return INPUT_SELECT
        add_summary_to_context(summary, context)
        await send_summary_confirmation_message(message, summary)

        return CONFIRM_OR_EDIT

    return decorated_handler


async def send_summary_confirmation_message(message: Message, summary: str):
    reply_keyboard = [["Yes", "No"]]

    await message.reply_text(
        text=f"""Here's what I got:

{summary}

Was that right?
            """,
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard,
            one_time_keyboard=True,
            input_field_placeholder="Is this summary accurate?",
        ),
    )


async def generate_transcript(audio_file_path: str, open_ai_client: OpenAI) -> str:
    with open(audio_file_path, "rb") as file:
        transcript = open_ai_client.audio.transcriptions.create(
            model="whisper-1", file=file
        )
    # Check if the file exists
    if os.path.exists(audio_file_path):
        # Delete the file
        os.remove(audio_file_path)
        print(f"File '{audio_file_path}' has been deleted.")
    else:
        print(f"File '{audio_file_path}' does not exist.")
    return transcript.text


async def summarize_prose(
    sender_username: str, transcript: str, open_ai_client: OpenAI
) -> Optional[str]:
    system_prompt = f"""
You are a task completion log summarizer.
The user will send you a transcript of a voice note outlining contributions from a team member, to be summarized and presented as a concise, clear bullet point list. 

Please extract from it the following information:
1. `N`, the number of hours worked. If there is no reference to time spent working, N should be set to Null. Otherwise, try to infer the amount of hours.
2. `key_point_1`, `key_point_2`, `...`, the summarized list of contributions described by the team member. Be sure to include the project(s) that the contribution comes under, as well as names of any collaborators mentioned.

Format output per the following structure:

*Contributor:* {sender_username}
*Date*: {datetime.now()}

*Contributions:*
- `key_point_1`
- `key_point_2`
- `...`

*Hours:* `N` hours
"""

    completion = open_ai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript},
        ],
    )

    return completion.choices[0].message.content


def add_summary_to_context(summary: str, context: ContextTypes.DEFAULT_TYPE):
    chat_data_context = context.chat_data
    if chat_data_context is None:
        logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
        return
    chat_data_context[SUMMARIZED_ENTRY_KEY] = summary


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_data_context = context.chat_data
    if chat_data_context is None:
        logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
        return INPUT_SELECT
    summary: str = chat_data_context[SUMMARIZED_ENTRY_KEY]
    if not summary:
        logging.warning(
            "handle-confirm step does not have a summmary to use from context!"
        )
        return INPUT_SELECT

    message = update.message
    if not message:
        return INPUT_SELECT
    await message.reply_text(text="Energy accounted. Thank you for your work!")
    clear_conversation_context(context)
    return ConversationHandler.END


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_data_context = context.chat_data
    if chat_data_context is None:
        logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
        return INPUT_SELECT
    summary: str = chat_data_context[SUMMARIZED_ENTRY_KEY]
    if not summary:
        logging.warning(
            "handle-confirm step does not have a summmary to use from context!"
        )
        return INPUT_SELECT

    message = update.message
    if not message:
        return INPUT_SELECT
    await message.reply_text(
        text="""Oops. Could you please tell me what the record should state then?
You can paste in the following to start (please follow the format!):
        """
    )
    await message.reply_text(text=summary)
    return EDITING_RAW


async def handle_edit_raw_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.message
    if not message:
        return INPUT_SELECT
    edited_raw = message.text
    if not edited_raw:
        return INPUT_SELECT
    await send_summary_confirmation_message(message, edited_raw)
    return CONFIRM_OR_EDIT


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name)
    clear_conversation_context(context)
    await update.message.reply_text(
        "Bye! I hope we can talk again some day.", reply_markup=ReplyKeyboardRemove()
    )

    return ConversationHandler.END


def clear_conversation_context(context: ContextTypes.DEFAULT_TYPE):
    chat_data = context.chat_data
    if chat_data is None:
        return
    chat_data.clear()


def inject_open_ai_client(client: OpenAI, handler):
    return handler(client)


def main(open_ai_api_key: str, telegram_bot_token: str) -> None:
    """Run the bot."""
    open_ai_client = OpenAI(api_key=open_ai_api_key)
    # Create the Application and pass it your bot's token.
    application = (
        Application.builder()
        .token(telegram_bot_token)
        .arbitrary_callback_data(True)
        .build()
    )

    new_from_text_handler = new_from_text(open_ai_client)
    new_from_voice_handler = new_from_voice(open_ai_client)
    # Add conversation handler with the states GENDER, PHOTO, LOCATION and BIO
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            INPUT_SELECT: [
                MessageHandler(
                    ~filters.Regex("^(/cancel)$") & filters.TEXT, new_from_text_handler
                ),
                MessageHandler(filters.VOICE, new_from_voice_handler),
            ],
            CONFIRM_OR_EDIT: [
                MessageHandler(filters.Regex("^(Yes)$"), handle_confirm),
                MessageHandler(filters.Regex("^(No)$"), handle_edit),
            ],
            EDITING_RAW: [
                MessageHandler(
                    ~filters.Regex("^(/cancel)$") & filters.TEXT, handle_edit_raw_input
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    load_dotenv()
    open_ai_api_key = os.getenv("OPEN_AI_API_KEY")
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if open_ai_api_key is None or telegram_bot_token is None:
        logging.warning(
            "Exiting -- you are missing env values for your Open AI api key and/or Telegram bot token."
        )
    else:
        main(open_ai_api_key, telegram_bot_token)
