# Group IDs listed below indicate the channels, and topics if any, a bot should be blasting a completed energy account out to.
# In the event a given user is part of multiple voyage groups, then we use these values to prompt the user for a selection on which group they'd like to account energy under
# NOTE: Every group listed down here should be a group that the bot is already added to + has permission to view member lists for.

from typing import Dict, Optional


class TelegramGroupIdentifier:
    def __init__(self, group_id: int, message_thread_id: Optional[int]) -> None:
        self.group_id = group_id
        self.message_thread_id = message_thread_id


TelegramGroupId = int
TelegramMessageThreadId = int  # indicates which topic in a given Telegram supergroup the bot should blast completed energy accounts out to.
TelegramGroupIdLookup = Dict[TelegramGroupId, TelegramMessageThreadId]

# TODO: make use of telegram API to determine which group ids the bot should be aware of upon startup
PRODUCTION_GROUPS: TelegramGroupIdLookup = {
    -1002133514647: 136,  # test channel
    -1002075316483: 178,  # phoenix crew
}

DEVELOPMENT_GROUPS: TelegramGroupIdLookup = {
    -1002133514647: 136,  # test channel
}
