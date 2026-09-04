"""集中管理运行配置和交易安全开关。"""

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从默认值和本地 ``.env`` 加载配置。

    环境变量优先于代码默认值。``.env`` 在本仓库里是被 Git 跟踪的——私有仓库，凭据
    集中在这一个文件里，不额外分层。
    """

    # Pydantic Settings 会自动读取项目根目录的 .env，并忽略暂未使用的扩展字段。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    live_trading: bool = False
    database_url: str = "mysql+pymysql://quant:quant@127.0.0.1:3306/quant?charset=utf8mb4"
    # 当前阶段只运行Gate的BTC和ETH；黄金代码保留，待实盘渠道明确后再启用。
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    intervals: tuple[str, ...] = ("5m", "15m", "30m", "1h")
    # 125 是系统上限，不代表每次下单固定使用 125 倍；还要受交易所品种上限约束。
    leverage: int = Field(default=125, ge=1, le=125)
    # 每笔最多亏损账户权益的 0.25%，仓位会根据入场价与止损价的距离反推。
    risk_per_trade: float = Field(default=0.0025, gt=0, le=0.02)
    # 当日累计亏损达到 2% 后禁止继续开仓。
    daily_loss_limit: float = Field(default=0.02, gt=0, le=0.10)
    # 策略累计回撤达到 8% 后触发总熔断。
    max_drawdown_limit: float = Field(default=0.08, gt=0, le=0.25)

    # Web监管后台默认只监听本机；需要通过Tailscale等安全内网访问时再显式调整监听地址。
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8765, ge=1024, le=65535)
    web_cookie_secure: bool = False

    # 通过SMTP发送成交和重要异常事件；留空时完全禁用邮件功能。
    status_email_to: str | None = None
    # SMTP配置使用独立授权码，不允许复用邮箱网页登录密码。
    smtp_host: str | None = None
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None

    # 钉钉自定义机器人：Webhook与加签密钥都留空时完全禁用，不影响邮件通道。
    dingtalk_webhook: str | None = None
    dingtalk_secret: SecretStr | None = None

    # 宏观数据发布前后暂停开仓。2026-09-04 非农那一分钟 BTC 振幅1155点、量放大12倍，
    # 影子账户在数据发布30秒后开了两笔多单并当场止损——震荡策略在数据驱动的单边启动
    # 里必然吃亏，而发布时刻是已知的：时间是确定的，形态是猜的。
    # 设为0即关闭该功能。
    macro_blackout_before_minutes: int = Field(default=15, ge=0, le=180)
    macro_blackout_after_minutes: int = Field(default=15, ge=0, le=180)

    # Redis 承载策略进程要读的实时K线与现价快照；留空密码即视为免认证实例。
    # 采集服务写、量化服务读，两边共用同一份连接配置。
    redis_host: str = "127.0.0.1"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0, le=15)
    redis_password: SecretStr | None = None
    # 所有键统一前缀，和同一实例上其它应用的键空间隔开。
    redis_key_prefix: str = "gcq"

    # Gate 测试网与实盘使用不同密钥，SecretStr 可防止日志意外打印完整密钥。
    gate_testnet_api_key: SecretStr | None = None
    gate_testnet_api_secret: SecretStr | None = None
    gate_testnet_base_url: str = "https://api-testnet.gateapi.io/api/v4"
    # 默认暂停OANDA长期服务；显式命令仍可读取历史数据和检查账户。
    oanda_enabled: bool = False
    # OANDA令牌同样使用SecretStr，防止配置对象或异常日志输出完整认证信息。
    oanda_practice_token: SecretStr | None = None
    oanda_practice_account_id: str | None = None
    oanda_practice_base_url: str = "https://api-fxpractice.oanda.com"

    @model_validator(mode="after")
    def reject_live_trading(self) -> "Settings":
        """硬性禁止实盘开关，防止配置错误导致真实下单。"""
        if self.live_trading:
            raise ValueError("LIVE_TRADING must remain false during the paper-trading phase")
        if self.status_email_to and not all(
            (self.smtp_host, self.smtp_username, self.smtp_password, self.smtp_from)
        ):
            raise ValueError("SMTP settings are required when status email is enabled")
        return self


@lru_cache
def get_settings() -> Settings:
    """返回进程内唯一的配置对象，避免每次调用都重复读取 ``.env``。"""
    # Settings() 会合并代码默认值与 .env；lru_cache 确保本进程只构造一次。
    return Settings()
