"""Web监管后台密码安全和登录页测试。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gold_crypto_quant.storage.web_admin import (
    hash_password,
    validate_password_strength,
    verify_password,
)


def test_scrypt_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("Correct-Horse-2026")
    second = hash_password("Correct-Horse-2026")

    assert first != second
    assert first.startswith("scrypt$")
    assert verify_password("Correct-Horse-2026", first)
    assert not verify_password("wrong-password", first)


@pytest.mark.parametrize(
    "password",
    [
        "Abcdef12",  # 大写+小写+数字，8位
        "r@In4ugust",  # 小写+数字+标点
        "PASSWORD1!",  # 大写+数字+标点
    ],
)
def test_password_strength_accepts_three_of_four_classes(password: str) -> None:
    validate_password_strength(password)


@pytest.mark.parametrize(
    "password",
    [
        "abcdefgh",  # 8位，只有小写一类
        "abcd1234",  # 8位，只有小写+数字两类
        "Ab1!cde",  # 7位，四类齐全但长度不足8位
    ],
)
def test_password_strength_rejects_weak_passwords(password: str) -> None:
    with pytest.raises(ValueError):
        validate_password_strength(password)


def test_login_page_is_public_but_dashboard_requires_session(monkeypatch, tmp_path) -> None:
    import gold_crypto_quant.web.app as web_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web_app, "create_schema", lambda _engine: None)
    monkeypatch.setattr(web_app, "ensure_system_shadow_accounts", lambda _engine: None)
    monkeypatch.setattr(web_app, "create_admin_if_missing", lambda _engine: None)
    app = web_app.create_app(engine=object())

    with TestClient(app, follow_redirects=False) as client:
        login = client.get("/login")
        dashboard = client.get("/")

    assert login.status_code == 200
    assert "超级管理员" in login.text
    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/login"


def test_admin_session_can_open_read_only_dashboard(monkeypatch, tmp_path) -> None:
    import gold_crypto_quant.web.app as web_app

    fake_admin = SimpleNamespace(
        id=1,
        username="admin",
        display_name="超级管理员",
        role="SUPER_ADMIN",
        is_active=True,
        force_password_change=False,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web_app, "create_schema", lambda _engine: None)
    monkeypatch.setattr(web_app, "ensure_system_shadow_accounts", lambda _engine: None)
    monkeypatch.setattr(web_app, "create_admin_if_missing", lambda _engine: None)
    monkeypatch.setattr(web_app, "authenticate_admin", lambda *_args, **_kwargs: fake_admin)
    monkeypatch.setattr(web_app, "get_active_admin", lambda *_args, **_kwargs: fake_admin)
    monkeypatch.setattr(web_app, "save_admin_audit", lambda **_kwargs: None)
    app = web_app.create_app(engine=object())

    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={"username": "admin", "password": "test-only-password"},
        )

    assert response.status_code == 200
    assert "Moon监管中心" in response.text
    assert "LIVE_TRADING=false" in response.text
