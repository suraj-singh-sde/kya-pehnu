from fastapi import APIRouter

from kyapehnu.auth.public import CurrentUser
from kyapehnu.core.deps import SessionDep
from kyapehnu.user import profile
from kyapehnu.user.models import UserProfile, UserProfileBase

router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("")
def get_profile(user: CurrentUser, s: SessionDep) -> UserProfile:
    return profile.get_profile(user.id, s)


@router.put("")
def put_profile(body: UserProfileBase, user: CurrentUser, s: SessionDep) -> UserProfile:
    return profile.update_profile(user.id, s, body)
