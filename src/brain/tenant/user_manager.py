"""User manager — CRUD operations for multi-user tenants.

Manages user lifecycle within tenants:
- Google OAuth2 login/registration (auto-provision on first login)
- Role assignment and management
- User listing and deactivation

Storage: users stored as JSON in the tenant's storage partition.
Format: users/{user_id}.json
Index:  users/_index.json (google_sub → user_id mapping for fast lookup)
"""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from brain.security.encryption import FieldEncryptor
from brain.storage.base import StorageBackend
from brain.tenant.models import User, UserRole

logger = logging.getLogger(__name__)

_USERS_PREFIX = "users/"
_INDEX_KEY = "users/_index.json"


class UserManager:
    """Manages users within the multi-tenant platform."""

    def __init__(
        self,
        storage: StorageBackend,
        encryptor: FieldEncryptor,
    ) -> None:
        self._storage = storage
        self._encryptor = encryptor
        self._index_lock = threading.Lock()

    # ── Lookup ─────────────────────────────────────────────────────

    def get_user(self, user_id: str) -> User | None:
        """Get a user by their internal user_id."""
        path = f"{_USERS_PREFIX}{user_id}.json"
        data = self._storage.read_json(path)
        if data is None:
            return None
        return self._from_dict(data)

    def get_user_by_google_sub(self, google_sub: str) -> User | None:
        """Find a user by their Google subject identifier."""
        index = self._read_index()
        user_id = index.get(google_sub)
        if user_id is None:
            return None
        return self.get_user(user_id)

    def get_user_by_email(self, email: str) -> User | None:
        """Find a user by email (scans index)."""
        index = self._read_index()
        for _sub, uid in index.items():
            user = self.get_user(uid)
            if user and user.email.lower() == email.lower():
                return user
        return None

    def list_users(self, tenant_id: str) -> list[User]:
        """List all users belonging to a tenant."""
        index = self._read_index()
        users = []
        for _sub, uid in index.items():
            user = self.get_user(uid)
            if user and user.tenant_id == tenant_id:
                users.append(user)
        return users

    # ── Create / Update ────────────────────────────────────────────

    def create_user(
        self,
        tenant_id: str,
        google_sub: str,
        email: str,
        name: str = "",
        picture_url: str = "",
        user_role: UserRole = UserRole.MEMBER,
    ) -> User:
        """Create a new user and add to index."""
        user_id = self._generate_user_id()
        user = User(
            user_id=user_id,
            tenant_id=tenant_id,
            google_sub=google_sub,
            email=email,
            name=name,
            picture_url=picture_url,
            user_role=user_role,
        )
        self._save_user(user)
        self._update_index(google_sub, user_id)
        logger.info(
            "Created user %s (email=%s, tenant=%s, role=%s)",
            user_id, email, tenant_id, user_role.value,
        )
        return user

    def update_user_role(self, user_id: str, new_role: UserRole) -> User | None:
        """Change a user's role within their tenant."""
        user = self.get_user(user_id)
        if user is None:
            return None
        user.user_role = new_role
        self._save_user(user)
        logger.info("Updated user %s role to %s", user_id, new_role.value)
        return user

    def deactivate_user(self, user_id: str) -> bool:
        """Soft-deactivate a user (sets is_active=False)."""
        user = self.get_user(user_id)
        if user is None:
            return False
        user.is_active = False
        self._save_user(user)
        logger.info("Deactivated user %s", user_id)
        return True

    def record_login(self, user: User) -> User:
        """Update last_login timestamp and login count."""
        user.last_login = datetime.now(timezone.utc)
        user.login_count += 1
        self._save_user(user)
        return user

    def find_or_create_user(
        self,
        tenant_id: str,
        google_sub: str,
        email: str,
        name: str = "",
        picture_url: str = "",
        default_role: UserRole = UserRole.MEMBER,
    ) -> User:
        """Find existing user by Google sub, or create new one."""
        existing = self.get_user_by_google_sub(google_sub)
        if existing is not None:
            # Update profile info from Google (name, picture may change)
            existing.name = name or existing.name
            existing.picture_url = picture_url or existing.picture_url
            return self.record_login(existing)

        new_user = self.create_user(
            tenant_id=tenant_id,
            google_sub=google_sub,
            email=email,
            name=name,
            picture_url=picture_url,
            user_role=default_role,
        )
        return self.record_login(new_user)

    # ── Invite System ──────────────────────────────────────────────

    def create_invite(
        self,
        tenant_id: str,
        invited_email: str,
        role: UserRole = UserRole.MEMBER,
    ) -> str:
        """Create an invite token for a new user.

        Returns an invite code that can be shared. When the invited
        user logs in with Google, they'll be matched by email.
        """
        invite_code = secrets.token_urlsafe(24)
        invite_data = {
            "invite_code": invite_code,
            "tenant_id": tenant_id,
            "email": invited_email.lower(),
            "role": role.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "used": False,
        }
        path = f"invites/{invite_code}.json"
        self._storage.write_json(path, invite_data)
        logger.info(
            "Created invite %s for %s to tenant %s (role=%s)",
            invite_code, invited_email, tenant_id, role.value,
        )
        return invite_code

    def redeem_invite(self, invite_code: str, google_sub: str, email: str) -> dict | None:
        """Look up and consume an invite code.

        Returns invite data (tenant_id, role) if valid, None if invalid/used.
        """
        path = f"invites/{invite_code}.json"
        data = self._storage.read_json(path)
        if data is None:
            return None
        if data.get("used"):
            return None
        if data.get("email", "").lower() != email.lower():
            return None
        # Mark as used
        data["used"] = True
        data["redeemed_by"] = google_sub
        data["redeemed_at"] = datetime.now(timezone.utc).isoformat()
        self._storage.write_json(path, data)
        return data

    # ── Internal ───────────────────────────────────────────────────

    def _save_user(self, user: User) -> None:
        path = f"{_USERS_PREFIX}{user.user_id}.json"
        self._storage.write_json(path, self._to_dict(user))

    def _read_index(self) -> dict[str, str]:
        """Read the google_sub → user_id index."""
        data = self._storage.read_json(_INDEX_KEY)
        return data if isinstance(data, dict) else {}

    def _update_index(self, google_sub: str, user_id: str) -> None:
        """Add or update a mapping in the index (thread-safe)."""
        with self._index_lock:
            index = self._read_index()
            index[google_sub] = user_id
            self._storage.write_json(_INDEX_KEY, index)

    @staticmethod
    def _generate_user_id() -> str:
        return f"usr_{secrets.token_hex(8)}"

    @staticmethod
    def _to_dict(u: User) -> dict[str, Any]:
        return {
            "user_id": u.user_id,
            "tenant_id": u.tenant_id,
            "google_sub": u.google_sub,
            "email": u.email,
            "name": u.name,
            "picture_url": u.picture_url,
            "user_role": u.user_role.value,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat(),
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "login_count": u.login_count,
        }

    @staticmethod
    def _from_dict(d: dict[str, Any]) -> User:
        return User(
            user_id=d["user_id"],
            tenant_id=d["tenant_id"],
            google_sub=d["google_sub"],
            email=d["email"],
            name=d.get("name", ""),
            picture_url=d.get("picture_url", ""),
            user_role=UserRole(d.get("user_role", "member")),
            is_active=d.get("is_active", True),
            created_at=datetime.fromisoformat(d["created_at"])
            if d.get("created_at")
            else datetime.now(timezone.utc),
            last_login=datetime.fromisoformat(d["last_login"])
            if d.get("last_login")
            else None,
            login_count=d.get("login_count", 0),
        )
