"""通过标准SMTP发送只读运行状态报告，不依赖Mac图形界面。"""

import platform
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from html import escape
from io import BytesIO
from pathlib import Path

import psutil

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.storage.execution_status import read_execution_safety_status
from gold_crypto_quant.storage.paper_simulation import read_paper_simulation_state
from gold_crypto_quant.storage.service_state import read_service_state

GATE_SERVICE_NAME = "GATE_TESTNET_MARKET_DATA"


@dataclass(frozen=True, slots=True)
class InlineEmailImage:
    """HTML邮件中通过CID引用的内嵌图片。"""

    content: bytes
    cid: str
    filename: str
    subtype: str = "png"


@dataclass(frozen=True, slots=True)
class StatusEmailMessage:
    """不含任何认证信息的邮件；纯文本正文用于兼容不支持HTML的客户端。"""

    subject: str
    body: str
    html_body: str | None = None
    inline_images: tuple[InlineEmailImage, ...] = ()


def _validate_email_address(address: str) -> str:
    """拒绝空值、换行和明显不是单一邮箱地址的输入。"""
    normalized = address.strip()
    if not normalized or "\n" in normalized or "\r" in normalized or normalized.count("@") != 1:
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
        # 调用项目所在路径的数据卷，而不是macOS只读系统卷“/”。APFS会把两者
        # 分开统计；量化数据库、日志和代码都位于数据卷，后者才是有意义的容量。
        disk_path = Path.cwd()
        disk = psutil.disk_usage(str(disk_path))
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
                f"数据盘：总容量 {_format_bytes(disk.total)}，"
                f"已用 {_format_bytes(disk.used)}，可用 {_format_bytes(disk.free)}，"
                f"使用率 {disk.percent:.1f}%"
            ),
            f"磁盘检测路径：{disk_path}",
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


def _parse_display_lines(lines: tuple[str, ...]) -> list[tuple[str, str]]:
    """把“名称：值”文本转换为HTML表格行，无法拆分的内容作为说明文字。"""
    rows: list[tuple[str, str]] = []
    for line in lines:
        normalized = line.strip()
        if not normalized:
            continue
        if "：" in normalized:
            label, value = normalized.split("：", 1)
            rows.append((label.strip(), value.strip() or "-"))
        else:
            rows.append(("", normalized))
    return rows


def _html_rows(lines: tuple[str, ...]) -> str:
    """生成兼容QQ邮箱的简单表格，所有动态内容都先进行HTML转义。"""
    cells: list[str] = []
    for label, value in _parse_display_lines(lines):
        if not label:
            cells.append(
                '<tr><td colspan="2" style="padding:10px 14px;color:#334155;'
                f'font-weight:700;background:#f1f5f9;">{escape(value)}</td></tr>'
            )
            continue
        cells.append(
            '<tr><td style="padding:9px 14px;color:#64748b;border-bottom:1px solid #eef2f7;'
            f'width:42%;">{escape(label)}</td><td style="padding:9px 14px;color:#0f172a;'
            f'font-weight:600;border-bottom:1px solid #eef2f7;">{escape(value)}</td></tr>'
        )
    return "".join(cells)


def _build_html_document(
    *,
    title: str,
    subtitle: str,
    severity: str,
    primary_lines: tuple[str, ...],
    status_lines: tuple[str, ...],
    system_lines: tuple[str, ...],
    chart_cid: str | None = None,
) -> str:
    """生成移动端友好的卡片式HTML邮件，使用内联样式兼容QQ邮箱。"""
    colors = {
        "INFO": ("#2563eb", "#eff6ff"),
        "WARNING": ("#d97706", "#fffbeb"),
        "CRITICAL": ("#dc2626", "#fef2f2"),
        "RECOVERED": ("#059669", "#ecfdf5"),
    }
    accent, pale = colors.get(severity, colors["INFO"])
    chart = ""
    if chart_cid:
        chart = (
            '<div style="margin-top:18px;background:#ffffff;border:1px solid #e2e8f0;'
            'border-radius:12px;padding:14px;">'
            '<div style="font-size:15px;font-weight:700;color:#0f172a;margin-bottom:10px;">'
            "近期价格与布林带</div>"
            f'<img src="cid:{escape(chart_cid)}" alt="价格与布林带折线图" '
            'style="display:block;width:100%;max-width:610px;height:auto;border-radius:8px;">'
            '<div style="font-size:12px;color:#94a3b8;margin-top:8px;">'
            "蓝线为收盘价，红/灰/绿线依次为上轨、中轨、下轨；图表仅用于模拟监控。</div></div>"
        )
    body_style = (
        "margin:0;padding:0;background:#f3f6fa;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;"
    )
    card_style = (
        "max-width:640px;background:#ffffff;border-radius:16px;overflow:hidden;"
        "box-shadow:0 6px 24px rgba(15,23,42,.08);"
    )
    table_style = "border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;"
    section_style = "font-size:15px;font-weight:700;color:#0f172a;margin:20px 0 9px;"
    return (
        '<!doctype html><html><body style="'
        f'{body_style}"><table role="presentation" width="100%" cellspacing="0" '
        'cellpadding="0" style="background:#f3f6fa;"><tr><td align="center" '
        'style="padding:20px 10px;"><table role="presentation" width="100%" '
        f'cellspacing="0" cellpadding="0" style="{card_style}"><tr><td '
        f'style="padding:22px 24px;background:{pale};border-top:5px solid {accent};">'
        '<div style="display:inline-block;padding:4px 9px;border-radius:999px;'
        f'background:{accent};color:#ffffff;font-size:12px;font-weight:700;">'
        f'{escape(severity)}</div><div style="margin-top:10px;font-size:22px;'
        f'line-height:1.35;font-weight:800;color:#0f172a;">{escape(title)}</div>'
        '<div style="margin-top:6px;font-size:13px;color:#64748b;">'
        f'{escape(subtitle)}</div></td></tr><tr><td style="padding:20px 20px 6px;">'
        '<div style="font-size:15px;font-weight:700;color:#0f172a;margin-bottom:9px;">'
        f'事件详情</div><table role="presentation" width="100%" cellspacing="0" '
        f'cellpadding="0" style="{table_style}">{_html_rows(primary_lines)}</table>'
        f'{chart}<div style="{section_style}">交易与风控</div>'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="{table_style}">{_html_rows(status_lines)}</table>'
        f'<div style="{section_style}">Mac 服务器</div>'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="{table_style}">{_html_rows(system_lines)}</table>'
        '<div style="padding:18px 2px 20px;color:#94a3b8;font-size:12px;line-height:1.6;">'
        "LIVE_TRADING=false · 交易所订单提交=false<br>"
        "本邮件由本地模拟交易系统自动发送。</div>"
        "</td></tr></table></td></tr></table></body></html>"
    )


def _extract_event_value(lines: tuple[str, ...], label: str) -> str | None:
    """从事件明细中读取指定字段，供图表选择周期和标注成交价。"""
    prefix = f"{label}："
    for line in lines:
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return None


def _build_market_chart_png(
    *,
    event_title: str,
    event_lines: tuple[str, ...],
    venue: str = GATE_TESTNET_VENUE,
) -> bytes | None:
    """为交易事件生成近期价格和布林带PNG；失败时省略图表且不阻断告警邮件。"""
    if not any(keyword in event_title for keyword in ("开仓", "减仓", "平仓", "成交")):
        return None
    symbol = _extract_event_value(event_lines, "品种")
    if symbol is None:
        symbol = next((part for part in event_title.split() if "_USDT" in part), "ETH_USDT")
    interval = _extract_event_value(event_lines, "交易周期") or "15m"
    if interval not in {"5m", "15m", "30m", "1h"}:
        interval = "15m"
    try:
        # 绘图依赖按需导入，普通状态/异常邮件不会承担matplotlib启动成本。
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt

        from gold_crypto_quant.storage.market_bars import load_market_bars
        from gold_crypto_quant.strategy.bollinger_range import (
            build_rotation_box_context,
            parameters_for_same_timeframe,
        )

        # 双行情对照时必须读取事件所属交易所的数据，不能继续默认使用Gate测试网。
        bars = load_market_bars(symbol, interval, limit=80, venue=venue)
        context = build_rotation_box_context(bars, parameters_for_same_timeframe(interval))
        display = bars.join(context[["bb_upper", "bb_middle", "bb_lower"]]).tail(48).dropna()
        if display.empty:
            return None
        local_index = display.index.tz_convert("Asia/Shanghai")
        fig, axis = plt.subplots(figsize=(8.4, 3.8), dpi=150)
        fig.patch.set_facecolor("#ffffff")
        axis.set_facecolor("#f8fafc")
        axis.plot(local_index, display["close"], color="#2563eb", linewidth=1.8, label="Close")
        axis.plot(local_index, display["bb_upper"], color="#ef4444", linewidth=1.1, label="Upper")
        axis.plot(local_index, display["bb_middle"], color="#64748b", linewidth=1.0, label="Middle")
        axis.plot(local_index, display["bb_lower"], color="#16a34a", linewidth=1.1, label="Lower")
        price_text = (
            _extract_event_value(event_lines, "成交价格")
            or _extract_event_value(event_lines, "轨道限价")
            or _extract_event_value(event_lines, "成交参考价")
        )
        if price_text:
            try:
                event_price = float(price_text.split()[0])
                axis.axhline(
                    event_price,
                    color="#f59e0b",
                    linestyle="--",
                    linewidth=1.0,
                    label="Event",
                )
            except ValueError:
                pass
        axis.grid(axis="y", color="#e2e8f0", linewidth=0.7)
        axis.spines[["top", "right", "left"]].set_visible(False)
        axis.spines["bottom"].set_color("#cbd5e1")
        axis.tick_params(colors="#64748b", labelsize=8)
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M", tz=local_index.tz))
        axis.legend(loc="best", frameon=False, fontsize=8, ncol=5)
        axis.set_title(
            f"{symbol} · {interval} · Asia/Shanghai",
            loc="left",
            fontsize=11,
            weight="bold",
        )
        fig.tight_layout()
        buffer = BytesIO()
        fig.savefig(buffer, format="png", bbox_inches="tight")
        plt.close(fig)
        return buffer.getvalue()
    except Exception:
        # 邮件属于安全通知通道；行情、数据库或绘图异常不能导致交易告警丢失。
        return None


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
    primary_lines = (
        f"报告时间：{local_time:%Y-%m-%d %H:%M:%S %z}",
        f"行情服务：{service_status}",
        f"成功轮次：{successful_cycles}",
        f"最近异常：{last_error or '-'}",
    )
    status_lines = (
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
    )
    system_lines = _build_system_status_lines(service_process_id, now)
    report_lines = primary_lines + status_lines + system_lines
    body = "\n".join(report_lines)
    html_body = _build_html_document(
        title="Gate 模拟交易运行状态",
        subtitle=f"北京时间 {local_time:%Y-%m-%d %H:%M:%S}",
        severity="INFO",
        primary_lines=primary_lines,
        status_lines=status_lines,
        system_lines=system_lines,
    )
    return StatusEmailMessage(subject=subject, body=body, html_body=html_body)


def build_gate_event_email(
    now: datetime,
    *,
    event_title: str,
    event_lines: tuple[str, ...],
    severity: str = "INFO",
    venue: str = GATE_TESTNET_VENUE,
    comparison_status_lines: tuple[str, ...] | None = None,
    process_id_override: int | None = None,
) -> StatusEmailMessage:
    """生成交易或异常事件邮件，并附带当时的账户与Mac状态。"""
    if now.tzinfo is None:
        raise ValueError("event email time must be timezone-aware")
    if severity not in {"INFO", "WARNING", "CRITICAL", "RECOVERED"}:
        raise ValueError("unsupported email event severity")
    service = read_service_state(GATE_SERVICE_NAME)
    safety = read_execution_safety_status(oanda_enabled=False)
    local_time = now.astimezone()
    process_id = (
        process_id_override
        if process_id_override is not None
        else (service.process_id if service is not None else None)
    )
    subject = f"[量化{severity}] {event_title} {local_time:%Y-%m-%d %H:%M:%S}"
    primary_lines = (
        f"事件时间：{local_time:%Y-%m-%d %H:%M:%S %z}",
        f"事件级别：{severity}",
        f"事件名称：{event_title}",
        *event_lines,
    )
    status_lines = comparison_status_lines or (
        f"活动策略批准数：{safety.approved_qualifications}",
        f"活动订单：{safety.active_orders}",
        f"开放持仓：{safety.open_positions}",
        f"模拟权益：{safety.paper_account_equity or 'NOT_INITIALIZED'}",
        f"Gate账户风控：{safety.gate_runtime_risk_state}",
        f"模拟账户风控：{safety.runtime_risk_state}",
        "真实交易：False",
        "交易所订单提交：False",
    )
    system_lines = _build_system_status_lines(process_id, now)
    body_lines = primary_lines + ("", "交易与风控状态") + status_lines + system_lines
    chart_png = _build_market_chart_png(
        event_title=event_title,
        event_lines=event_lines,
        venue=venue,
    )
    chart_cid = "market-chart" if chart_png is not None else None
    html_body = _build_html_document(
        title=event_title,
        subtitle=f"北京时间 {local_time:%Y-%m-%d %H:%M:%S}",
        severity=severity,
        primary_lines=primary_lines,
        status_lines=status_lines,
        system_lines=system_lines,
        chart_cid=chart_cid,
    )
    images = (
        (InlineEmailImage(chart_png, chart_cid, "market-chart.png"),)
        if chart_png is not None and chart_cid is not None
        else ()
    )
    return StatusEmailMessage(
        subject=subject,
        body="\n".join(body_lines),
        html_body=html_body,
        inline_images=images,
    )


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
    if message.html_body is not None:
        # 调用multipart/alternative同时提供纯文本和HTML，QQ邮箱优先显示新版卡片。
        email.add_alternative(message.html_body, subtype="html")
        html_part = email.get_payload()[-1]
        for inline_image in message.inline_images:
            # 调用CID内嵌图片，收件人无需访问外部网址也能看到交易折线图。
            html_part.add_related(
                inline_image.content,
                maintype="image",
                subtype=inline_image.subtype,
                cid=f"<{inline_image.cid}>",
                filename=inline_image.filename,
                disposition="inline",
            )
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
