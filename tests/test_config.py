"""配置默认值和实盘安全锁测试。"""

import pytest
from pydantic import ValidationError

from gold_crypto_quant.config import Settings


def test_live_trading_is_rejected() -> None:
    # 显式尝试开启实盘，确认 Settings 校验器在程序启动前就拒绝危险配置。
    with pytest.raises(ValidationError, match="LIVE_TRADING must remain false"):
        Settings(live_trading=True)


def test_project_defaults_match_phase_one_rules() -> None:
    # 构造当前有效配置，验证代码默认值与 .env 合并后仍符合第一阶段规则。
    settings = Settings()
    assert settings.symbols == ("BTC/USDT", "ETH/USDT")
    assert settings.intervals == ("5m", "15m", "30m", "1h")
    assert settings.leverage == 125
    assert settings.daily_loss_limit == 0.02
    assert settings.max_drawdown_limit == 0.08
    assert settings.database_url.startswith("mysql+pymysql://")
    assert "charset=utf8mb4" in settings.database_url
    assert settings.gate_testnet_base_url == "https://api-testnet.gateapi.io/api/v4"
    assert settings.oanda_enabled is False
    assert settings.oanda_practice_base_url == "https://api-fxpractice.oanda.com"
