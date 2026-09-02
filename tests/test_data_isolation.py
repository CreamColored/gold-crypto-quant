"""多用户数据隔离：普通用户不得看到系统影子账户或他人账户。"""

from types import SimpleNamespace

import pytest

from gold_crypto_quant.web.data import account_is_visible, is_super_admin

SUPER_ADMIN = SimpleNamespace(id=1, role="SUPER_ADMIN")
ALICE = SimpleNamespace(id=2, role="USER")
BOB = SimpleNamespace(id=3, role="USER")

SYSTEM_ACCOUNT = SimpleNamespace(
    account_code="SYSTEM_GATE", owner_user_id=None, owner_type="SYSTEM"
)
ALICE_ACCOUNT = SimpleNamespace(account_code="ALICE_GATE", owner_user_id=2, owner_type="USER")
BOB_ACCOUNT = SimpleNamespace(account_code="BOB_GATE", owner_user_id=3, owner_type="USER")


def test_super_admin_sees_every_account() -> None:
    for account in (SYSTEM_ACCOUNT, ALICE_ACCOUNT, BOB_ACCOUNT):
        assert account_is_visible(account, SUPER_ADMIN) is True


def test_plain_user_cannot_see_system_account() -> None:
    """系统影子账户归超管专属，普通用户登录后不该看到。"""
    assert account_is_visible(SYSTEM_ACCOUNT, ALICE) is False


def test_plain_user_cannot_see_another_users_account() -> None:
    assert account_is_visible(BOB_ACCOUNT, ALICE) is False
    assert account_is_visible(ALICE_ACCOUNT, BOB) is False


def test_plain_user_sees_own_account() -> None:
    assert account_is_visible(ALICE_ACCOUNT, ALICE) is True


@pytest.mark.parametrize(
    "viewer",
    [
        SimpleNamespace(id=2, role=""),
        SimpleNamespace(id=2, role="ADMIN"),
        SimpleNamespace(id=2),
        SimpleNamespace(id=2, role="super_admin"),
    ],
)
def test_only_exact_super_admin_role_is_privileged(viewer) -> None:
    """角色缺失、大小写不符或近似取值都不得当成超管。"""
    assert is_super_admin(viewer) is False
    assert account_is_visible(SYSTEM_ACCOUNT, viewer) is False


def test_unowned_account_is_not_visible_to_plain_user() -> None:
    """owner_user_id 为空的账户不能因为“都是None”而被误判为归属某人。"""
    orphan = SimpleNamespace(account_code="ORPHAN", owner_user_id=None, owner_type="USER")
    nobody = SimpleNamespace(id=None, role="USER")

    assert account_is_visible(orphan, nobody) is False
    assert account_is_visible(orphan, ALICE) is False
