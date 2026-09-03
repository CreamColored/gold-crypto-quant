"""对比不同代理节点下交易所链路的实际表现，结果存档以便切换节点后直接比较。

用法：

    .venv/bin/python scripts/benchmark_network.py 日本
    # 切换节点后
    .venv/bin/python scripts/benchmark_network.py 香港
    # 比较
    .venv/bin/python scripts/benchmark_network.py --compare

测的是这套系统实际依赖的三件事：
1. REST K线延迟——双行情服务每轮要发30次，中位数直接乘以30就是每轮的HTTP开销
2. WebSocket 帧率——盘口采集器的数据密度，帧率低意味着秒级极值更容易漏掉插针
3. 失败率——比延迟更要紧，2026-09-03 下午币安那次 ProxyError 503 就是链路问题

只访问公开行情接口，不涉及任何账户信息。
"""

import argparse
import asyncio
import json
import statistics
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

OUTPUT_DIR = Path("var/network")
SAMPLES = 12
WS_SECONDS = 10
REST_TARGETS = {
    "Gate 永续K线": "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
                    "?contract=BTC_USDT&interval=1m&limit=100",
    "币安 永续K线": "https://fapi.binance.com/fapi/v1/klines"
                    "?symbol=BTCUSDT&interval=1m&limit=100",
}
WS_TARGETS = {
    "Gate 永续盘口": ("wss://fx-ws.gateio.ws/v4/ws/usdt", "futures.book_ticker"),
    "币安 永续盘口": ("wss://fstream.binance.com/ws/btcusdt@bookTicker", None),
}


def describe_exit() -> dict:
    try:
        request = urllib.request.Request(
            "https://www.cloudflare.com/cdn-cgi/trace", headers={"User-Agent": "bench/1.0"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(400).decode("utf-8", "replace")
        fields = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
        return {"ip": fields.get("ip", "?"), "loc": fields.get("loc", "?"),
                "colo": fields.get("colo", "?")}
    except Exception as error:  # noqa: BLE001 - 取不到出口信息不影响其余测量
        return {"ip": f"取不到（{type(error).__name__}）", "loc": "?", "colo": "?"}


def measure_rest(url: str) -> dict:
    """连打若干次，报中位数与p95。失败次数比延迟更值得看。"""
    samples: list[float] = []
    failures = 0
    for _ in range(SAMPLES):
        started = time.perf_counter()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "bench/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
            samples.append((time.perf_counter() - started) * 1000)
        except Exception:  # noqa: BLE001 - 失败本身就是结论
            failures += 1
    if not samples:
        return {"failures": failures, "samples": 0}
    ordered = sorted(samples)
    return {
        "median_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "min_ms": round(ordered[0], 1),
        "failures": failures,
        "samples": len(samples),
    }


async def measure_ws(url: str, channel: str | None) -> dict:
    import websockets

    started = time.perf_counter()
    frames = 0
    handshake = None
    try:
        async with websockets.connect(url, open_timeout=20) as socket:
            handshake = (time.perf_counter() - started) * 1000
            if channel:
                await socket.send(json.dumps({
                    "time": int(time.time()), "channel": channel,
                    "event": "subscribe", "payload": ["BTC_USDT"],
                }))
            while time.perf_counter() - started < WS_SECONDS:
                remaining = WS_SECONDS - (time.perf_counter() - started)
                try:
                    await asyncio.wait_for(socket.recv(), timeout=max(0.3, remaining))
                except TimeoutError:
                    break
                frames += 1
    except Exception as error:  # noqa: BLE001 - 连不上也是结论
        return {"error": f"{type(error).__name__}: {str(error)[:60]}"}
    return {
        "handshake_ms": round(handshake, 1) if handshake else None,
        "frames": frames,
        "frames_per_second": round(frames / WS_SECONDS, 1),
    }


async def run(label: str) -> dict:
    print(f"节点标签：{label}")
    exit_info = describe_exit()
    print(f"出口 IP {exit_info['ip']}    国家 {exit_info['loc']}    机房 {exit_info['colo']}\n")

    rest: dict = {}
    print(f"--- REST 延迟（各打{SAMPLES}次）---")
    for name, url in REST_TARGETS.items():
        result = measure_rest(url)
        rest[name] = result
        if result["samples"]:
            print(f"  {name:14s} 中位 {result['median_ms']:>7.1f} ms   "
                  f"p95 {result['p95_ms']:>7.1f} ms   最快 {result['min_ms']:>7.1f} ms   "
                  f"失败 {result['failures']}/{SAMPLES}")
        else:
            print(f"  {name:14s} 全部失败（{result['failures']}/{SAMPLES}）")

    websocket: dict = {}
    print(f"\n--- WebSocket 帧率（各{WS_SECONDS}秒）---")
    for name, (url, channel) in WS_TARGETS.items():
        result = await measure_ws(url, channel)
        websocket[name] = result
        if "error" in result:
            print(f"  {name:14s} ❌ {result['error']}")
        else:
            print(f"  {name:14s} 握手 {result['handshake_ms']:>6.1f} ms   "
                  f"{result['frames']:>6,} 帧   {result['frames_per_second']:>7.1f} 帧/秒")

    payload = {
        "label": label,
        "measured_at": datetime.now(UTC).isoformat(),
        "exit": exit_info,
        "rest": rest,
        "websocket": websocket,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_DIR / f"{label}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已存档 {target}")

    cycle = sum(
        item["median_ms"] for item in rest.values() if item.get("median_ms")
    ) / max(1, len([i for i in rest.values() if i.get("median_ms")]))
    print(f"\n推算：双行情服务每轮30次HTTP，按中位 {cycle:.0f} ms 计约 {cycle * 30 / 1000:.1f} 秒")
    return payload


def compare() -> int:
    files = sorted(OUTPUT_DIR.glob("*.json")) if OUTPUT_DIR.exists() else []
    if len(files) < 2:
        print(f"至少需要两份存档才能比较，当前 {len(files)} 份于 {OUTPUT_DIR}")
        return 1
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    labels = [item["label"] for item in reports]
    print(f"{'指标':22s}" + "".join(f"{label:>16s}" for label in labels))
    print("-" * (22 + 16 * len(labels)))
    for name in REST_TARGETS:
        row = "".join(
            f"{item['rest'].get(name, {}).get('median_ms', '—')!s:>16}" for item in reports
        )
        print(f"{name + ' 中位ms':22s}{row}")
        row = "".join(
            f"{item['rest'].get(name, {}).get('failures', '—')!s:>16}" for item in reports
        )
        print(f"{name + ' 失败次数':22s}{row}")
    for name in WS_TARGETS:
        row = "".join(
            f"{item['websocket'].get(name, {}).get('frames_per_second', '—')!s:>16}"
            for item in reports
        )
        print(f"{name + ' 帧/秒':22s}{row}")
    print()
    for item in reports:
        print(f"  {item['label']}：{item['exit']['ip']} / {item['exit']['loc']} "
              f"/ {item['exit']['colo']}   测于 {item['measured_at'][:19]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="代理节点网络基准")
    parser.add_argument("label", nargs="?", help="节点标签，例如 日本 / 香港")
    parser.add_argument("--compare", action="store_true", help="比较已存档的全部节点")
    args = parser.parse_args()
    if args.compare:
        return compare()
    if not args.label:
        parser.print_help()
        return 2
    asyncio.run(run(args.label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
