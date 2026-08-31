"""通过标准SMTP发送只读运行状态报告，不依赖Mac图形界面。"""

import platform
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage

import psutil

from gold_crypto_quant.storage.execution_status import read_execution_safety_status
from gold_crypto_quant.storage.paper_simulation import read_paper_simulation_state
from gold_crypto_quant.storage.service_state import read_service_state

GATE_SERVICE_NAME = "GATE_TESTNET_MARKET_DATA"


@dataclass(frozen=True, slots=True)
class StatusEmailMessage:
    """不含任何认证信息的状态邮件标题和正文。"""

    subject: str
    body: str


def _validate_email_address(address: str) -> str:
    """拒绝空值、换行和明显不是单一邮箱地址的输入。"""
    normalized = address.strip()
    if (
        not normalized
        or "\n" in normalized
        or "\r" in normalized
        or normalized.count("@") != 1
    ):
        raise ValueError("email address is invalid")
    return normalized


def _format_bytes(value: int) -> str:
    """把字节数转换为便于邮件阅读的GiB。"""
    return f"{value / (1024**3):.2f} GiB"


def _format_uptime(seconds: float) -> str:
    """把系统运行秒数转换为天、小时和分钟。"""
    total_minutes = max(0, int(seconds // 60))
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    return f"{days}天 {hours}小时 {minutes}分钟"


def _build_system_status_lines(process_id: int | None, now: datetime) -> tuple[str, ...]:
    """读取Mac资源占用；采集异常时返回可读状态而不阻断邮件。"""
    try:
        # 调用短采样获得当前整机CPU占用，0.2秒不会影响一分钟行情轮询周期。
        cpu_percent = psutil.cpu_percent(interval=0.2)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        load_1m, load_5m, load_15m = psutil.getloadavg()
        boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
        uptime = (now.astimezone(UTC) - boot_time).total_seconds()
        memory_in_use = memory.total - memory.available
        lines = [
            "",
            "Mac服务器状态",
            f"主机：{platform.node() or '-'}",
            f"系统：macOS {platform.mac_ver()[0] or '-'} / {platform.machine()}",
            f"整机CPU：{cpu_percent:.1f}%",
            f"系统负载：{load_1m:.2f} / {load_5m:.2f} / {load_15m:.2f}",
            (
                f"内存：{_format_bytes(memory_in_use)} / {_format_bytes(memory.total)} "
                f"({memory.percent:.1f}%)"
            ),
            (
                f"系统盘：{_format_bytes(disk.used)} / {_format_bytes(disk.total)} "
                f"({disk.percent:.1f}%)"
            ),
            f"开机时长：{_format_uptime(uptime)}",
        ]
        if process_id is None:
            lines.append("量化进程：NOT_STARTED")
        else:
            # 调用进程级短采样，区分系统整体负载和量化服务自身资源占用。
            process = psutil.Process(process_id)
            process_cpu = process.cpu_percent(interval=0.1)
            process_memory = process.memory_info().rss
            process_memory_percent = process.memory_percent()
            lines.extend(
                (
                    f"量化进程PID：{process_id}",
                    f"量化进程CPU：{process_cpu:.1f}%",
                    (
                        f"量化进程内存：{_format_bytes(process_memory)} "
                        f"({process_memory_percent:.2f}%)"
                    ),
                )
            )
        return tuple(lines)
    except (OSError, psutil.Error, ValueError) as error:
        return ("", f"Mac服务器状态：采集失败（{type(error).__name__}）")


def build_gate_status_email(now: datetime) -> StatusEmailMessage:
    """只读取本地状态表，生成不含API密钥和数据库密码的邮件内容。"""
    service = read_service_state(GATE_SERVICE_NAME)
    safety = read_execution_safety_status(oanda_enabled=False)
    simulation = read_paper_simulation_state()
    local_time = now.astimezone()
    subject = f"[量化模拟] Gate状态 {local_time:%Y-%m-%d %H:%M}"
    service_status = service.status if service is not None else "NOT_STARTED"
    successful_cycles = service.successful_cycles if service is not None else 0
    last_error = service.last_error if service is not None else None
    simulation_status = simulation.status if simulation is not None else "NOT_STARTED"
    healthy_days = simulation.consecutive_healthy_days if simulation is not None else 0
    equity = safety.paper_account_equity or "NOT_INITIALIZED"
    service_process_id = service.process_id if service is not None else None
    report_lines = (
        f"报告时间：{local_time:%Y-%m-%d %H:%M:%S %z}",
        f"行情服务：{service_status}",
        f"成功轮次：{successful_cycles}",
        f"最近异常：{last_error or '-'}",
        f"健康行情流：{safety.healthy_market_streams}",
        f"异常行情流：{safety.unhealthy_market_streams}",
        f"Gate账户风控：{safety.gate_runtime_risk_state}",
        f"模拟账户风控：{safety.runtime_risk_state}",
        f"模拟权益：{equity}",
        f"活动订单：{safety.active_orders}",
        f"开放持仓：{safety.open_positions}",
        f"模拟成交：{safety.paper_trades}",
        f"已平仓：{safety.closed_paper_positions}",
        f"模拟监督：{simulation_status}",
        f"连续健康日：{healthy_days}/30",
        "真实交易：False",
        "交易所订单提交：False",
    ) + _build_system_status_lines(service_process_id, now)
    body = "\n".join(report_lines)
    return StatusEmailMessage(subject=subject, body=body)


def build_gate_event_email(
    now: datetime,
    *,
    event_title: str,
    event_lines: tuple[str, ...],
    severity: str = "INFO",
) -> StatusEmailMessage:
    """生成交易或异常事件邮件，并附带当时的账户与Mac状态。"""
    if now.tzinfo is None:
        raise ValueError("event email time must be timezone-aware")
    if severity not in {"INFO", "WARNING", "CRITICAL", "RECOVERED"}:
        raise ValueError("unsupported email event severity")
    service = read_service_state(GATE_SERVICE_NAME)
    safety = read_execution_safety_status(oanda_enabled=False)
    local_time = now.astimezone()
    process_id = service.process_id if service is not None else None
    subject = f"[量化{severity}] {event_title} {local_time:%Y-%m-%d %H:%M:%S}"
    body_lines = (
        f"事件时间：{local_time:%Y-%m-%d %H:%M:%S %z}",
        f"事件级别：{severity}",
        f"事件名称：{event_title}",
        "",
        *event_lines,
        "",
        "交易与风控状态",
        f"活动策略批准数：{safety.approved_qualifications}",
        f"活动订单：{safety.active_orders}",
        f"开放持仓：{safety.open_positions}",
        f"模拟权益：{safety.paper_account_equity or 'NOT_INITIALIZED'}",
        f"Gate账户风控：{safety.gate_runtime_risk_state}",
        f"模拟账户风控：{safety.runtime_risk_state}",
        "真实交易：False",
        "交易所订单提交：False",
    ) + _build_system_status_lines(process_id, now)
    return StatusEmailMessage(subject=subject, body="\n".join(body_lines))


def send_smtp_email(
    message: StatusEmailMessage,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
    recipient: str,
) -> None:
    """使用SSL SMTP发送邮件；认证信息只在内存中传给SMTP连接。"""
    normalized_sender = _validate_email_address(sender)
    normalized_recipient = _validate_email_address(recipient)
    email = EmailMessage()
    email["Subject"] = message.subject
    email["From"] = normalized_sender
    email["To"] = normalized_recipient
    email.set_content(message.body)
    try:
        # 调用系统证书库建立SSL连接，禁止明文传输邮箱授权码。
        with smtplib.SMTP_SSL(
            host,
            port,
            context=ssl.create_default_context(),
            timeout=30,
        ) as client:
            # 调用SMTP认证时使用专用授权码，绝不使用或记录网页登录密码。
            client.login(username, password)
            client.send_message(email)
    except (OSError, smtplib.SMTPException) as error:
        raise RuntimeError("SMTP failed to send the status report") from error
