"""The chat a model request belongs to, as the outfit pick needs to know it."""

from dataclasses import dataclass


@dataclass
class ChatView:
    """The chat a request belongs to: which events it made itself, and the user's home city (never sent)."""

    session_id: int
    session_event_ids: set[int]
    home_city: str

    @property
    def marker(self) -> str:
        """Starts the log row of a nested pick, so deleting the chat can find it (`session=1 ` is not `session=12 `)."""
        return f"session={self.session_id}"
