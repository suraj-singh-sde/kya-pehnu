from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from kyapehnu.core.config import Prefs
from kyapehnu.core.errors import NotFoundError
from kyapehnu.user.models import UserProfile, UserProfileBase


def get_profile(user_id: int, s: Session) -> UserProfile:
    profile = s.exec(select(UserProfile).where(UserProfile.user_id == user_id)).first()
    if profile is None:
        raise NotFoundError(f"Profile of user {user_id} not found")
    return profile


def update_profile(user_id: int, s: Session, body: UserProfileBase) -> UserProfile:
    profile = get_profile(user_id, s)
    for field, value in body.model_dump().items():
        setattr(profile, field, value)
    s.commit()
    s.refresh(profile)
    return profile


def ensure_profile(user_id: int, s: Session, prefs: Prefs) -> UserProfile:
    """The user's profile row, created on first use: home city and budget from the preferences file, gender unset so
    the SPA shows the onboarding chat."""
    profile = s.exec(select(UserProfile).where(UserProfile.user_id == user_id)).first()
    if profile is None:
        profile = UserProfile(
            user_id=user_id,
            home_city=prefs.home_city,
            budget_min=prefs.profile.budget_min,
            budget_max=prefs.profile.budget_max,
        )
        s.add(profile)
        try:
            s.commit()
        except IntegrityError:  # another request (a second tab) created it first: use theirs
            s.rollback()
            return get_profile(user_id, s)
    return profile
