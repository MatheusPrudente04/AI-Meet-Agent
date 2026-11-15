"""Cadastro, login e sessão (ADR-011)."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.security import Principal, current_user
from presentation.container import get_container
from use_cases.auth import MIN_PASSWORD, AuthError

router = APIRouter()


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Maria Souza"])
    email: str = Field(max_length=254, examples=["maria@empresa.com"])
    password: str = Field(min_length=MIN_PASSWORD, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254, examples=["maria@empresa.com"])
    password: str = Field(max_length=200)


class UserSchema(BaseModel):
    id: str
    name: str
    email: str
    created_at: datetime


class SessionResponse(BaseModel):
    token: str = Field(description="Enviar em `Authorization: Bearer <token>` nas demais rotas.")
    user: UserSchema


def _auth():
    auth = get_container().auth
    if auth is None:
        raise HTTPException(503, "Login indisponível neste modo (APP_MODE=solo não tem usuários)")
    return auth


def _user_schema(user) -> UserSchema:
    return UserSchema(id=user.id, name=user.name, email=user.email, created_at=user.created_at)


def _client(request: Request) -> tuple[str, str]:
    return request.headers.get("user-agent", ""), request.client.host if request.client else ""


@router.post("/register", response_model=SessionResponse, status_code=201, summary="Cadastra um usuário",
             responses={409: {"description": "E-mail já cadastrado, dado inválido ou cadastro fechado"}})
def register(body: RegisterRequest, request: Request):
    """Cria a conta e já devolve uma sessão aberta."""
    _auth()
    try:
        user = get_container().register_user(body.name, body.email, body.password)
    except AuthError as exc:
        raise HTTPException(409, str(exc))
    return SessionResponse(token=_auth().open_session(user, *_client(request)), user=_user_schema(user))


@router.post("/login", response_model=SessionResponse, summary="Abre uma sessão",
             responses={401: {"description": "E-mail ou senha incorretos"}})
def login(body: LoginRequest, request: Request):
    try:
        token, user = _auth().login(body.email, body.password, *_client(request))
    except AuthError as exc:
        raise HTTPException(401, str(exc))
    return SessionResponse(token=token, user=_user_schema(user))


@router.post("/logout", status_code=204, summary="Encerra a sessão atual")
def logout(principal: Principal = Depends(current_user)):
    _auth().logout(principal.token)


@router.get("/me", response_model=UserSchema, summary="Usuário da sessão")
def me(principal: Principal = Depends(current_user)):
    return _user_schema(principal.user)
