import logging
from datetime import datetime
from dotenv import load_dotenv
import os
import requests
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

from allowed_groups import DEVELOPMENT_GROUPS, PRODUCTION_GROUPS, TelegramGroupIdLookup

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

AWAITING_GROUP_SELECTION, AWAITING_RAW_INPUT, CONFIRM_OR_EDIT, EDITING_RAW = range(4)
SUMMARIZED_ENTRY_KEY = "summarized_entry"
SELECTED_GROUP_ID_KEY = "selected_group_id"
SELECTABLE_GROUPS_KEY = "selectable_groups"
PRODUCTION_ENV_NAME = "production"


def start(
    allowed_groups: TelegramGroupIdLookup,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:
    async def decorated_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Starts the conversation and asks the user about their preferred start to the flow."""

        message = update.message
        if not message:
            return AWAITING_RAW_INPUT

        await message.reply_text(
            "Hello voyager! Welcome to your energy accounting assistant. "
            "Send /cancel at any time to stop talking to me. "
            "Just verifying your user data for a second..."
        )
        group_ids_accessible_for_user = []
        for chat_id in allowed_groups.keys():
            chat_membership_data = await context.bot.get_chat_member(
                chat_id=chat_id, user_id=message.from_user.id
            )
            if chat_membership_data.status in {"member", "creator", "administrator"}:
                group_ids_accessible_for_user.append(chat_id)

        if len(group_ids_accessible_for_user) == 0:
            await message.reply_text(
                "It appears you haven't yet been added into any Telegram channels for an Astralship voyage. "
                "Please come back after that has happened! Reach out to a member of Astralship crew for help here. "
                "Later!"
            )
            return ConversationHandler.END

        if len(group_ids_accessible_for_user) == 1:
            await message.reply_text(
                f"You're part of just the one Astralship telegram channel ({( await context.bot.get_chat(group_ids_accessible_for_user[0]) ).effective_name}), so I'll take it that you're doing energy accounting for that."
                "Whenever you're ready, tell me via voice or text what you did today and how much time it took."
            )
            context_chat_data = context.chat_data
            if context_chat_data is None:
                return AWAITING_RAW_INPUT
            context_chat_data[SELECTED_GROUP_ID_KEY] = group_ids_accessible_for_user[0]
            return AWAITING_RAW_INPUT

        groups = {
            (await context.bot.get_chat(group_id))
            for group_id in group_ids_accessible_for_user
        }
        groups = {}
        reply_keyboard = []
        for group_id in group_ids_accessible_for_user:
            chat = await context.bot.get_chat(group_id)
            groups[chat.effective_name] = chat.id
            reply_keyboard.append([chat.effective_name])

        context_chat_data = context.chat_data
        if context_chat_data is None:
            return AWAITING_RAW_INPUT
        context_chat_data[SELECTABLE_GROUPS_KEY] = groups
        await message.reply_text(
            text="I see you are in multiple Astralship-related channels. Which will you be adding an energy accounting entry for?",
            reply_markup=ReplyKeyboardMarkup(
                reply_keyboard,
                one_time_keyboard=True,
                input_field_placeholder="Choose which voyage to add an entry for...",
            ),
        )
        return AWAITING_GROUP_SELECTION

    return decorated_handler


async def handle_group_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    selection = update.message.text

    context_chat_data = context.chat_data
    if context_chat_data is None:
        return AWAITING_RAW_INPUT
    selectable_groups = context_chat_data[SELECTABLE_GROUPS_KEY]

    if selection not in selectable_groups:
        reply_keyboard = []
        for selectable_group_name in selectable_groups.keys():
            reply_keyboard.append([selectable_group_name])

        await update.message.reply_text(
            text="Sorry, I didn't recognize that selection. Please select from one of the groups presented in the keyboard.",
            reply_markup=ReplyKeyboardMarkup(
                reply_keyboard,
                one_time_keyboard=True,
                input_field_placeholder="Choose which voyage to add an entry for...",
            ),
        )
        return AWAITING_GROUP_SELECTION

    selected_group_id = selectable_groups[selection]
    context_chat_data[SELECTED_GROUP_ID_KEY] = selected_group_id

    await update.message.reply_text(
        f"Ok, creating an energy accounting entry for {selection}.\n"
        "Whenever you're ready, tell me via voice or text what you did today and how much time it took."
    )
    return AWAITING_RAW_INPUT


def new_from_voice(
    open_ai_client: OpenAI,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:
    """Handles user sending a voice message for their energy accounting entry"""

    async def decorated_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        message = update.message
        if not message:
            return AWAITING_RAW_INPUT

        voice_file = message.voice
        if not voice_file:
            return AWAITING_RAW_INPUT

        voice_file_path = f"voice_message-{message.id}.ogg"
        download_handle = await voice_file.get_file()
        voice_file_downloaded = await download_handle.download_to_drive(voice_file_path)
        transcript = await generate_transcript(
            str(voice_file_downloaded), open_ai_client
        )

        user = message.from_user
        if not user:
            logging.warning("SHOULD HAVE USER FOR NEW FROM VOICE")
            return AWAITING_RAW_INPUT

        summary = await summarize_prose(transcript, open_ai_client)
        if not summary:
            await message.reply_text(
                "I wasn't able to summarize your input. Could you try again (or send the send the same message again, if you don't wish to change anything about it) please?"
            )
            return AWAITING_RAW_INPUT
        summary_headers_added = f"""*Contributor:* @{user.username}
*Date*: {datetime.now()}

{summary}"""
        add_summary_to_context(summary_headers_added, context)
        await send_summary_confirmation_message(message, summary_headers_added)
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
            return AWAITING_RAW_INPUT

        input = message.text
        if not input:
            await message.reply_text(
                "Did you mean to send that? I can't do much with an empty message."
            )
            return AWAITING_RAW_INPUT

        user = message.from_user
        if not user:
            logging.warning("SHOULD HAVE USER FOR NEW FROM VOICE")
            return AWAITING_RAW_INPUT

        summary = await summarize_prose(input, open_ai_client)
        if not summary:
            await message.reply_text(
                "I wasn't able to summarize your input. Could you try again (or send the send the same message again, if you don't wish to change anything about it) please?"
            )
            return AWAITING_RAW_INPUT

        summary_headers_added = f"""*Contributor:* @{user.username}
*Date*: {datetime.now()}

{summary}"""
        add_summary_to_context(summary_headers_added, context)
        await send_summary_confirmation_message(message, summary_headers_added)

        return CONFIRM_OR_EDIT

    return decorated_handler


async def send_summary_confirmation_message(message: Message, summary: str):
    reply_keyboard = [["Yes"], ["No"]]

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


async def summarize_prose(transcript: str, open_ai_client: OpenAI) -> Optional[str]:
    system_prompt = """
You are a task completion log summarizer.
The user will send you a transcript of a voice note outlining contributions from a team member, to be summarized and presented as a concise, clear bullet point list. 

Please extract from it the following information:
1. `N`, the number of hours worked. If there is no reference to time spent working, N should be set to Null. Otherwise, try to infer the amount of hours.
2. `key_point_1`, `key_point_2`, `...`, the summarized list of contributions described by the team member. Be sure to include the project(s) that the contribution comes under, as well as names of any collaborators mentioned.

Format output per the following structure:

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


def handle_confirm(
    allowed_groups: TelegramGroupIdLookup,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:

    async def decorated_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_data_context = context.chat_data
        if chat_data_context is None:
            logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
            return AWAITING_RAW_INPUT
        summary: str = chat_data_context[SUMMARIZED_ENTRY_KEY]
        if not summary:
            logging.warning(
                "handle-confirm step does not have a summmary to use from context!"
            )
            return AWAITING_RAW_INPUT

        message = update.message
        if not message:
            return AWAITING_RAW_INPUT
        await message.reply_text(text="Energy accounted. Thank you for your work!")

        selected_group_id = chat_data_context[SELECTED_GROUP_ID_KEY]
        await context.bot.send_message(
            chat_id=selected_group_id,
            message_thread_id=allowed_groups[selected_group_id],
            text=summary,
        )

        clear_conversation_context(context)
        return ConversationHandler.END

    return decorated_handler


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_data_context = context.chat_data
    if chat_data_context is None:
        logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
        return AWAITING_RAW_INPUT
    summary: str = chat_data_context[SUMMARIZED_ENTRY_KEY]
    if not summary:
        logging.warning(
            "handle-confirm step does not have a summmary to use from context!"
        )
        return AWAITING_RAW_INPUT

    message = update.message
    if not message:
        return AWAITING_RAW_INPUT
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
        return AWAITING_RAW_INPUT
    edited_raw = message.text
    if not edited_raw:
        return AWAITING_RAW_INPUT
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


# source: https://github.com/kaxing/simple-telegram-gpt-bot/blob/main/main.py
def railway_dns_workaround():
    from time import sleep

    sleep(1.3)
    for _ in range(3):
        if requests.get("https://api.telegram.org", timeout=3).status_code == 200:
            print("The api.telegram.org is reachable.")
            return
        print(f"The api.telegram.org is not reachable. Retrying...({_})")
    print("Failed to reach api.telegram.org after 3 attempts.")


def main(
    open_ai_api_key: str, telegram_bot_token: str, allowed_groups: TelegramGroupIdLookup
) -> None:
    """Run the bot."""
    railway_dns_workaround()
    open_ai_client = OpenAI(api_key=open_ai_api_key)
    application = Application.builder().token(telegram_bot_token).build()

    new_from_text_handler = new_from_text(open_ai_client)
    new_from_voice_handler = new_from_voice(open_ai_client)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start(allowed_groups))],
        states={
            AWAITING_GROUP_SELECTION: [
                MessageHandler(
                    ~filters.Regex("^(/cancel)$") & filters.TEXT, handle_group_selection
                ),
            ],
            AWAITING_RAW_INPUT: [
                MessageHandler(
                    ~filters.Regex("^(/cancel)$") & filters.TEXT, new_from_text_handler
                ),
                MessageHandler(filters.VOICE, new_from_voice_handler),
            ],
            CONFIRM_OR_EDIT: [
                MessageHandler(
                    filters.Regex("^(Yes)$"), handle_confirm(allowed_groups)
                ),
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
    environment_name = os.getenv("RAILWAY_ENVIRONMENT_NAME")
    if (
        open_ai_api_key is None
        or telegram_bot_token is None
        or environment_name is None
    ):
        logging.warning(
            "Exiting -- you are missing env values for your environment name (needed to resolve group membership checks) Open AI api key and/or Telegram bot token."
        )
    else:
        allowed_groups = (
            PRODUCTION_GROUPS
            if environment_name == PRODUCTION_ENV_NAME
            else DEVELOPMENT_GROUPS
        )
        main(open_ai_api_key, telegram_bot_token, allowed_groups)
