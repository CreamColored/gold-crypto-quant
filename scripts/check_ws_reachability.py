"""在任意一台机器上检查交易所的 WebSocket 实时行情能否收到推送。

用途：判断"币安期货 fstream 连得上却一帧不推"是不是出口IP或网络环境造成的。
Mac 上实测的现象是——同一个出口，币安现货WS、币安期货REST、Gate期货WS 三条都正常，
唯独 wss://fstream.binance.com 握手成功、订阅被接受、连接一直开着，但零数据。
换一台网络环境跑本脚本，就能判断那是不是该出口IP被单独限流。

用法（Windows / macOS / Linux 通用）：

    pip install websockets
    python check_ws_reachability.py

本脚本不读取也不发送任何账户信息，只访问公开行情接口。
"""

import asyncio
import json
import ssl
import sys
import time
import urllib.request

PROBE_SECONDS = 15
# 判定"有推送"的阈值：正常行情每秒都有成交，15秒内远不止5帧。
MIN_FRAMES_OK = 5


def http_get(url: str, timeout: float = 15.0) -> tuple:
    """用标准库发一个GET，返回(状态码, 正文前200字符)；失败返回(0, 错误说明)。"""
    request = urllib.request.Request(url, headers={"User-Agent": "ws-probe/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(200).decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001 - 任何失败都只作为检测结论
        return 0, f"{type(error).__name__}: {error}"


def show_exit() -> None:
    """报告当前出口IP与所在国家，用于和其他机器对照。"""
    status, body = http_get("https://www.cloudflare.com/cdn-cgi/trace")
    if status != 200:
        print(f"出口信息取不到：{body}")
        return
    fields = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
    print(
        f"出口 IP {fields.get('ip', '?')}    国家 {fields.get('loc', '?')}    "
        f"入口机房 {fields.get('colo', '?')}"
    )


def check_rest() -> None:
    """REST 作为对照：REST 通而 WS 不通，说明问题只在实时流这一路。"""
    print("\n--- REST 对照 ---")
    for label, url in (
        ("币安期货 klines", "https://fapi.binance.com/fapi/v1/klines"
                            "?symbol=BTCUSDT&interval=1m&limit=2"),
        ("Gate 期货 K线", "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
                          "?contract=BTC_USDT&interval=1m&limit=2"),
    ):
        started = time.perf_counter()
        status, body = http_get(url)
        elapsed = time.perf_counter() - started
        mark = "OK " if status == 200 else "失败"
        detail = "" if status == 200 else f"  {body[:90]}"
        print(f"  [{mark}] {label:16s} HTTP {status}  {elapsed:5.2f}s{detail}")


async def probe_ws(label: str, url: str, subscribe: dict | None) -> None:
    """连一个WebSocket，数15秒内收到多少帧；连接状态和关闭码一并打印。"""
    import websockets

    started = time.perf_counter()
    frames = 0
    first_sample = ""
    closed_note = ""
    try:
        # 不传 proxy 参数，沿用各版本 websockets 的默认行为，避免版本差异。
        async with websockets.connect(url, open_timeout=20) as socket:
            handshake = time.perf_counter() - started
            if subscribe is not None:
                await socket.send(json.dumps(subscribe))
            while time.perf_counter() - started < PROBE_SECONDS:
                remaining = PROBE_SECONDS - (time.perf_counter() - started)
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=max(0.5, remaining))
                except TimeoutError:
                    break
                except Exception as error:  # noqa: BLE001 - 关闭原因要如实呈现
                    closed_note = f"  连接被关闭：{type(error).__name__}: {error}"
                    break
                frames += 1
                if frames == 1:
                    first_sample = str(raw)[:100]
    except Exception as error:  # noqa: BLE001 - 握手失败同样是检测结论
        print(f"  [失败] {label:22s} {type(error).__name__}: {str(error)[:80]}")
        return

    verdict = "有推送" if frames >= MIN_FRAMES_OK else "无推送"
    mark = "OK " if frames >= MIN_FRAMES_OK else "!! "
    print(
        f"  [{mark}] {label:22s} 握手{handshake:5.2f}s  "
        f"{PROBE_SECONDS}秒收到 {frames:4d} 帧  → {verdict}{closed_note}"
    )
    if first_sample:
        print(f"           首帧 {first_sample}")


async def check_ws() -> None:
    print("\n--- WebSocket 实时推送 ---")
    # 目标：币安期货。其余三条都是对照组，用来排除"网络整体不通"这种解释。
    await probe_ws(
        "币安期货 fstream", "wss://fstream.binance.com/ws/btcusdt@aggTrade", None
    )
    await probe_ws(
        "币安期货 fstream K线", "wss://fstream.binance.com/ws/btcusdt@kline_1m", None
    )
    await probe_ws(
        "对照 币安现货 stream", "wss://stream.binance.com:9443/ws/btcusdt@aggTrade", None
    )
    await probe_ws(
        "对照 Gate 期货",
        "wss://fx-ws.gateio.ws/v4/ws/usdt",
        {
            "time": int(time.time()),
            "channel": "futures.trades",
            "event": "subscribe",
            "payload": ["BTC_USDT"],
        },
    )


def main() -> int:
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("缺少依赖，请先执行：  pip install websockets")
        return 2
    print(f"Python {sys.version.split()[0]}    SSL {ssl.OPENSSL_VERSION}")
    show_exit()
    check_rest()
    asyncio.run(check_ws())
    print(
        "\n怎么看结果：\n"
        "  币安期货两条有推送            → 该网络环境正常，Mac那边是出口IP被单独限流\n"
        "  币安期货无推送、其余三条正常    → 与出口无关，是币安对期货实时流的普遍限制\n"
        "  全部失败                     → 该机器网络不通，本次结果无参考价值"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
