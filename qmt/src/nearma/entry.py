# -*- coding: utf-8 -*-
"""QMT 内置策略入口：init / after_init / handlebar / stop / main_loop 与实时回调。
"""

import time

from .config import COOLDOWN_DAYS, FIXED_DATES, INDUSTRY_SECTOR, MARKET_OPEN_TIME, REFRESH_TIME, TRADE_END_TIME, UNIVERSE_SECTOR, VERBOSE_TICK
from .state import G, _attr, _clock, _f, _i, _norm_code, _reset_daily, _today, log
from .screen import _maybe_screen
from .sentiment import _maybe_sample_sentiment
from .market import _poll_pool
from .signals import _is_loss_exit, _record_cooldown
from .trade import FINAL_STATUS, _available_cash, _check_sell_timeout, _deployed_cash, _pos_map, _refresh_pool, _sync_orders, _trade_buys, _trade_positions



# =============================================================================
# 十五、QMT 入口
# =============================================================================


def init(C):
    """策略初始化（只执行一次）。"""
    G.day = _today()
    G.seq = int(time.time()) % 100000
    if not ACCOUNT:
        log('警告：未取到 account（策略交易界面需选定账号），下单将失败')

    try:
        C.set_account(ACCOUNT)     # 启用 order/deal 等实时回调（仅实盘模式）
    except Exception as exc:
        log('set_account 失败：%s' % exc)

    # 定时器与主图周期无关，最短可到毫秒；设过去的时间即立刻生效
    C.run_time('main_loop', '3nSecond', '2019-01-01 09:00:00')
    log('策略初始化完成 build=%s acct=%s type=%s 票池板块=%s 单行业=%s'
        % (BUILD_ID, ACCOUNT, ACCOUNT_TYPE, UNIVERSE_SECTOR, INDUSTRY_SECTOR or '（全市场）'))



def after_init(C):
    """init 之后可用函数放这里（get_trading_dates 在 init 里不可用）。"""
    try:
        end = _today()
        start = FIXED_DATES[0]
        G.trade_dates = [str(item) for item in
                         (C.get_trading_dates('000001.SH', start, end, -1, '1d') or [])]
    except Exception as exc:
        log('取交易日历失败：%s' % exc)
        G.trade_dates = []
    G.is_trade_day = G.day in G.trade_dates
    log('交易日历 %d 个，今天 %s %s'
        % (len(G.trade_dates), G.day, '是交易日' if G.is_trade_day else '非交易日'))
    if not G.trade_dates:
        # 日历不可用时不影响交易，只影响选股的均线窗口起点判断，降级为「不判非交易日」
        G.is_trade_day = True



def handlebar(C):
    """定时器型策略不用 K 线驱动：必须留空，否则盘中每个 tick 都会进来。"""
    return



def stop(C):
    """策略停止回调；此时交易连接已断，不能报撤单，只做收尾。"""
    alive = [uid for uid, item in G.pending.items() if item.get('status') == 'alive']
    if alive:
        log('策略停止：仍有 %d 笔在途委托（%s），请人工核对' % (len(alive), ','.join(alive[:5])))
    log('策略停止')



def main_loop(C):
    """主循环（默认 3 秒一轮）：日切 → 选股推进 → 情绪采样 → 对账 → 交易判定。"""
    # 1. 日切
    today = _today()
    if G.day != today:
        G.day = today
        _reset_daily()
        G.is_trade_day = (today in G.trade_dates) if G.trade_dates else True
        log('日切：%s %s' % (today, '交易日' if G.is_trade_day else '非交易日'))
    if not G.is_trade_day:
        return

    # 2. 盘前选股（分片推进）
    _maybe_screen(C)

    # 3. 情绪采样（每 SENTIMENT_INTERVAL 秒；日切时顺带刷新代码表与板块成员）
    _maybe_sample_sentiment(C)

    clock = _clock()

    # 4. 盘前刷新（09:15 起、开盘前完成）：票池确认 + 持仓兜底 + 订阅 + 布林上轨。
    #    必须早于开盘：订阅与上轨都要在连续竞价开始前就绪
    if clock >= REFRESH_TIME and G.pool_date != G.day:
        _refresh_pool(C)

    if clock < MARKET_OPEN_TIME or clock > TRADE_END_TIME:
        return

    # 5. 一轮只查一次持仓与资金，供行情、对账与交易共用
    positions = _pos_map()
    G.deployed_cash = _deployed_cash(positions)
    G.available_cash = _available_cash()

    # 6. 委托对账 + 卖出超时监控
    _sync_orders()
    _check_sell_timeout(C)

    # 7. 行情：每轮批量取最新价（票池 + 持仓）。订阅回调若可用，只是额外补充窗口样本
    _poll_pool(C, positions)

    # 8. 交易判定
    _trade_positions(C, positions)
    _trade_buys(C, positions)

    if VERBOSE_TICK:
        log('轮询 %s 池内 %d 持仓 %d 在途 %d 大盘 %s %.1f'
            % (clock, len(G.target_symbols), len(positions), len(G.pending),
               G.sent_level, G.sent_score))



# =============================================================================
# 十六、实时回调（仅实盘模式生效；与策略同线程，保持轻量）
# =============================================================================


def order_callback(C, orderInfo):
    """委托推送：维护 sysid 与终态。"""
    try:
        uid = str(_attr(orderInfo, 'm_strRemark', '') or '')
        record = G.pending.get(uid)
        if record is None:
            return
        sysid = str(_attr(orderInfo, 'm_strOrderSysID', '') or '')
        if sysid:
            record['sysid'] = sysid
        record['traded'] = _i(_attr(orderInfo, 'm_nVolumeTraded', 0))
        status = _i(_attr(orderInfo, 'm_nOrderStatus', 255), 255)
        if status in FINAL_STATUS:
            record['status'] = 'done'
    except Exception:
        pass



def deal_callback(C, dealInfo):
    """成交推送：维护开仓价、离场冷却、平仓清理。"""
    try:
        _handle_deal(dealInfo)
    except Exception as exc:
        log('成交处理异常：%s' % exc)



def _handle_deal(dealInfo):
    """成交处理（拆出来便于异常隔离）。"""
    code = ''
    symbol = str(_attr(dealInfo, 'm_strInstrumentID', '') or '')
    market = str(_attr(dealInfo, 'm_strExchangeID', '') or '')
    if symbol and market:
        code = _norm_code(symbol + '.' + market)
    if not code:
        return
    price = _f(_attr(dealInfo, 'm_dPrice', 0.0))
    volume = _i(_attr(dealInfo, 'm_nVolume', 0))
    offset = _i(_attr(dealInfo, 'm_nOffsetFlag', 0))
    op_type = _i(_attr(dealInfo, 'm_nOpType', 0))
    # 方向：m_nOffsetFlag 48 买 / 49 卖；缺失时按 opType 推断
    is_buy = (offset == 48) or (offset != 49 and op_type in (23, 27, 33))
    side_text = '买入' if is_buy else '卖出'
    log('成交通知 %s(%s) 方向 %s 价格 %.3f 数量 %d'
        % (code, G.target_symbols.get(code, ''), side_text, price, volume))

    positions = _pos_map()
    info = positions.get(code, {})

    if is_buy:
        # 建仓成交（成交后仓位 == 本次成交量）→ 用实际成交价确定开仓价
        pos_after = info.get('volume', 0)
        if pos_after <= volume:
            G.entry_prices[code] = price
            G.max_profit_pct[code] = 0.0
        G.entered.add(code)
        return

    # 卖出成交：任何离场都至少锁当天；亏损离场额外锁 COOLDOWN_DAYS 天
    days = COOLDOWN_DAYS if _is_loss_exit(code, price) else 0
    end = _record_cooldown(code, days)
    if info.get('volume', 0) <= 0:
        # 已清仓：清理追踪（冷却保留），下一波拉升要等禁入期过去
        G.entry_prices.pop(code, None)
        G.max_profit_pct.pop(code, None)
        G.entered.discard(code)
        log('%s 已清仓，买入冷却至 %s（含）' % (code, end))



def orderError_callback(C, orderArgs, errMsg):
    """下单异常回调。"""
    try:
        code = str(_attr(orderArgs, 'orderCode', '') or '')
        uid = str(_attr(orderArgs, 'userOrderId', '') or '')
        log('下单异常 %s uid=%s：%s' % (code, uid, errMsg))
        record = G.pending.get(uid)
        if record is not None:
            record['status'] = 'error'
    except Exception:
        pass



def position_callback(C, positionInfo):
    """持仓推送：不做本地簿记（每次现查），只记录一个薄日志。"""
    return



def account_callback(C, accountInfo):
    """资金推送：不做本地缓存（每次现查）。"""
    return
