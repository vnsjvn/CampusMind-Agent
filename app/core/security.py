import base64
import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.entities import UserAccount


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(password), hashed)


def _credentials(request: Request) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Basic authorization")
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Basic authorization") from exc


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserAccount:
    username, password = _credentials(request)
    user = db.query(UserAccount).filter(UserAccount.username == username).first()
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad credentials")
    return user


def require_admin(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    if "ROLE_ADMIN" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user

