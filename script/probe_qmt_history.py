"""探测大 QMT（经 bigqmt_signal_trader RPC 桥）能取到多久的历史行情。

用法：
    # 1) 读本地已有数据，看 1m 从 2020 到现在到底返回多少、最早到哪天
    python script/probe_qmt_history.py 000001.SZ --period 1m --start 20200101

    # 2) 先向大 QMT 提交服务端下载任务再读（慢，冷代码可能几分钟）
    python script/probe_qmt_history.py 000001.SZ --period tick --start 20250101 --download

    # 3) 看看服务端交易日历最早给到哪天（数据保留期的上界参考）
    python script/probe_qmt_history.py --calendar

判定口径（重要）：
    大 QMT 对**没下过数据**的窗口不报错，而是返回"垫行"——全 0，或把前收盘价填进
    open/high/low/close（不同券商终端构建不同，见 xtquant_big_convert CHANGELOG #339）。
    所以脚本以「行数 > 0 且 volume 不是全 0」作为"有真实数据"，并把垫行明确标出来。

依赖：本机装有 xtquant_big_convert（`bigqmt_signal_trader`）与 vnpy_xt 的 client config，
且大 QMT 端策略在跑（Redis/ZMQ 桥通）。路径可用环境变量 BIGQMT_SRC / VNPY_XT_ROOT 覆盖。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))

# 与 script/run.py 同风格：优先用旁边仓库根目录的包，不依赖 site-packages
for _root in (
    os.environ.get("BIGQMT_SRC", ""),
    os.path.abspath(os.path.join(_HERE, "..", "..", "xtquant_big_convert", "src")),
):
    if _root and os.path.isdir(_root) and _root not in sys.path:
        sys.path.insert(0, _root)

for _root in (
    os.environ.get("VNPY_XT_ROOT", ""),
    os.path.abspath(os.path.join(_HERE, "..", "..", "vnpy_xt")),
):
    if _root and os.path.isdir(_root) and _root not in sys.path:
        sys.path.insert(0, _root)

from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient, BigQmtXtData  # noqa: E402

try:
    import bigqmt_signal_trader_client_config as cfg  # noqa: E402
except ImportError:
    cfg = None

BASE_PERIODS = ("tick", "1m", "5m", "1d")

BAR_FIELDS: list[str] = ["open", "high", "low", "close", "volume", "amount"]
TICK_FIELDS: list[str] = ["time", "lastPrice", "lastClose", "volume", "amount"]


def make_data() -> BigQmtXtData:
    """按 deploy/qmt_cli.py 的方式构造行情客户端。"""
    if cfg is None:
        sys.exit(
            "找不到 bigqmt_signal_trader_client_config（应放在旁边的 vnpy_xt 仓库根下）"
        )
    client = BigQmtRpcClient(
        account_id=cfg.BIGQMT_ACCOUNT_ID,
        redis_config=dict(cfg.BIGQMT_REDIS_CONFIG),
        timeout_seconds=getattr(cfg, "BIGQMT_RPC_TIMEOUT_SECONDS", 30.0),
    )
    return BigQmtXtData(client)


def describe(df, period: str) -> str:
    """把一帧行情概括成一行：行数 / 首末时间 / 是否垫行。"""
    if df is None or getattr(df, "shape", (0,))[0] == 0:
        return "0 行（本地无数据，或未下载/未落盘）"

    rows: int = int(df.shape[0])
    first = df.index[0]
    last = df.index[-1]

    note: str = ""
    volumes = df["volume"] if "volume" in getattr(df, "columns", ()) else None
    if volumes is not None:
        try:
            total = float(volumes.fillna(0).astype(float).sum())
            if total == 0:
                note = "  ← 疑似垫行（volume 全 0，不是真实成交）"
        except Exception:  # noqa: BLE001 - 字段类型异常不影响探测结论
            pass

    return f"{rows} 行，{first} ~ {last}{note}"


def read_range(
    data: BigQmtXtData,
    code: str,
    period: str,
    start: str,
    end: str,
    count: int = -1,
):
    """读一段区间（纯本地，不订阅）：subscribe 语义在桥里由 fill_data/heal 控制。"""
    fields: list[str] = TICK_FIELDS if period == "tick" else BAR_FIELDS
    result = data.get_market_data_ex(
        fields,
        [code],
        period=period,
        start_time=start,
        end_time=end,
        count=count,
        dividend_type="none",
        fill_data=False,  # fill 会用占位行冒充数据，探测必须关掉
    )
    return (result or {}).get(code)


def download(data: BigQmtXtData, code: str, period: str, start: str, end: str) -> None:
    """先触发服务端下载（异步落盘，桥内部会轮询等待）。"""
    print(f"  提交服务端下载：{code} {period} {start}~{end} …", flush=True)
    result = data.download_history_data2([code], period, start_time=start, end_time=end)
    print(f"  下载返回：{result}")


def probe(
    data: BigQmtXtData,
    code: str,
    period: str,
    start: str,
    end: str,
    do_download: bool,
    step_days: int,
    max_windows: int,
) -> None:
    """读 [start, end]，空则把窗口起点向后挪，直到拿到数据，报出最早窗口。"""
    print(f"\n=== {code}  period={period}  区间 {start} ~ {end} ===")

    if do_download:
        download(data, code, period, start, end)

    df = read_range(data, code, period, start, end)
    print(f"  直接读：{describe(df, period)}")
    if df is not None and getattr(df, "shape", (0,))[0] > 0:
        return

    # 本地没有 → 按 step 把起点往后挪，找"最早有数据的窗口"
    cur_start: datetime = datetime.strptime(start, "%Y%m%d")
    end_dt: datetime = datetime.strptime(end, "%Y%m%d")
    for index in range(1, max_windows + 1):
        cur_start += timedelta(days=step_days)
        if cur_start >= end_dt:
            break
        win_start: str = cur_start.strftime("%Y%m%d")
        if do_download:
            download(data, code, period, win_start, end)
        df = read_range(data, code, period, win_start, end)
        print(f"  窗口 {index}：起点 {win_start} → {describe(df, period)}")
        if df is not None and getattr(df, "shape", (0,))[0] > 0:
            print(f"  ⇒ 该周期最早能拿到数据的起点约在 {win_start} 附近/之后")
            return

    print("  ⇒ 一直没拿到真实数据：要么服务端本来就没这段历史，要么本终端不支持该周期下载")


def show_calendar(data: BigQmtXtData) -> None:
    """打印服务端交易日历的首末日期，作为数据保留期的上界参考。"""
    print("=== 交易日历（SH）===")
    dates = data.get_trading_dates("SH", start_time="", end_time="", count=-1) or []
    if not dates:
        print("  取不到交易日历（RPC 不通？）")
        return
    print(f"  共 {len(dates)} 个交易日：{dates[0]} ~ {dates[-1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="探测大 QMT 历史行情可获取范围")
    parser.add_argument("code", nargs="?", default="000001.SZ", help="如 000001.SZ / 600519.SH")
    parser.add_argument("--period", default="1m", choices=BASE_PERIODS, help="基础周期")
    parser.add_argument("--start", default="20100101", help="探测起点 YYYYMMDD")
    parser.add_argument("--end", default="", help="探测终点 YYYYMMDD，默认今天")
    parser.add_argument("--download", action="store_true", help="先提交服务端下载再读（慢）")
    parser.add_argument("--step-days", type=int, default=180, help="空窗口时起点向后挪的天数")
    parser.add_argument("--max-windows", type=int, default=12, help="最多试探多少个窗口")
    parser.add_argument("--calendar", action="store_true", help="只打印交易日历")
    parser.add_argument("--no-baseline", action="store_true", help="跳过日线基线探测")
    args = parser.parse_args()

    data = make_data()
    end: str = args.end or date.today().strftime("%Y%m%d")

    if args.calendar:
        show_calendar(data)
        return

    # 日线基线：区分"服务端保留期就这么短"和"这个周期没下载/不支持"
    if not args.no_baseline and args.period != "1d":
        probe(data, args.code, "1d", "20100101", end, args.download, 0, 0)

    probe(
        data,
        args.code,
        args.period,
        args.start,
        end,
        args.download,
        args.step_days,
        args.max_windows,
    )
    show_calendar(data)


if __name__ == "__main__":
    main()
