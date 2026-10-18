"""The dashboard chat (design 2026-10-17, requirement 9). Callers use `Chat.ask(question, user)`."""

from backend.chat.service import Answer, Chat, FollowUp
from backend.chat.tools import Ref

__all__ = ["Answer", "Chat", "FollowUp", "Ref"]
