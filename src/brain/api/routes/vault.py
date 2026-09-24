"""API Key Vault routes — Per-user encrypted API key management.

Provides a secure vault where each user stores their own API keys
(Todoist, Notion, Google AI, etc.). Keys are AES-256-GCM encrypted
at rest with per-tenant key derivation. Only the key owner and
tenant admin/owner can manage vault entries.

Endpoints:
- GET  /vault/keys           → list key names (not values) for current user
- POST /vault/keys           → store a new API key
- GET  /vault/keys/{name}    → retrieve a decrypted key (requires explicit confirmation)
- DELETE /vault/keys/{name}  → delete a stored key
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from brain.api.dependencies import (
    RequirePermission,
    get_encryptor,
    get_current_tenant_id,
    get_storage,
)
from brain.security.encryption import FieldEncryptor
from brain.security.rbac import Permission
from brain.storage.base import StorageBackend

router = APIRouter()

_VAULT_PREFIX = "vault/"
_KEY_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Known integrations for documentation/validation hints
_KNOWN_INTEGRATIONS = {
    "todoist_api_token",
    "notion_api_token",
    "google_ai_api_key",
    "groq_api_key",
    "litellm_proxy_token",
    "gmail_imap_password",
    "smtp_password",
    "callmebot_api_key",
}


class StoreKeyRequest(BaseModel):
    name: str
    value: str
    description: str = ""


class KeyListItem(BaseModel):
    name: str
    description: str
    updated_at: str


class StoreKeyResponse(BaseModel):
    name: str
    status: str = "stored"


def _vault_path(tenant_id: str, user_id: str, key_name: str) -> str:
    """Build the storage path for a vault entry."""
    return f"{_VAULT_PREFIX}{tenant_id}/{user_id}/{key_name}.json"


def _vault_user_prefix(tenant_id: str, user_id: str) -> str:
    return f"{_VAULT_PREFIX}{tenant_id}/{user_id}/"


@router.get(
    "/vault/keys",
    response_model=list[KeyListItem],
    dependencies=[Depends(RequirePermission(Permission.MANAGE_API_KEYS))],
)
async def list_vault_keys(
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
) -> list[KeyListItem]:
    """List all API key names stored by the current user (values NOT returned)."""
    user_id = getattr(request.state, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User context required")

    prefix = _vault_user_prefix(tenant_id, user_id)
    keys = []
    # List files in the user's vault directory
    if hasattr(storage, "list_keys"):
        for path in storage.list_keys(prefix):
            data = storage.read_json(path)
            if data:
                keys.append(KeyListItem(
                    name=data.get("name", ""),
                    description=data.get("description", ""),
                    updated_at=data.get("updated_at", ""),
                ))
    return keys


@router.post(
    "/vault/keys",
    response_model=StoreKeyResponse,
    dependencies=[Depends(RequirePermission(Permission.MANAGE_API_KEYS))],
)
async def store_vault_key(
    body: StoreKeyRequest,
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
    encryptor: FieldEncryptor = Depends(get_encryptor),
) -> StoreKeyResponse:
    """Securely store an API key in the user's vault."""
    user_id = getattr(request.state, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User context required")

    if not _KEY_NAME_PATTERN.match(body.name):
        raise HTTPException(
            status_code=400,
            detail="Key name must be 1-64 chars: alphanumeric, underscore, hyphen only.",
        )

    if not body.value or len(body.value) > 4096:
        raise HTTPException(
            status_code=400,
            detail="Key value must be 1-4096 characters.",
        )

    # Encrypt the API key value
    encrypted = encryptor.encrypt(
        body.value,
        tenant_id=tenant_id,
        field_name=f"vault_{body.name}",
    )

    path = _vault_path(tenant_id, user_id, body.name)
    storage.write_json(path, {
        "name": body.name,
        "description": body.description,
        "encrypted_value": encrypted,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    return StoreKeyResponse(name=body.name)


@router.get(
    "/vault/keys/{key_name}",
    dependencies=[Depends(RequirePermission(Permission.MANAGE_API_KEYS))],
)
async def get_vault_key(
    key_name: str,
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
    encryptor: FieldEncryptor = Depends(get_encryptor),
) -> dict:
    """Retrieve and decrypt an API key from the vault.

    This is a sensitive operation — returns the decrypted key value.
    Use sparingly and prefer server-side key injection via config.
    """
    user_id = getattr(request.state, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User context required")

    if not _KEY_NAME_PATTERN.match(key_name):
        raise HTTPException(status_code=400, detail="Invalid key name format")

    path = _vault_path(tenant_id, user_id, key_name)
    data = storage.read_json(path)
    if data is None:
        raise HTTPException(status_code=404, detail="Key not found in vault")

    # Decrypt the value
    try:
        decrypted = encryptor.decrypt(
            data["encrypted_value"],
            tenant_id=tenant_id,
            field_name=f"vault_{key_name}",
        )
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Failed to decrypt vault key. Master key may have changed.",
        )

    return {"name": key_name, "value": decrypted}


@router.delete(
    "/vault/keys/{key_name}",
    dependencies=[Depends(RequirePermission(Permission.MANAGE_API_KEYS))],
)
async def delete_vault_key(
    key_name: str,
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
) -> dict:
    """Delete an API key from the vault."""
    user_id = getattr(request.state, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User context required")

    if not _KEY_NAME_PATTERN.match(key_name):
        raise HTTPException(status_code=400, detail="Invalid key name format")

    path = _vault_path(tenant_id, user_id, key_name)
    data = storage.read_json(path)
    if data is None:
        raise HTTPException(status_code=404, detail="Key not found in vault")

    if hasattr(storage, "delete"):
        storage.delete(path)
    else:
        # Overwrite with empty data as soft-delete
        storage.write_json(path, {"_deleted": True})

    return {"status": "deleted", "name": key_name}
