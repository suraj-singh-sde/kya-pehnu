"""What the other domains may use from the auth domain (LLD 3.4)."""

from kyapehnu.auth.accounts import user_id_for_email
from kyapehnu.auth.deps import AdminUser, CurrentUser, Principal, admin_user

__all__ = ["AdminUser", "CurrentUser", "Principal", "admin_user", "user_id_for_email"]
