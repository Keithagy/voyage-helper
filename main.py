import json
import logging
import os
import re
import time as os_time
from typing_extensions import Dict
import uuid
import copy
from datetime import datetime, time, timedelta
import pytz
from typing import Any, Callable, Coroutine, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    create_engine,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, sessionmaker
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
logging.getLogger("httpx").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

(
    AWAITING_GROUP_SELECTION,
    AWAITING_RAW_INPUT,
    AWAITING_HOURS_INPUT,
    CONFIRM_OR_EDIT,
    EDITING_RAW,
) = range(5)
ENTRY_BEING_CREATED_KEY = "entry_being_created"  # Value should be an instance of `EnergyAccountDTO`, representing the energy account being incrementally built over the bot's /start flow
SELECTED_GROUP_KEY = "selected_group"  # Value should be a tuple representing (group_id, group_name); MAINTAINABILITY NOTE, this kind of context-dependent nullability is where python starts to show its lack, and something like rust starts to become very attractive.
SELECTABLE_GROUPS_KEY = "selectable_groups"  # Value should be a dict of (group_name, group_id) to facilitate selection
PRODUCTION_ENV_NAME = "production"

Base = declarative_base()


class TaskDTO:
    def __init__(self, description: str):
        self.description = description

    @classmethod
    def from_db_model(cls, db_model: "TaskModel") -> "TaskDTO":
        return TaskDTO(
            description=db_model.description,
        )

    def present(self) -> str:
        return f"- {self.description}"


class EnergyAccountDTO:
    def __init__(
        self,
        tg_user_id: int,
        audit_tg_user_name: str,
        tg_group_id: int,
        audit_tg_group_name: str,
        hours: float,
        tasks: List[TaskDTO],
        timestamp: datetime,
    ):
        self.tg_user_id = tg_user_id
        self.audit_tg_user_name = audit_tg_user_name
        self.tg_group_id = tg_group_id
        self.audit_tg_group_name = audit_tg_group_name
        self.hours = hours
        self.tasks = tasks
        self.timestamp = timestamp

    @classmethod
    def from_db_model(
        cls, db_model: "EnergyAccountModel", tasks: List["TaskModel"]
    ) -> "EnergyAccountDTO":
        return EnergyAccountDTO(
            tg_user_id=db_model.tg_user_id,
            audit_tg_user_name=db_model.audit_tg_user_name,
            tg_group_id=db_model.tg_group_id,
            audit_tg_group_name=db_model.audit_tg_group_name,
            hours=db_model.hours,
            timestamp=db_model.timestamp,
            tasks=[TaskDTO.from_db_model(dto) for dto in tasks],
        )

    def __present_tasks(self) -> str:
        task_strings = [task.present() for task in self.tasks]
        return "\n".join(task_strings)

    def present(self) -> str:
        return f"""*Contributor:* {self.audit_tg_user_name}
*Date*: {self.timestamp}

{self.present_for_editing()}"""

    def present_for_editing(self) -> str:
        return f"""*Contributions*
{self.__present_tasks()}

*Hours*: {self.hours} hours"""


class EnergyAccountModel(Base):
    __tablename__ = "energy_accounts"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tg_user_id = Column(BigInteger)
    audit_tg_user_name = Column(
        String
    )  # per python-telegram-bot's API, this value is f"@{username}" if `username` is available, and `full_name` otherwise.
    tg_group_id = Column(BigInteger)
    audit_tg_group_name = Column(String)
    hours = Column(Float)
    timestamp = Column(DateTime)

    __table_args__ = (
        Index("idx_tg_user_id", tg_user_id),
        Index("idx_tg_group_id", tg_group_id),
    )

    @classmethod
    def from_dto(cls, dto: EnergyAccountDTO) -> "EnergyAccountModel":
        return EnergyAccountModel(
            tg_user_id=dto.tg_user_id,
            audit_tg_user_name=dto.audit_tg_user_name,
            tg_group_id=dto.tg_group_id,
            audit_tg_group_name=dto.audit_tg_group_name,
            hours=dto.hours,
            timestamp=dto.timestamp,
        )


class EnergyAccountingReport:
    entries_by_user_id: Dict[int, List[EnergyAccountDTO]]

    def __init__(self, energy_accounts_by_user_id: Dict[int, List[EnergyAccountDTO]]):
        self.entries_by_user_id = energy_accounts_by_user_id

    @classmethod
    def from_dtos(cls, dtos: List[EnergyAccountDTO]) -> "EnergyAccountingReport":
        # Reports would already be subsetted by group, but we have to subset further by user
        energy_accounts_by_user_id: Dict[int, List[EnergyAccountDTO]] = {}
        for energy_account in dtos:
            user_id = energy_account.tg_user_id
            if user_id in energy_accounts_by_user_id:
                energy_accounts_by_user_id[user_id].append(energy_account)
            else:
                energy_accounts_by_user_id[user_id] = [energy_account]
        return EnergyAccountingReport(
            energy_accounts_by_user_id=energy_accounts_by_user_id
        )

    def present(self) -> str:
        report_entries: List[Tuple[str, List[TaskDTO], float]] = (
            []
        )  # user display name, tasks, cumul. hours
        for user_id in self.entries_by_user_id:
            accounts_for_user = self.entries_by_user_id[user_id]
            display_name = accounts_for_user[
                0
            ].audit_tg_user_name  # no list in this dict should be empty
            cumul_tasks = []
            cumul_hours = 0.0
            for account in accounts_for_user:
                for task in account.tasks:
                    cumul_tasks.append(task)
                cumul_hours += account.hours
            report_entries.append((display_name, cumul_tasks, cumul_hours))

        report_rows = []
        for report_entry in report_entries:
            task_lines = "\n".join([task.present() for task in report_entry[1]])
            report_rows.append(
                f"""*Contributor*: {report_entry[0]}
*Contributions*
{task_lines}
*Hours*: {report_entry[2]} hours"""
            )
        report_rows_display = "\n\n".join(report_rows)
        return f"""It's been another week! Here's what all of us managed to get done:

{report_rows_display}

Congratulations on the good work all around!
"""


class TaskModel(Base):
    __tablename__ = "tasks"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    energy_account_id = Column(UUID(as_uuid=True), ForeignKey("energy_accounts.id"))
    description = Column(String)

    @classmethod
    def from_dto(cls, task_dto: TaskDTO, energy_account_id: UUID) -> "TaskModel":
        return TaskModel(
            energy_account_id=energy_account_id, description=task_dto.description
        )


async def handle_uninitialized_voice_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.message
    if not message:
        return ConversationHandler.END

    if message.chat.type != "private":
        # bot_link = f"https://t.me/{context.bot.username}"
        # keyboard = [[InlineKeyboardButton("Start Private Chat", url=bot_link)]]
        # reply_markup = InlineKeyboardMarkup(keyboard)
        # await message.reply_text(
        #     f"{message.from_user.name}, are you trying to log a new energy account? This channel should be for announcements only. Please click the button below to open our private chat, then try again with /start. Thank you!",
        #     reply_markup=reply_markup,
        # )
        return ConversationHandler.END

    await message.reply_text(
        f"Hi {message.from_user.name}, please /start me first. Thank you!",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


def start(
    allowed_groups: TelegramGroupIdLookup,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:
    async def decorated_start_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Starts the conversation and asks the user about their preferred start to the flow."""

        message = update.message
        if not message:
            return ConversationHandler.END

        if message.chat.type != "private":
            bot_link = f"https://t.me/{context.bot.username}"
            keyboard = [[InlineKeyboardButton("Start Private Chat", url=bot_link)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await message.reply_text(
                f"{message.from_user.name}, please click the button below to open our private chat, then try again with /start. Thank you!",
                reply_markup=reply_markup,
            )
            return ConversationHandler.END

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
                "Later!",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END

        if len(group_ids_accessible_for_user) == 1:
            lone_chat = await context.bot.get_chat(group_ids_accessible_for_user[0])
            await message.reply_text(
                f"You're part of just the one Astralship telegram channel ({lone_chat.effective_name}), so I'll take it that you're doing energy accounting for that. "
                "Whenever you're ready, tell me via voice or text what you did today and how much time it took."
            )
            context_chat_data = context.chat_data
            if context_chat_data is None:
                return AWAITING_RAW_INPUT
            context_chat_data[SELECTED_GROUP_KEY] = (
                lone_chat.id,
                lone_chat.effective_name,
            )
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

    return decorated_start_handler


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
    context_chat_data[SELECTED_GROUP_KEY] = (selected_group_id, selection)

    await update.message.reply_text(
        f"Ok, creating an energy accounting entry for {selection}.\n"
        "Whenever you're ready, tell me via voice or text what you did today and how much time it took."
    )
    return AWAITING_RAW_INPUT


def new_from_voice(
    open_ai_client: OpenAI,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:
    """Handles user sending a voice message for their energy accounting entry"""

    async def decorated_new_from_voice_handler(
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

        llm_output_json_string = await summarize_prose(transcript, open_ai_client)
        if llm_output_json_string is None:
            await message.reply_text(
                "I wasn't able to summarize your input. Could you try again (or send the send the same message again, if you don't wish to change anything about it) please?"
            )
            return AWAITING_RAW_INPUT

        chat_data_context = context.chat_data
        if chat_data_context is None:
            logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
            return ConversationHandler.END

        (selected_group_id, selected_group_name) = chat_data_context[SELECTED_GROUP_KEY]
        new_energy_account = EnergyAccountDTO(
            message.from_user.id,
            message.from_user.name,
            selected_group_id,
            selected_group_name,
            None,
            None,
            datetime.now(),
        )
        task_hours_summarized_dict = json.loads(llm_output_json_string)
        # Read `summarize_prose` for more information how wonky-prompt edge cases get handled.
        if (
            JSON_OUTPUT_HOURS_KEY not in task_hours_summarized_dict
            or JSON_OUTPUT_HOURS_KEY not in task_hours_summarized_dict
        ):
            await message.reply_text(
                "I couldn't identify any meaningful tasks to summarize. Could you tell me again, please? It might help me if you rephrase a little bit."
            )
            return AWAITING_RAW_INPUT
        hours = task_hours_summarized_dict[JSON_OUTPUT_HOURS_KEY]
        tasks_json = task_hours_summarized_dict[JSON_OUTPUT_TASKS_KEY]
        if tasks_json is None or len(tasks_json) == 0:
            await message.reply_text(
                "I couldn't identify any meaningful tasks to summarize. Could you tell me again, please? It might help me if you rephrase a little bit."
            )
            return AWAITING_RAW_INPUT

        # Iterate through model output for tasks to parse into code-friendly structure
        tasks_parsed: List[TaskDTO] = []
        for task_json in tasks_json:
            task_description = task_json[JSON_OUTPUT_DESCRIPTION_KEY]
            if task_description is None or task_description == "":
                logging.error("PROMPT IS GENERATING EMPTY TASK DESCRIPTIONS")
                continue
            task_parsed = TaskDTO(task_description)
            tasks_parsed.append(task_parsed)
        new_energy_account.tasks = tasks_parsed

        if hours is None:
            await message.reply_text(
                """Don't think you said anything about how many hours you spent there. How many was that?
Please just input the (positive!) number and nothing else.

That is, DO:
3
15.5
18.7

NOT:
3 hours
-1hr
8 hours and 5 minutes"""
            )
            add_summary_to_context(new_energy_account, context)
            return AWAITING_HOURS_INPUT
        new_energy_account.hours = hours

        add_summary_to_context(new_energy_account, context)
        await send_summary_confirmation_message(message, new_energy_account)

        return CONFIRM_OR_EDIT

    return decorated_new_from_voice_handler


def new_from_text(
    open_ai_client: OpenAI,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:
    """Handles user sending a text message for their energy accounting entry"""

    async def decorated_new_from_text_handler(
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

        llm_output_json_string = await summarize_prose(input, open_ai_client)
        if llm_output_json_string is None:
            await message.reply_text(
                "I wasn't able to summarize your input. Could you try again (or send the send the same message again, if you don't wish to change anything about it) please?"
            )
            return AWAITING_RAW_INPUT

        chat_data_context = context.chat_data
        if chat_data_context is None:
            logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
            return ConversationHandler.END

        (selected_group_id, selected_group_name) = chat_data_context[SELECTED_GROUP_KEY]
        new_energy_account = EnergyAccountDTO(
            message.from_user.id,
            message.from_user.name,
            selected_group_id,
            selected_group_name,
            None,
            None,
            datetime.now(),
        )
        task_hours_summarized_dict = json.loads(llm_output_json_string)
        # Read `summarize_prose` for more information how wonky-prompt edge cases get handled.
        if (
            JSON_OUTPUT_HOURS_KEY not in task_hours_summarized_dict
            or JSON_OUTPUT_HOURS_KEY not in task_hours_summarized_dict
        ):
            await message.reply_text(
                "I couldn't identify any meaningful tasks to summarize. Could you tell me again, please? It might help me if you rephrase a little bit."
            )
            return AWAITING_RAW_INPUT
        hours = task_hours_summarized_dict[JSON_OUTPUT_HOURS_KEY]
        tasks_json = task_hours_summarized_dict[JSON_OUTPUT_TASKS_KEY]
        # Read `summarize_prose` for more information how wonky-prompt edge cases get handled.
        if tasks_json is None or len(tasks_json) == 0:
            await message.reply_text(
                "I couldn't identify any meaningful tasks to summarize. Could you tell me again, please? It might help me if you rephrase a little bit."
            )
            return AWAITING_RAW_INPUT

        # Iterate through model output for tasks to parse into code-friendly structure
        tasks_parsed: List[TaskDTO] = []
        for task_json in tasks_json:
            task_description = task_json[JSON_OUTPUT_DESCRIPTION_KEY]
            if task_description is None or task_description == "":
                logging.error("PROMPT IS GENERATING EMPTY TASK DESCRIPTIONS")
                continue
            task_parsed = TaskDTO(task_description)
            tasks_parsed.append(task_parsed)
        new_energy_account.tasks = tasks_parsed

        if hours is None:
            await message.reply_text(
                """Don't think you said anything about how many hours you spent there. How many was that?

Please just input the (positive!) number and nothing else."""
            )
            add_summary_to_context(new_energy_account, context)
            return AWAITING_HOURS_INPUT
        new_energy_account.hours = hours

        add_summary_to_context(new_energy_account, context)
        await send_summary_confirmation_message(message, new_energy_account)

        return CONFIRM_OR_EDIT

    return decorated_new_from_text_handler


async def backfilling_hours_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number_only_regex = r"^\d+(\.\d+)?$"
    if re.match(number_only_regex, update.message.text):
        # parse the number and update energy account dto held in context with the new value
        energy_account = context.chat_data[ENTRY_BEING_CREATED_KEY]
        energy_account.hours = float(update.message.text)
        add_summary_to_context(energy_account, context)
        await send_summary_confirmation_message(update.message, energy_account)
        return CONFIRM_OR_EDIT
    await update.message.reply_text(
        """That didn't make sense to me.

Please input the hour count and nothing else (e.g. 3, 15.5, 18.7 would work, "3 hours", "- 15", or "8 hours and 5 minutes" would not)."""
    )
    return AWAITING_HOURS_INPUT


async def send_summary_confirmation_message(
    message: Message, new_energy_account: EnergyAccountDTO
):
    reply_keyboard = [["Yes"], ["No"]]

    await message.reply_text(
        text=f"""Here's what I got:

{new_energy_account.present()}

Was that right?
            """,
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard,
            one_time_keyboard=True,
            input_field_placeholder="Is this summary accurate?",
        ),
    )


JSON_OUTPUT_HOURS_KEY = "hours"
JSON_OUTPUT_TASKS_KEY = "tasks"
JSON_OUTPUT_DESCRIPTION_KEY = "description"


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
    system_prompt = f"""
You are a producer of JSON objects representing task completion accounting entries.
The user will send you a transcript of a voice note outlining contributions from a team member, to be summarized and presented as a concise, clear bullet point list. 

Please extract from it the following information:
1. `N`, the number of hours worked. If there is no reference to time spent working, N should be null. Otherwise, try to infer the amount of hours.
2. `key_point_1`, `key_point_2`, `...`, the summarized list of contributions described by the team member. Be sure to include the project(s) that the contribution comes under, as well as names of any collaborators mentioned.

Format output as a well-formed JSON object per the following schema:

{{
    "{JSON_OUTPUT_HOURS_KEY}": N,
    "{JSON_OUTPUT_TASKS_KEY}": [
        {{"{JSON_OUTPUT_DESCRIPTION_KEY}": key_point_1 }},
        {{"{JSON_OUTPUT_DESCRIPTION_KEY}": key_point_2 }},
        ...
    ],
}}

If you weren't able to identify any meaningful tasks to summarize, DO NOT output a default placeholder reply prompting the user to give you input. Instead, just return the empty JSON object, `{{}}`.
"""
    completion = open_ai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript},
        ],
    )
    return completion.choices[0].message.content


def add_summary_to_context(
    summary: EnergyAccountDTO, context: ContextTypes.DEFAULT_TYPE
):
    chat_data_context = context.chat_data
    if chat_data_context is None:
        logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
        return
    chat_data_context[ENTRY_BEING_CREATED_KEY] = summary


def handle_confirm(
    allowed_groups: TelegramGroupIdLookup,
    session: sessionmaker,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, int]]:

    async def decorated_handle_confirm_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_data_context = context.chat_data
        if chat_data_context is None:
            logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
            return AWAITING_RAW_INPUT
        summary: EnergyAccountDTO = chat_data_context[ENTRY_BEING_CREATED_KEY]
        if not summary:
            logging.warning(
                "handle-confirm step does not have a summmary to use from context!"
            )
            return AWAITING_RAW_INPUT

        message = update.message
        if not message:
            return AWAITING_RAW_INPUT

        energy_account_dto: EnergyAccountDTO = context.chat_data[
            ENTRY_BEING_CREATED_KEY
        ]
        db_session = session()
        created_energy_account_db_model = EnergyAccountModel.from_dto(
            energy_account_dto
        )
        db_session.add(created_energy_account_db_model)
        db_session.flush()

        task_db_models = [
            TaskModel.from_dto(dto, created_energy_account_db_model.id)
            for dto in energy_account_dto.tasks
        ]
        db_session.add_all(task_db_models)

        await context.bot.send_message(
            chat_id=energy_account_dto.tg_group_id,
            message_thread_id=allowed_groups[energy_account_dto.tg_group_id],
            text=energy_account_dto.present(),
        )
        await message.reply_text(
            text="Energy accounted. Thank you for your work!",
            reply_markup=ReplyKeyboardRemove(),
        )
        db_session.commit()
        db_session.close()

        clear_conversation_context(context)
        return ConversationHandler.END

    return decorated_handle_confirm_handler


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_data_context = context.chat_data
    if chat_data_context is None:
        logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
        return AWAITING_RAW_INPUT
    new_account_dto: EnergyAccountDTO = chat_data_context[ENTRY_BEING_CREATED_KEY]
    if not new_account_dto:
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
    await message.reply_text(text=new_account_dto.present_for_editing())
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
    pattern = r"\*Contributions\*\n(- .+\n)+\n\*Hours\*: (\d+(\.\d+)?) hours"
    match = re.search(pattern, edited_raw)
    if match:
        chat_data_context = context.chat_data
        if chat_data_context is None:
            logging.warning("CONTEXT SHOULD NOT BE MISSING `chat_data`")
            return AWAITING_RAW_INPUT
        being_edited_account_dto: EnergyAccountDTO = chat_data_context[
            ENTRY_BEING_CREATED_KEY
        ]

        task_descriptions = [
            line.strip("- ") for line in match.group(0).split("\n")[1:-2]
        ]

        edited_tasks_parsed: List[TaskDTO] = []
        for task_description in task_descriptions:
            trimmed = task_description.strip()
            if trimmed == "":
                continue
            task_parsed = TaskDTO(trimmed)
            edited_tasks_parsed.append(task_parsed)

        being_edited_account_dto.tasks = edited_tasks_parsed
        hours = float(match.group(2))
        being_edited_account_dto.hours = hours
        await send_summary_confirmation_message(message, being_edited_account_dto)
        return CONFIRM_OR_EDIT
    await message.reply_text(
        text="""I don't really understand this. Please make sure your edit follows the following format:
*Contributions*
- <<Task description>>
- <<Task description>>
- <<Task description>>
<<... any number hyphen-delimited task descriptions>>

*Hours*: <<Number, possibly with a decimal place>> hours"""
    )
    return EDITING_RAW


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.name)
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


def create_energy_accounts_reminder(
    allowed_groups: TelegramGroupIdLookup,
) -> Callable[[ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]:
    async def decorated_create_energy_accounts_reminder_handler(
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        logging.info("reminding users to create energy acccouts...")
        relevant_group_ids = allowed_groups.keys()
        for group_id in relevant_group_ids:
            logging.info(f"Reminding group id f{group_id}")
            bot_link = f"https://t.me/{context.bot.username}"
            keyboard = [[InlineKeyboardButton("Start Private Chat", url=bot_link)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=group_id,
                text="""Here's your friendly reminder to create your energy accounting logs if you haven't already for the day!

/start me in our private chat.""",
                reply_markup=reply_markup,
            )

    return decorated_create_energy_accounts_reminder_handler


def get_energy_accounts_created_in_reporting_interval_for_group(
    reporting_interval: timedelta, group_id: int, session: sessionmaker
) -> List[EnergyAccountDTO]:
    db_session = session()
    energy_account_models = (
        db_session.query(EnergyAccountModel)
        .filter(
            EnergyAccountModel.tg_group_id == group_id,
            EnergyAccountModel.timestamp >= datetime.now() - reporting_interval,
        )
        .all()
    )
    energy_account_ids = {energy_account.id for energy_account in energy_account_models}
    task_models = (
        db_session.query(TaskModel)
        .filter(
            TaskModel.energy_account_id.in_(energy_account_ids),
        )
        .all()
    )
    db_session.close()
    result = []
    for energy_account_model in energy_account_models:
        related_tasks = [
            model
            for model in task_models
            if model.energy_account_id == energy_account_model.id
        ]
        result.append(
            EnergyAccountDTO.from_db_model(energy_account_model, related_tasks)
        )
    return result


def generate_reports(
    reporting_interval: timedelta,
    allowed_groups: TelegramGroupIdLookup,
    session: sessionmaker,
) -> Callable[[ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]:
    async def decorated_generate_reports_handler(
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        for group_id in allowed_groups.keys():
            energy_accounts_created_in_reporting_interval_for_group = (
                get_energy_accounts_created_in_reporting_interval_for_group(
                    reporting_interval, group_id, session
                )
            )
            # this model would already have been scoped to the voyage by virtue of the channel id, so we expect to index by user id
            report = EnergyAccountingReport.from_dtos(
                energy_accounts_created_in_reporting_interval_for_group
            )
            await context.bot.send_message(
                chat_id=group_id,
                message_thread_id=allowed_groups[group_id],
                text=report.present(),
            )

    return decorated_generate_reports_handler


def test_generate_reports(
    reporting_interval: timedelta,
    allowed_groups: TelegramGroupIdLookup,
    session: sessionmaker,
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]:
    async def decorated_generate_reports_handler(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        for group_id in allowed_groups.keys():
            energy_accounts_created_in_reporting_interval_for_group = (
                get_energy_accounts_created_in_reporting_interval_for_group(
                    reporting_interval, group_id, session
                )
            )
            # this model would already have been scoped to the voyage by virtue of the channel id, so we expect to index by user id
            report = EnergyAccountingReport.from_dtos(
                energy_accounts_created_in_reporting_interval_for_group
            )
            await context.bot.send_message(
                chat_id=group_id,
                message_thread_id=allowed_groups[group_id],
                text=report.present(),
            )

    return decorated_generate_reports_handler


def main(
    open_ai_api_key: str,
    telegram_bot_token: str,
    allowed_groups: TelegramGroupIdLookup,
    session: sessionmaker,
) -> None:
    """Run the bot."""
    railway_dns_workaround()
    open_ai_client = OpenAI(api_key=open_ai_api_key)
    application = (
        Application.builder()
        .concurrent_updates(
            False
        )  # Needed for ConversationHandler to function correctly
        .token(telegram_bot_token)
        .build()
    )

    # Handle energy account creation with a llm-enabled multi-step conversation
    new_from_text_handler = new_from_text(open_ai_client)
    new_from_voice_handler = new_from_voice(open_ai_client)
    sending_text_or_voice_without_start_handler = MessageHandler(
        filters.VOICE | filters.TEXT, handle_uninitialized_voice_text_input
    )

    job_queue = application.job_queue
    if job_queue is None:
        logging.warning("APPLICATION JOB_QUEUE SHOULD NOT BE NONE")
        return

    timezone = pytz.timezone("Europe/London")
    # Every day at 6pm GMT+1, send a reminder to all users to create energy accounts.
    # Each user should get a single private message only regardless of how many voyages they might be a part of.
    # NOTE: would be important to be able to mark voyages as active or not.
    # TODO: instead of a dumb reminder, it can show the user all tasks logged for that day and make sure it's correct.
    create_energy_accounts_reminder_time = time(18, 00, tzinfo=timezone)
    job_queue.run_daily(
        create_energy_accounts_reminder(allowed_groups),
        time=create_energy_accounts_reminder_time,
    )

    # Every week at 7pm GMT+1, summarize crew contributions for the week and update charts + blast out onto relevant channels.
    report_generation_time = time(19, 0, tzinfo=timezone)
    current_datetime = datetime.now(timezone)
    REPORT_GEN_DAY_INT_REPR = 4  # friday, week starts from 0 on monday
    DAYS_IN_WEEK = 7
    days_until_report_gen = (
        REPORT_GEN_DAY_INT_REPR - current_datetime.weekday()
    ) % DAYS_IN_WEEK
    if days_until_report_gen == 0 and current_datetime.time() > report_generation_time:
        days_until_report_gen = DAYS_IN_WEEK
    next_report_gen_day = current_datetime.date() + timedelta(
        days=days_until_report_gen
    )
    next_report_gen_datetime = datetime.combine(
        next_report_gen_day, report_generation_time, tzinfo=timezone
    )
    report_generation_interval = timedelta(weeks=1)
    job_queue.run_repeating(
        generate_reports(
            copy.deepcopy(report_generation_interval), allowed_groups, session
        ),
        interval=report_generation_interval,
        first=next_report_gen_datetime,
    )
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start(allowed_groups)),
            CommandHandler(
                "testreport",
                test_generate_reports(
                    copy.deepcopy(report_generation_interval), allowed_groups, session
                ),
            ),
            sending_text_or_voice_without_start_handler,
        ],
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
            AWAITING_HOURS_INPUT: [
                MessageHandler(filters.TEXT, backfilling_hours_handler),
            ],
            CONFIRM_OR_EDIT: [
                MessageHandler(
                    filters.Regex("^(Yes)$"), handle_confirm(allowed_groups, session)
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
    os_time.sleep(
        3
    )  # workaround for railway private networking delay: https://docs.railway.app/guides/private-networking#initialization-time
    load_dotenv()
    open_ai_api_key = os.getenv("OPEN_AI_API_KEY")
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    environment_name = os.getenv("RAILWAY_ENVIRONMENT_NAME")
    database_url = os.getenv("DATABASE_URL")
    if (
        open_ai_api_key is None
        or telegram_bot_token is None
        or environment_name is None
        or database_url is None
    ):
        logging.warning(
            "Exiting -- you are missing env values for your, database url, environment name (needed to resolve group membership checks) Open AI api key and/or Telegram bot token."
        )
    else:
        engine = create_engine(database_url)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        logging.info(f"Starting bot for environment {environment_name}.")
        allowed_groups = (
            PRODUCTION_GROUPS
            if environment_name == PRODUCTION_ENV_NAME
            else DEVELOPMENT_GROUPS
        )
        main(open_ai_api_key, telegram_bot_token, allowed_groups, Session)
