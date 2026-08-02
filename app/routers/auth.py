from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordRequestForm

from app.auth import (
    get_user_by_phone,
    create_access_token,
    resolve_user_role,
    verify_login_otp,
    CurrentUser,
    DbSession,
)
from app.schemas import Token, LoginRequest, UserOut
from app.serializers import user_out

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login_user(db, phone: str, otp: str) -> Token:
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(status_code=401, detail="Phone number not found")
    verify_login_otp(db, user, otp)
    user.lastLoginAt = datetime.utcnow()
    db.commit()
    role = resolve_user_role(db, user)
    token = create_access_token({"sub": str(user.id), "role": role})
    return Token(access_token=token)


@router.post("/login", response_model=Token)
def login_form(db: DbSession, form: OAuth2PasswordRequestForm = Depends()):
    # OAuth2 form: username = phone, password = OTP
    return _login_user(db, form.username, form.password)


@router.post("/login/json", response_model=Token)
def login_json(payload: LoginRequest, db: DbSession):
    return _login_user(db, payload.phone, payload.otp)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser, db: DbSession):
    role = resolve_user_role(db, user)
    return user_out(user, role=role)
