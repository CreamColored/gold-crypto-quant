"""三个线上服务不得依赖回测那套重依赖。

vectorbt 会拖进 numba、llvmlite、scipy、plotly 共约 330 MB，而部署机器磁盘只有
8.4 G。更要紧的是这条约束很容易被无意打破——某天有人在 cli.py 顶层加一行
`from gold_crypto_quant.backtest import X`，服务照样能在本机跑，只有部署时才炸。
"""

import subprocess
import sys

_STUB = '''
import sys
BLOCKED = {"vectorbt", "numba", "llvmlite", "plotly", "scipy"}


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("BLOCKED:" + name)
        return None


sys.meta_path.insert(0, _Block())
sys.argv = ["main.py", CMD, "--help"]
exec(open("main.py").read())
'''


def _starts_without_backtest_deps(command: str) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-c", f"CMD={command!r}\n" + _STUB],
        capture_output=True, text=True, timeout=180,
    )
    blocked = [line for line in result.stderr.splitlines() if "BLOCKED:" in line]
    return result.returncode == 0, (blocked[-1] if blocked else result.stderr[-200:])


def test_quant_service_needs_no_backtest_deps() -> None:
    ok, detail = _starts_without_backtest_deps("public-market-comparison")
    assert ok, f"量化服务被回测依赖拖住：{detail}"


def test_collector_needs_no_backtest_deps() -> None:
    ok, detail = _starts_without_backtest_deps("quote-collector")
    assert ok, f"采集服务被回测依赖拖住：{detail}"


def test_web_needs_no_backtest_deps() -> None:
    ok, detail = _starts_without_backtest_deps("web-run")
    assert ok, f"Web 被回测依赖拖住：{detail}"
