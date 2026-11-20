from fastapi import APIRouter

from kyapehnu.auth.public import CurrentUser
from kyapehnu.core.deps import CtxDep, SessionDep
from kyapehnu.wardrobe.outfits import feedback
from kyapehnu.wardrobe.outfits.schemas import FeedbackIn, FeedbackOut

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


@router.post("", status_code=201)
def post_feedback(body: FeedbackIn, s: SessionDep, ctx: CtxDep, user: CurrentUser) -> FeedbackOut:
    return feedback.record(user.id, ctx, s, body)
