"""备份影子账户状态与监管数据；本机不装MySQL客户端也能运行。

数据是7天对照实验唯一的记录来源，一旦丢失无法重建，因此每次运行都：
1. 复制 .runtime 下的影子状态文件（权益、持仓、止损位只存在这些JSON里）
2. 把监管表导出为JSON（K线量大且可从交易所重拉，默认不导）
3. 轮转体积超限的日志
4. 清理超过保留期的旧备份
"""

import gzip
import json
import shutil
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gold_crypto_quant.config import get_settings  # noqa: E402

BACKUP_ROOT = Path("var/backups")
RUNTIME_DIR = Path(".runtime")
LOG_DIR = Path("logs")
KEEP_DAYS = 7
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_KEEP = 5
# K线可以随时从交易所重新拉取，导出它只会让备份膨胀到几十MB。
TABLES = (
    "app_users",
    "trading_accounts",
    "instruments",
    "shadow_equity_snapshots",
    "shadow_trade_events",
    "admin_audit_logs",
    "risk_events",
)


def _plain(value: object) -> object:
    """把datetime与Decimal转成JSON可写的形式，保留完整精度。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def dump_tables(target: Path) -> dict[str, int]:
    """导出监管表；失败的表不影响其他表继续导出。"""
    engine = create_engine(get_settings().database_url)
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in TABLES:
            try:
                rows = connection.execute(text(f"SELECT * FROM {table}")).mappings().all()
            except Exception as error:  # noqa: BLE001 - 单表失败不应中断整次备份
                print(f"  ! {table}: {type(error).__name__}")
                continue
            payload = [{k: _plain(v) for k, v in row.items()} for row in rows]
            path = target / f"{table}.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            counts[table] = len(payload)
    return counts


def copy_runtime_state(target: Path) -> int:
    """复制影子状态文件；这些文件不在数据库里，丢了等于丢掉当前持仓。"""
    state_dir = target / "runtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in RUNTIME_DIR.glob("*.json"):
        shutil.copy2(path, state_dir / path.name)
        copied += 1
    return copied


def rotate_logs() -> list[str]:
    """超过阈值的日志切片压缩；服务用 tee -a 写入，因此截断而不是改名，避免句柄失效。"""
    rotated: list[str] = []
    if not LOG_DIR.exists():
        return rotated
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    for log in LOG_DIR.glob("*.log"):
        if log.stat().st_size < LOG_MAX_BYTES:
            continue
        archive = log.with_name(f"{log.stem}-{stamp}.log.gz")
        with log.open("rb") as source, gzip.open(archive, "wb") as sink:
            shutil.copyfileobj(source, sink)
        # 就地截断：正在写这个文件的 tee 句柄仍然有效，不会因改名而写进孤儿文件。
        with log.open("r+") as handle:
            handle.truncate(0)
        rotated.append(archive.name)
        history = sorted(LOG_DIR.glob(f"{log.stem}-*.log.gz"))
        for old in history[:-LOG_KEEP]:
            old.unlink()
    return rotated


def prune_old_backups() -> int:
    """只保留最近若干天的备份目录。"""
    if not BACKUP_ROOT.exists():
        return 0
    directories = sorted(p for p in BACKUP_ROOT.iterdir() if p.is_dir())
    removed = 0
    for path in directories[:-KEEP_DAYS]:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def main() -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = BACKUP_ROOT / stamp
    target.mkdir(parents=True, exist_ok=True)
    print(f"备份到 {target}")

    states = copy_runtime_state(target)
    print(f"  影子状态文件 {states} 个")

    counts = dump_tables(target)
    for table, rows in counts.items():
        print(f"  {table}: {rows} 行")

    rotated = rotate_logs()
    if rotated:
        print(f"  日志已轮转: {', '.join(rotated)}")

    pruned = prune_old_backups()
    if pruned:
        print(f"  清理旧备份 {pruned} 份")

    size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    print(f"完成，本次 {size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
