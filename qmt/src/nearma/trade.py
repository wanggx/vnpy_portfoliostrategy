# -*- coding: utf-8 -*-
"""持仓与资金现查、下单与委托对账、卖单超时撤单、布林上轨、票池刷新、每轮交易流程。
"""

import math
import time

from .config import BOLL_DEV, BOLL_DIVIDEND_TYPE, BOLL_LOOKBACK_DAYS, BOLL_WINDOW, COOLDOWN_DAYS, FIXED_SIZE, PENDING_LOST_SECONDS, SELL_ORDER_TIMEOUT, STRATEGY_NAME, T1
from .state import G, _alert_once, _attr, _f, _float_or_none, _i, _norm_code, _now_ts, log
from .sentiment import _market_values, _sector_values
from .market import _subscribe_pool
from .signals import _buy_signal, _fmt_context, _profit_pct, _record_cooldown, _sell_signal, _update_max_profit



# =============================================================================
# 九、持仓 / 资金（现查，不做本地簿记）
# =============================================================================


# 柜台查询的 datatype：官方手册写小写（'position'），但本机 QMT 实盘部署的适配器
# 用的是大写（'POSITION'/'ORDER'/'DEAL'）。两种都试，取到非空结果为止，避开版本差异。
QUERY_KINDS = {
    'position': ('position', 'POSITION'),
    'order': ('order', 'ORDER'),
    'deal': ('deal', 'DEAL'),
    'account': ('account', 'ACCOUNT'),
}


def _query(kind, strategy_name=''):
    """查柜台数据（持仓/委托/成交/资金），返回行列表；查不到返回 []。"""
    for candidate in QUERY_KINDS.get(kind, (kind, kind.upper())):
        try:
            if strategy_name:
                rows = get_trade_detail_data(ACCOUNT, ACCOUNT_TYPE, candidate, strategy_name)
            else:
                rows = get_trade_detail_data(ACCOUNT, ACCOUNT_TYPE, candidate)
        except Exception as exc:
            # 同一 kind 当日只记一次，避免每轮刷屏
            if _alert_once('query_fail', kind):
                log('查询 %s（datatype=%s）失败：%s' % (kind, candidate, exc))
            continue
        if rows:
            return rows
    return []


def _positions():
    """取持仓列表。QMT 持仓查询只返回有仓的行（完全平掉的不会出现）。"""
    return _query('position')



def _pos_map():
    """code -> dict(volume, can_use, open_price)。QMT 持仓查询只返回有仓的行。"""
    out = {}
    for pos in _positions():
        code = _position_code(pos)
        if not code:
            continue
        out[code] = {
            'volume': _i(_attr(pos, 'm_nVolume', 0)),
            'can_use': _i(_attr(pos, 'm_nCanUseVolume', 0)),
            'open_price': _f(_attr(pos, 'm_dOpenPrice', 0.0)),
            'on_road': _i(_attr(pos, 'm_nOnRoadVolume', 0)),
        }
    return out



def _position_code(pos):
    """持仓对象的代码：m_strInstrumentID + '.' + m_strExchangeID。"""
    symbol = str(_attr(pos, 'm_strInstrumentID', '') or '')
    market = str(_attr(pos, 'm_strExchangeID', '') or '')
    if not symbol or not market:
        return ''
    return _norm_code(symbol + '.' + market)



def _available_cash():
    """账户可用资金。"""
    rows = _query('account')
    if not rows:
        return 0.0
    return _f(_attr(rows[0], 'm_dAvailable', 0.0))



def _pending_buy_amount():
    """在途买单占用金额。"""
    total = 0.0
    for order in G.pending.values():
        if order.get('status') != 'alive':
            continue
        if order.get('side') != 'BUY':
            continue
        left = order.get('vol', 0) - order.get('traded', 0)
        if left > 0:
            total += left * order.get('price', 0.0)
    return total



def _deployed_cash(positions=None):
    """本策略总占用资金 = 持仓成本 + 在途买单。

    ``positions`` 由主循环传入（一轮查一次），不传则自查。
    """
    if positions is None:
        positions = _pos_map()
    total = 0.0
    for info in positions.values():
        if info['open_price'] > 0 and info['volume'] > 0:
            total += info['open_price'] * info['volume']
    total += _pending_buy_amount()
    return total



def _has_active_order(code):
    """该标的是否有在途委托。"""
    for order in G.pending.values():
        if order.get('code') == code and order.get('status') == 'alive':
            return True
    return False



def _sync_entry_from_broker(code, info):
    """本地没有开仓价时，用券商成本价兜底（券商给的多为摊薄成本价，不可反向覆盖）。"""
    if code in G.entry_prices:
        return
    if info.get('open_price', 0.0) > 0:
        G.entry_prices[code] = info['open_price']
        G.max_profit_pct.setdefault(code, 0.0)
        log('%s 用券商成本价 %.3f 兜底开仓价' % (code, info['open_price']))



# =============================================================================
# 十、下单 / 对账
# =============================================================================


def _op_type(side):
    """买卖 opType：普通账户 23/24，两融担保品 33/34。"""
    if ACCOUNT_TYPE == 'CREDIT':
        return 33 if side == 'BUY' else 34
    return 23 if side == 'BUY' else 24



def _place_order(C, code, side, volume, price, tag):
    """下单；同标的有在途委托时拒绝（防超单）。返回 userOrderId 或 None。"""
    if volume <= 0:
        return None
    if _has_active_order(code):
        log('跳过下单：%s 已有在途委托' % code)
        return None
    G.seq += 1
    uid = '%s_%s_%d' % (tag or STRATEGY_NAME, G.day, G.seq)
    try:
        # prType=11 指定价；quickTrade 在定时器/回调中必须为 2
        passorder(_op_type(side), 1101, ACCOUNT, code, 11, float(price),
                  int(volume), STRATEGY_NAME, 2, uid, C)
    except Exception as exc:
        log('下单异常 %s %s %d @%.3f：%s' % (side, code, volume, price, exc))
        return None
    G.pending[uid] = {
        'code': code, 'side': side, 'vol': int(volume), 'status': 'alive',
        'sysid': '', 'traded': 0, 'price': float(price), 'ts': _now_ts(),
    }
    log('下单 %s %s %d股 @%.3f uid=%s' % (side, code, volume, price, uid))
    return uid



def _cancel_order(uid, C):
    """按柜台委托号撤单。QMT 的 cancel 需要 (委托号, 账号, 账号类型, ContextInfo)。"""
    order = G.pending.get(uid)
    if not order:
        return False
    sysid = order.get('sysid', '')
    if not sysid:
        log('撤单跳过：%s 还没有柜台委托号' % uid)
        return False
    try:
        ok = cancel(sysid, ACCOUNT, ACCOUNT_TYPE, C)
    except Exception as exc:
        log('撤单异常 %s sysid=%s：%s' % (uid, sysid, exc))
        return False
    log('撤单 uid=%s sysid=%s 信号=%s' % (uid, sysid, ok))
    return bool(ok)



# 委托状态码：在途 / 终态
ALIVE_STATUS = (48, 49, 50, 51, 52, 55, 86, 255)

FINAL_STATUS = (53, 54, 56, 57)



def _sync_orders():
    """轮询对账：把柜台委托按 m_strRemark 关联回 pending（回调之外的兜底）。"""
    orders = _query('order', STRATEGY_NAME)
    if not orders:
        # 部分版本不支持第 4 个 strategyName 参数，回退查全量再自行过滤
        orders = _query('order')
    for order in orders:
        uid = str(_attr(order, 'm_strRemark', '') or '')
        record = G.pending.get(uid)
        if record is None:
            continue
        sysid = str(_attr(order, 'm_strOrderSysID', '') or '')
        if sysid:
            record['sysid'] = sysid
        record['traded'] = _i(_attr(order, 'm_nVolumeTraded', 0))
        status = _i(_attr(order, 'm_nOrderStatus', 255), 255)
        record['status'] = 'alive' if status in ALIVE_STATUS else 'done'

    now = _now_ts()
    for uid, record in G.pending.items():
        if record.get('status') != 'alive':
            continue
        if not record.get('sysid') and now - record.get('ts', now) > PENDING_LOST_SECONDS:
            record['status'] = 'lost'
            log('警告：委托 %s（%s）超过 %.0f 秒未见柜台委托号，标记异常，请人工核对'
                % (uid, record.get('code', ''), PENDING_LOST_SECONDS))



def _check_sell_timeout(C):
    """卖出委托超时：挂满 SELL_ORDER_TIMEOUT 未全成交则告警并撤单（不重发）。"""
    now = _now_ts()
    for uid, record in list(G.pending.items()):
        if record.get('status') != 'alive':
            continue
        if record.get('side') != 'SELL':
            continue
        if now - record.get('ts', now) < SELL_ORDER_TIMEOUT:
            continue
        left = record.get('vol', 0) - record.get('traded', 0)
        if left <= 0:
            record['status'] = 'done'
            continue
        log('卖出委托超时告警 %s(%s) 已成交 %d/%d 股，挂满 %.0f 秒，撤单（不重发）'
            % (record.get('code', ''), G.target_symbols.get(record.get('code', ''), ''),
               record.get('traded', 0), record.get('vol', 0), SELL_ORDER_TIMEOUT))
        if _cancel_order(uid, C):
            record['status'] = 'done'



# =============================================================================
# 十二、布林上轨（盘前按可卖持仓算一次，前复权日线）
# =============================================================================


def _refresh_boll(C):
    """刷新当日布林上轨：只算「当天可卖出的持仓」；取不到写 0（不可用）。"""
    positions = _pos_map()
    targets = []
    for code, info in positions.items():
        if info['volume'] > 0 and (not T1 or info['can_use'] > 0):
            targets.append(code)
    if not targets:
        G.boll_up = {}
        return

    end = time.strftime('%Y%m%d', time.localtime(_now_ts() - 86400.0))
    start = time.strftime('%Y%m%d', time.localtime(_now_ts() - BOLL_LOOKBACK_DAYS * 86400.0))
    try:
        data = C.get_market_data_ex(
            ['close'], targets, '1d', start, end, -1, BOLL_DIVIDEND_TYPE, False) or {}
    except Exception as exc:
        G.boll_up = {}
        log('布林上轨取数失败，当日不启用该兜底止盈：%s' % exc)
        return

    bands = {}
    empty = []
    for code in targets:
        upper = _boll_upper(data.get(code))
        if upper > 0:
            bands[code] = upper
        else:
            empty.append(code)
    G.boll_up = bands
    msg = '布林上轨刷新 %d/%d 只' % (len(bands), len(targets))
    if empty:
        msg += '，无上轨 %d 只（%s）' % (len(empty), '、'.join(empty[:5]))
    log(msg)



def _boll_upper(df):
    """由前复权日线收盘价算布林上轨；样本不足或价格异常返回 0。

    口径与原版一致：均值 = 收盘价简单均值，标准差用总体标准差（ddof=0）。
    """
    if df is None or len(df) == 0:
        return 0.0
    columns = getattr(df, 'columns', None)
    if columns is None or 'close' not in columns:
        return 0.0
    values = list(df['close'].values)
    closes = []
    for item in values:
        val = _float_or_none(item)
        if val is not None:
            closes.append(val)
    closes = closes[-BOLL_WINDOW:]
    if len(closes) < BOLL_WINDOW:
        return 0.0
    for val in closes:
        if val <= 0:
            return 0.0
    mean = sum(closes) / len(closes)
    variance = 0.0
    for val in closes:
        variance += (val - mean) ** 2
    variance /= len(closes)
    return mean + BOLL_DEV * math.sqrt(variance)



# =============================================================================
# 十三、票池刷新
# =============================================================================


def _refresh_pool(C):
    """盘前 09:15 刷新：确认选股结果可用、同步券商持仓兜底、订阅 tick、算布林上轨。"""
    if not G.target_symbols:
        log('票池为空（选股可能未完成），本轮不交易')
    G.pool_date = G.day

    # 用券商持仓兜底开仓价（重启后本地 JSON 没了，只能靠券商成本价）
    positions = _pos_map()
    for code, info in positions.items():
        if info['volume'] > 0:
            _sync_entry_from_broker(code, info)
            G.entered.add(code)
            G.max_profit_pct.setdefault(code, 0.0)

    subscribed = _subscribe_pool(C, positions)
    _refresh_boll(C)
    log('票池刷新完成：池内 %d 只，持仓 %d 只，冷却中 %d 只，tick 订阅 %d 只'
        % (len(G.target_symbols), len(positions), len(G.cooldown_dates), subscribed))



# =============================================================================
# 十四、交易主流程（每个主循环轮次）
# =============================================================================


def _trade_positions(C, positions):
    """对每只有可卖量的持仓跑卖出链。"""
    for code, info in positions.items():
        if info['volume'] <= 0:
            continue
        sellable = info['can_use'] if T1 else info['volume']
        if sellable <= 0:
            continue
        quote = G.last.get(code)
        price = quote['price'] if quote else 0.0
        if price <= 0:
            continue

        _sync_entry_from_broker(code, info)
        _update_max_profit(code, price)

        # 是否已减半：可卖量不足以再减半一个标准仓，由可卖量与 FIXED_SIZE 比较推断
        half_done = sellable < FIXED_SIZE
        signal = _sell_signal(code, price, sellable, half_done)
        if not signal:
            continue
        uid = _place_order(C, code, 'SELL', signal['volume'], signal['price'], 'SELL')
        if uid is None:
            continue
        name = G.target_symbols.get(code, '')
        sector_name, sector_level, sector_score = _sector_values(code)
        market_level, market_score = _market_values()
        log('卖出信号 %s(%s) %s 卖出价格 %.2f 当前收益 %.2f%% 最大收益 %.2f%% %s'
            % (code, name, signal['reason'], signal['price'],
               _profit_pct(code, price) * 100.0,
               G.max_profit_pct.get(code, 0.0) * 100.0,
               _fmt_context(market_level, market_score, sector_name,
                            sector_level, sector_score)))
        # 亏损离场：即便卖单被拒/撤单也先登记冷却（额外锁 COOLDOWN_DAYS 天）
        if signal['type'] == 'CLEAR' and _profit_pct(code, price) < 0:
            end = _record_cooldown(code, COOLDOWN_DAYS)
            log('%s 亏损离场，买入冷却至 %s（含）' % (code, end))



def _trade_buys(C, positions):
    """对无持仓且在票池内的标的跑买入链。"""
    for code in list(G.target_symbols.keys()):
        if positions.get(code, {}).get('volume', 0) > 0:
            continue
        quote = G.last.get(code)
        if not quote:
            continue
        price = quote['price']
        if price <= 0:
            continue
        signal = _buy_signal(code, price, quote.get('pre_close', 0.0))
        if not signal:
            continue
        name = G.target_symbols.get(code, '')
        if _has_active_order(code):
            log('%s(%s) 已有在途委托，跳过重复买入' % (code, name))
            G.entered.add(code)
            continue
        uid = _place_order(C, code, 'BUY', signal['volume'], signal['price'], 'BUY')
        if uid is None:
            continue
        G.entered.add(code)
        G.entry_prices[code] = price
        G.max_profit_pct[code] = 0.0
        log('快速拉升买入 %s(%s) %s' % (code, name, signal['reason']))
