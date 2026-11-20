"""GET|HEAD /images/{owner_id}/{kind}/{name}: a picture is served to its owner only (LLD 3.3).

Everything that is not the caller's own picture is the same 404 as a missing one."""

from typing import Annotated, Final

from fastapi import APIRouter, Path, Request
from starlette.responses import Response

from kyapehnu.auth.public import CurrentUser
from kyapehnu.core.deps import CtxDep, PathId
from kyapehnu.core.errors import NotFoundError
from kyapehnu.files.keys import KIND_PATTERN, NAME_PATTERN, key

CACHE_CONTROL: Final = "private, no-cache"

router = APIRouter(tags=["images"], include_in_schema=False)


@router.api_route("/images/{owner_id}/{kind}/{name}", methods=["GET", "HEAD"])
def get_image(
    request: Request,
    owner_id: PathId,
    kind: Annotated[str, Path(pattern=KIND_PATTERN)],
    name: Annotated[str, Path(pattern=NAME_PATTERN)],
    user: CurrentUser,
    ctx: CtxDep,
) -> Response:
    if owner_id != user.id:
        raise NotFoundError("Image not found")
    response = ctx.files.serve(key(owner_id, kind, name), request)
    # ETag revalidation keeps repeat views cheap; without this a browser or proxy could keep the picture after sign-out.
    response.headers["Cache-Control"] = CACHE_CONTROL
    return response
