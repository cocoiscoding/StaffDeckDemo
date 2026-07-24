from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.auth import (
    LoginRequest,
    UserCreateRequest,
    UserUpdateRequest,
    create_user,
    login,
    update_user,
)
from app.db.models import Tenant, User
from app.security.auth import hash_password


def test_unknown_login_does_not_create_account() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        try:
            login(LoginRequest(tenant_id="tenant_demo", username="missing", password="secret"), db)
        except HTTPException as error:
            assert error.status_code == 401
            assert error.detail == "Invalid username or password"
        else:
            raise AssertionError("unknown account must not be created during login")

        assert db.exec(select(User)).all() == []


def test_database_role_controls_account_management() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        member_named_admin = User(
            id="user_named_admin",
            tenant_id="tenant_demo",
            username="admin",
            role="member",
            password_hash=hash_password("secret"),
        )
        role_admin = User(
            id="user_role_admin",
            tenant_id="tenant_demo",
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(member_named_admin)
        db.add(role_admin)
        db.commit()

        try:
            create_user(
                UserCreateRequest(tenant_id="tenant_demo", username="blocked", password="secret"),
                member_named_admin,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 403
        else:
            raise AssertionError("an admin-looking username must not grant administrator access")

        created = create_user(
            UserCreateRequest(
                tenant_id="tenant_demo",
                username="created_admin",
                password="secret",
                role="admin",
            ),
            role_admin,
            db,
        )
        assert created.role == "admin"

        updated = update_user(
            created.id,
            UserUpdateRequest(tenant_id="tenant_demo", role="member"),
            role_admin,
            db,
        )
        assert updated.role == "member"


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
