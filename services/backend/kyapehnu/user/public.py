"""What the other domains may use from the user domain (LLD 3.4)."""

from sqlmodel import Session

from kyapehnu.core.config import Prefs
from kyapehnu.user import profile
from kyapehnu.user.models import UserProfile as Profile  # annotation only

__all__ = ["Profile", "ensure_profile", "get_profile"]


def get_profile(user_id: int, s: Session) -> Profile:
    return profile.get_profile(user_id, s)


def ensure_profile(user_id: int, s: Session, prefs: Prefs) -> Profile:
    return profile.ensure_profile(user_id, s, prefs)
