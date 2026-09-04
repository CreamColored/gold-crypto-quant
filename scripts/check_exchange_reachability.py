"""检查当前网络出口能否同时访问 Gate 与币安的公开行情。

切换代理节点后运行本脚本，可以确认该节点是否两个交易所都支持：

    .venv/bin/python scripts/check_exchange_reachability.py

用项目自己的交易所客户端发请求，与量化服务走完全相同的代码路径和代理设置，
因此结论可以直接套用到线上；用 curl 另测可能因为代理规则不同而得出不一致的结果。
"""

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gold_crypto_quant.exchanges.binance import BinancePublicClient  # noqa: E402
from gold_crypto_quant.exchanges.gate import GatePublicClient  # noqa: E402

CONTRACTS = ("BTC_USDT", "ETH_USDT", "XAU_USDT")
INTERVAL = "5m"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


def describe_exit() -> str:
    """报告当前出口IP与所在国家；失败不影响后续交易所检测。"""
    try:
        with httpx.Client(timeout=10.0) as client:
            body = client.get(TRACE_URL).text
    except httpx.HTTPError as error:
        return f"出口信息不可用：{type(error).__name__}"
    fields = dict(
        line.split("=", 1) for line in body.splitlines() if "=" in line
    )
    return (
        f"出口 IP {fields.get('ip', '?')}　国家 {fields.get('loc', '?')}　"
        f"入口机房 {fields.get('colo', '?')}"
    )


def check(label: str, calls: dict) -> bool:
    """逐项调用并打印耗时；任一项失败即判定该交易所在当前节点不可用。"""
    print(f"\n[{label}]")
    ok = True
    for name, call in calls.items():
        started = time.perf_counter()
        try:
            call()
        except Exception as error:  # noqa: BLE001 - 任何异常都算该节点不可用
            ok = False
            print(
                f"  {name:26s} ❌ {time.perf_counter() - started:5.2f}s  "
                f"{type(error).__name__}: {str(error)[:80]}"
            )
        else:
            print(f"  {name:26s} ✅ {time.perf_counter() - started:5.2f}s")
    return ok


def main() -> int:
    print(describe_exit())
    with GatePublicClient() as gate:
        gate_ok = check(
            "Gate",
            {
                f"合约信息 {c}": (lambda c=c: gate.get_contract(c)) for c in CONTRACTS
            }
            | {
                f"K线 {c} {INTERVAL}": (
                    lambda c=c: gate.get_candlesticks(c, INTERVAL, limit=5)
                )
                for c in CONTRACTS
            },
        )
    with BinancePublicClient() as binance:
        binance_ok = check(
            "币安",
            {
                f"合约信息 {c}": (
                    lambda c=c: binance.get_exchange_symbol(c.replace("_", ""))
                )
                for c in CONTRACTS
            }
            | {
                f"K线 {c} {INTERVAL}": (
                    lambda c=c: binance.get_candlesticks(
                        c.replace("_", ""), INTERVAL, limit=5
                    )
                )
                for c in CONTRACTS
            },
        )
    both = gate_ok and binance_ok
    verdict = "两个交易所都可用" if both else "该节点不满足双所对照"
    print(
        f"\n结论：Gate {'✅' if gate_ok else '❌'}　"
        f"币安 {'✅' if binance_ok else '❌'}　→ {verdict}"
    )
    return 0 if both else 1


if __name__ == "__main__":
    raise SystemExit(main())
