"""near-MA surge strategy for the QMT built-in model runtime (Python 3.6, ASCII only).

THIS SINGLE FILE IS THE STRATEGY -- there is no build step and no other file.
Paste the whole thing into the QMT strategy editor and compile.

The file is organized in five parts:

    PART 1  QMT framework callbacks   the functions the client calls by name
    PART 2  buy signals               surge window, gates, order placement
    PART 3  sell signals              profit tiers, stops, bollinger, timeout
    PART 4  sentiment                 market / sector scores computed from ticks
    PART 5  everything else           parameters, state, screening, market data,
                                      trade infrastructure

Python resolves names at call time, so parts 1-4 may reference names defined in
part 5. Only constants and defs run at import time, so the order is safe.

The BUILD_ID constant identifies this revision; init logs it, so the QMT log
tells you which copy is actually running.

account / accountType are injected as globals by the client (the account you
select in the strategy trading dialog). Referenced directly; never redefined.

WHY ASCII ONLY: this client encrypts saved models and execs the source with a
`#coding:gbk` declaration. A GBK file whose high bytes shift inside the client's
paste/re-encode path fails with "'gbk' codec can't decode byte 0xfe". Pure ASCII
decodes identically under every encoding, so the paste path cannot break.

Non-ASCII (Chinese) log strings are kept as unicode escapes (U+XXXX) -- they still
print as Chinese at runtime. If you edit this file, keep it pure ASCII and keep
py3.6 syntax. Never leave a bare unicode escape in a comment or docstring: the
module is parsed before it runs and an incomplete escape raises a SyntaxError
("truncated escape"). Write U+XXXX instead.
"""

from datetime import datetime

import bisect

import math

import time


# =============================================================================
# PART 1/5  QMT FRAMEWORK CALLBACKS
# Only the functions the client calls by name. Nothing else lives in this part.
# =============================================================================

# -----------------------------------------------------------------------------
# 1.1 lifecycle
# -----------------------------------------------------------------------------

def init(C):
    "\u7b56\u7565\u521d\u59cb\u5316\uff08\u53ea\u6267\u884c\u4e00\u6b21\uff09\u3002"
    G.day = _today()
    G.seq = int(time.time()) % 100000
    if not account:
        log("\u8b66\u544a\uff1a\u672a\u53d6\u5230 account\uff08\u7b56\u7565\u4ea4\u6613\u754c\u9762\u9700\u9009\u5b9a\u8d26\u53f7\uff09\uff0c\u4e0b\u5355\u5c06\u5931\u8d25")

    try:
        C.set_account(account)
    except Exception as exc:
        log("set_account \u5931\u8d25\uff1a%s" % exc)


    C.run_time('main_loop', '3nSecond', '2019-01-01 09:00:00')
    log("\u7b56\u7565\u521d\u59cb\u5316\u5b8c\u6210 build=%s acct=%s type=%s \u7968\u6c60\u677f\u5757=%s \u5355\u884c\u4e1a=%s"
        % (BUILD_ID, account, accountType, UNIVERSE_SECTOR, INDUSTRY_SECTOR or "\uff08\u5168\u5e02\u573a\uff09"))


def after_init(C):
    "init \u4e4b\u540e\u53ef\u7528\u51fd\u6570\u653e\u8fd9\u91cc\uff08get_trading_dates \u5728 init \u91cc\u4e0d\u53ef\u7528\uff09\u3002"
    try:
        end = _today()
        start = FIXED_DATES[0]
        G.trade_dates = [str(item) for item in
                         (C.get_trading_dates('000001.SH', start, end, -1, '1d') or [])]
    except Exception as exc:
        log("\u53d6\u4ea4\u6613\u65e5\u5386\u5931\u8d25\uff1a%s" % exc)
        G.trade_dates = []
    G.is_trade_day = G.day in G.trade_dates
    log("\u4ea4\u6613\u65e5\u5386 %d \u4e2a\uff0c\u4eca\u5929 %s %s"
        % (len(G.trade_dates), G.day, "\u662f\u4ea4\u6613\u65e5" if G.is_trade_day else "\u975e\u4ea4\u6613\u65e5"))
    if not G.trade_dates:

        G.is_trade_day = True


def handlebar(C):
    "\u5b9a\u65f6\u5668\u578b\u7b56\u7565\u4e0d\u7528 K \u7ebf\u9a71\u52a8\uff1a\u5fc5\u987b\u7559\u7a7a\uff0c\u5426\u5219\u76d8\u4e2d\u6bcf\u4e2a tick \u90fd\u4f1a\u8fdb\u6765\u3002"
    return


def stop(C):
    "\u7b56\u7565\u505c\u6b62\u56de\u8c03\uff1b\u6b64\u65f6\u4ea4\u6613\u8fde\u63a5\u5df2\u65ad\uff0c\u4e0d\u80fd\u62a5\u64a4\u5355\uff0c\u53ea\u505a\u6536\u5c3e\u3002"
    alive = [uid for uid, item in G.pending.items() if item.get('status') == 'alive']
    if alive:
        log("\u7b56\u7565\u505c\u6b62\uff1a\u4ecd\u6709 %d \u7b14\u5728\u9014\u59d4\u6258\uff08%s\uff09\uff0c\u8bf7\u4eba\u5de5\u6838\u5bf9" % (len(alive), ','.join(alive[:5])))
    log("\u7b56\u7565\u505c\u6b62")


# -----------------------------------------------------------------------------
# 1.2 main loop (registered as a 3-second timer in init)
# -----------------------------------------------------------------------------

def main_loop(C):
    "\u4e3b\u5faa\u73af\uff08\u9ed8\u8ba4 3 \u79d2\u4e00\u8f6e\uff09\uff1a\u65e5\u5207 \u2192 \u9009\u80a1\u63a8\u8fdb \u2192 \u60c5\u7eea\u91c7\u6837 \u2192 \u5bf9\u8d26 \u2192 \u4ea4\u6613\u5224\u5b9a\u3002"

    today = _today()
    if G.day != today:
        G.day = today
        _reset_daily()
        G.is_trade_day = (today in G.trade_dates) if G.trade_dates else True
        log("\u65e5\u5207\uff1a%s %s" % (today, "\u4ea4\u6613\u65e5" if G.is_trade_day else "\u975e\u4ea4\u6613\u65e5"))
    if not G.is_trade_day:
        return


    _maybe_screen(C)


    _maybe_sample_sentiment(C)

    clock = _clock()


    if clock >= REFRESH_TIME and G.pool_date != G.day:
        _refresh_pool(C)

    if clock < MARKET_OPEN_TIME or clock > TRADE_END_TIME:
        return


    positions = _pos_map()
    G.deployed_cash = _deployed_cash(positions)
    G.available_cash = _available_cash()


    _sync_orders()
    _check_sell_timeout(C)


    _poll_pool(C, positions)


    _trade_positions(C, positions)
    _trade_buys(C, positions)

    if VERBOSE_TICK:
        log("\u8f6e\u8be2 %s \u6c60\u5185 %d \u6301\u4ed3 %d \u5728\u9014 %d \u5927\u76d8 %s %.1f"
            % (clock, len(G.target_symbols), len(positions), len(G.pending),
               G.sent_level, G.sent_score))


# -----------------------------------------------------------------------------
# 1.3 realtime callbacks (live trading mode only; same thread as the strategy)
# -----------------------------------------------------------------------------

def order_callback(C, orderInfo):
    "\u59d4\u6258\u63a8\u9001\uff1a\u7ef4\u62a4 sysid \u4e0e\u7ec8\u6001\u3002"
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
    "\u6210\u4ea4\u63a8\u9001\uff1a\u7ef4\u62a4\u5f00\u4ed3\u4ef7\u3001\u79bb\u573a\u51b7\u5374\u3001\u5e73\u4ed3\u6e05\u7406\u3002"
    try:
        _handle_deal(dealInfo)
    except Exception as exc:
        log("\u6210\u4ea4\u5904\u7406\u5f02\u5e38\uff1a%s" % exc)


def _handle_deal(dealInfo):
    "\u6210\u4ea4\u5904\u7406\uff08\u62c6\u51fa\u6765\u4fbf\u4e8e\u5f02\u5e38\u9694\u79bb\uff09\u3002"
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

    is_buy = (offset == 48) or (offset != 49 and op_type in (23, 27, 33))
    side_text = "\u4e70\u5165" if is_buy else "\u5356\u51fa"
    log("\u6210\u4ea4\u901a\u77e5 %s(%s) \u65b9\u5411 %s \u4ef7\u683c %.3f \u6570\u91cf %d"
        % (code, G.target_symbols.get(code, ''), side_text, price, volume))

    positions = _pos_map()
    info = positions.get(code, {})

    if is_buy:

        pos_after = info.get('volume', 0)
        if pos_after <= volume:
            G.entry_prices[code] = price
            G.max_profit_pct[code] = 0.0
        G.entered.add(code)
        return


    days = COOLDOWN_DAYS if _is_loss_exit(code, price) else 0
    end = _record_cooldown(code, days)
    if info.get('volume', 0) <= 0:

        G.entry_prices.pop(code, None)
        G.max_profit_pct.pop(code, None)
        G.entered.discard(code)
        log("%s \u5df2\u6e05\u4ed3\uff0c\u4e70\u5165\u51b7\u5374\u81f3 %s\uff08\u542b\uff09" % (code, end))


def orderError_callback(C, orderArgs, errMsg):
    "\u4e0b\u5355\u5f02\u5e38\u56de\u8c03\u3002"
    try:
        code = str(_attr(orderArgs, 'orderCode', '') or '')
        uid = str(_attr(orderArgs, 'userOrderId', '') or '')
        log("\u4e0b\u5355\u5f02\u5e38 %s uid=%s\uff1a%s" % (code, uid, errMsg))
        record = G.pending.get(uid)
        if record is not None:
            record['status'] = 'error'
    except Exception:
        pass


def position_callback(C, positionInfo):
    "\u6301\u4ed3\u63a8\u9001\uff1a\u4e0d\u505a\u672c\u5730\u7c3f\u8bb0\uff08\u6bcf\u6b21\u73b0\u67e5\uff09\uff0c\u53ea\u8bb0\u5f55\u4e00\u4e2a\u8584\u65e5\u5fd7\u3002"
    return


def account_callback(C, accountInfo):
    "\u8d44\u91d1\u63a8\u9001\uff1a\u4e0d\u505a\u672c\u5730\u7f13\u5b58\uff08\u6bcf\u6b21\u73b0\u67e5\uff09\u3002"
    return


# =============================================================================
# PART 2/5  BUY SIGNALS
# From "should we buy" to "order sent". Sentiment gates live in part 4.
# =============================================================================

# -----------------------------------------------------------------------------
# 2.1 buy gate inputs
# -----------------------------------------------------------------------------

def _is_buy_cooldown(code):
    "\u8be5\u6807\u7684\u4eca\u5929\u662f\u5426\u5904\u4e8e\u4e70\u5165\u7981\u5165\u671f\u3002"
    end = G.cooldown_dates.get(code, '')
    if not end:
        return False
    return G.day <= end


def _record_cooldown(code, days):
    "\u767b\u8bb0\u4e70\u5165\u7981\u5165\u622a\u6b62\u65e5\uff1b\u53ea\u5728\u65b0\u7684\u622a\u6b62\u65e5\u66f4\u665a\u65f6\u624d\u66f4\u65b0\uff08\u7edd\u4e0d\u6539\u77ed\uff09\u3002"
    end = _ymd_plus(days)
    old = G.cooldown_dates.get(code, '')
    if end <= old:
        return old
    G.cooldown_dates[code] = end
    return end


def _window_gain(code, price):
    "\u7a97\u53e3\u5185\u76f8\u5bf9\u6700\u4f4e\u4ef7\u7684\u6da8\u5e45\uff1b\u7a97\u53e3\u65f6\u957f\u4e0d\u8db3\u8fd4\u56de None\u3002"
    samples = G.window.get(code)
    if not samples:
        return None
    span = samples[-1][0] - samples[0][0]
    if span < MIN_SURGE_SPAN:
        return None
    low = None
    for _, item in samples:
        if item > 0 and (low is None or item < low):
            low = item
    if not low or low <= 0:
        return None
    return (price - low) / low


def _sector_buyable(code):
    level = G.sent_level
    score = G.sent_score
    if level in ("\u504f\u5f31", "\u6781\u5f31"):
        return False, True, '', '', 0.0, level, score
    name, sector_level, sector_score = _sector_values(code)
    if not name:
        return False, False, '', sector_level, sector_score, level, score
    if _sentiment_available() and level in ("\u504f\u5f3a", "\u6781\u5f3a"):
        ok = sector_level in ("\u4e2d\u6027", "\u504f\u5f3a", "\u6781\u5f3a")
    else:
        ok = sector_level in ("\u504f\u5f3a", "\u6781\u5f3a")
    return ok, False, name, sector_level, sector_score, level, score


# -----------------------------------------------------------------------------
# 2.2 buy decision and execution
# -----------------------------------------------------------------------------

def _buy_signal(code, price, pre_close):
    if code not in G.target_symbols:
        return None
    if code in G.entered:
        return None
    if price <= 0:
        return None
    if _is_buy_cooldown(code):
        return None

    gain = _window_gain(code, price)
    source = "\u7a97\u53e3"
    if gain is None or gain < SURGE_PCT:
        gain = None
        if pre_close > 0:
            day_gain = (price - pre_close) / pre_close
            if day_gain >= SURGE_PCT:
                gain = day_gain
                source = "\u6628\u6536"
    if gain is None:
        return None

    buy_price = price + PRICE_ADD
    base_reason = "%s\u6da8\u5e45 %.2f%% \u4e70\u5165\u4ef7\u683c %.2f \u6570\u91cf %d" % (
        source, gain * 100.0, buy_price, FIXED_SIZE)


    ok, blocked, sector_name, sector_level, sector_score, market_level, market_score = \
        _sector_buyable(code)
    name = G.target_symbols.get(code, '')
    if blocked:
        if _alert_once('market_block', code):
            log("\u5927\u76d8\u60c5\u7eea\u62e6\u622a %s(%s) \u4e70\u5165\u4ef7\u683c %.2f %s\uff0c\u5927\u76d8\u4e2d\u6027\u4ee5\u4e0b\u4e0d\u4e70\u5165\u4efb\u4f55\u6807\u7684"
                % (code, name, buy_price,
                   "\u5927\u76d8 \u8bc4\u7ea7 %s \u5f97\u5206 %.1f" % (market_level, market_score)))
        return None
    if not ok:
        if _alert_once('sector_block', code):
            log("\u884c\u4e1a\u60c5\u7eea\u62e6\u622a %s(%s) \u4e70\u5165\u4ef7\u683c %.2f %s\uff0c\u672a\u8fbe\u51c6\u5165\uff0c\u4e0d\u4e70\u5165"
                % (code, name, buy_price,
                   _fmt_context(market_level, market_score, sector_name,
                                sector_level, sector_score)))
        G.entered.add(code)
        return None


    deployed = G.deployed_cash
    need = buy_price * FIXED_SIZE
    if deployed + need > MAX_CASH:
        if _alert_once('cash_short', code):
            G.cash_short += 1
            log("\u8d44\u91d1\u4e0d\u8db3\uff0c\u8df3\u8fc7\u4e70\u5165 %s(%s) \u4e0b\u5355\u4ef7 %.2f \u5df2\u5360\u7528\u8d44\u91d1 %.2f \u4e0a\u9650 %.0f\uff08\u5f53\u65e5\u5df2\u8df3\u8fc7 %d \u7b14\uff09"
                % (code, name, buy_price, deployed, MAX_CASH, G.cash_short))
        return None

    if CHECK_AVAILABLE_CASH and need > G.available_cash:
        if _alert_once('cash_short_broker', code):
            log("\u8d26\u6237\u53ef\u7528\u8d44\u91d1\u4e0d\u8db3\uff0c\u8df3\u8fc7\u4e70\u5165 %s(%s) \u9700\u8981 %.2f \u53ef\u7528 %.2f\uff08\u67dc\u53f0\u53ef\u7528\u8d44\u91d1\u53e3\u5f84\uff09"
                % (code, name, need, G.available_cash))
        return None

    context = _fmt_context(market_level, market_score, sector_name,
                           sector_level, sector_score)
    return {
        'type': 'BUY',
        'volume': FIXED_SIZE,
        'price': buy_price,
        'reason': base_reason + ' ' + context,
    }


def _trade_buys(C, positions):
    "\u5bf9\u65e0\u6301\u4ed3\u4e14\u5728\u7968\u6c60\u5185\u7684\u6807\u7684\u8dd1\u4e70\u5165\u94fe\u3002"
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
            log("%s(%s) \u5df2\u6709\u5728\u9014\u59d4\u6258\uff0c\u8df3\u8fc7\u91cd\u590d\u4e70\u5165" % (code, name))
            G.entered.add(code)
            continue
        uid = _place_order(C, code, 'BUY', signal['volume'], signal['price'], 'BUY')
        if uid is None:
            continue
        G.entered.add(code)
        G.entry_prices[code] = price
        G.max_profit_pct[code] = 0.0
        log("\u5feb\u901f\u62c9\u5347\u4e70\u5165 %s(%s) %s" % (code, name, signal['reason']))


# =============================================================================
# PART 3/5  SELL SIGNALS
# Seven-step priority chain plus the bollinger fallback and the sell timeout.
# =============================================================================

# -----------------------------------------------------------------------------
# 3.1 profit tracking helpers
# -----------------------------------------------------------------------------

def _profit_pct(code, price):
    "\u5f53\u524d\u6536\u76ca\u7387\uff08\u76f8\u5bf9\u5f00\u4ed3\u4ef7\uff09\u3002"
    entry = G.entry_prices.get(code, 0.0)
    if entry <= 0 or price <= 0:
        return 0.0
    return (price - entry) / entry


def _update_max_profit(code, price):
    "\u5237\u65b0\u5f00\u4ed3\u540e\u6700\u5927\u6536\u76ca\u7387\u3002"
    entry = G.entry_prices.get(code, 0.0)
    if entry <= 0 or price <= 0:
        return
    profit = (price - entry) / entry
    if profit > G.max_profit_pct.get(code, 0.0):
        G.max_profit_pct[code] = profit


def _is_loss_exit(code, price):
    "\u5356\u51fa\u6210\u4ea4\u662f\u5426\u4e8f\u635f\u79bb\u573a\uff08\u6210\u4ea4\u4ef7\u4f4e\u4e8e\u5f00\u4ed3\u4ef7\uff09\u3002"
    entry = G.entry_prices.get(code, 0.0)
    return entry > 0 and price < entry


# -----------------------------------------------------------------------------
# 3.2 sell gate: sector sentiment exit
# -----------------------------------------------------------------------------

def _sector_sellable(code):
    "\u884c\u4e1a\u60c5\u7eea\u79bb\u573a\u5224\u5b9a\uff1b\u8fd4\u56de (sellable, \u677f\u5757\u540d, \u8bc4\u7ea7, \u5206\u6570)\u3002"
    name, sector_level, sector_score = _sector_values(code)
    if not name:
        return False, '', sector_level, sector_score
    return sector_level in ("\u504f\u5f31", "\u6781\u5f31"), name, sector_level, sector_score


# -----------------------------------------------------------------------------
# 3.3 sell decision (priority chain)
# -----------------------------------------------------------------------------

def _sell_signal(code, price, sellable, half_done):
    profit = _profit_pct(code, price)
    max_profit = G.max_profit_pct.get(code, 0.0)


    if profit < TIER_BREAKEVEN_PCT:
        sellable_sector, sector_name, sector_level, sector_score = _sector_sellable(code)
        if sellable_sector:
            market_level, market_score = _market_values()
            context = _fmt_context(market_level, market_score, sector_name,
                                   sector_level, sector_score)
            return {
                'type': 'CLEAR',
                'volume': sellable,
                'price': price - PRICE_ADD,
                'reason': ("\u884c\u4e1a\u60c5\u7eea\u79bb\u573a \u6536\u76ca%.2f%% \u4f4e\u4e8e%.0f%% %s"
                           % (profit * 100.0, TIER_BREAKEVEN_PCT * 100.0, context)),
            }


    wide_ok = False
    if _clock() < WIDE_STOP_END_TIME:
        wide_ok = _sector_usable(code) and _sentiment_available()
    stop_pct = WIDE_STOP_LOSS_PCT if wide_ok else STOP_LOSS_PCT
    tier_name = "\u5f00\u76d8\u5bbd\u6863" if wide_ok else "\u5e38\u89c4\u6863"
    if profit <= -stop_pct:
        return {
            'type': 'CLEAR',
            'volume': sellable,
            'price': price - PRICE_ADD,
            'reason': ("\u6b62\u635f \u4e8f\u635f %.2f%% \u8d85\u8fc7%s %.0f%%"
                       % (abs(profit) * 100.0, tier_name, stop_pct * 100.0)),
        }


    if profit >= CLEAR_PROFIT_PCT:
        return {
            'type': 'CLEAR',
            'volume': sellable,
            'price': price - PRICE_ADD,
            'reason': ("\u6e05\u4ed3 \u6536\u76ca %.2f%% \u8fbe\u5230 %.0f%%"
                       % (profit * 100.0, CLEAR_PROFIT_PCT * 100.0)),
        }


    if profit >= HALF_PROFIT_PCT and not half_done:
        half = int(math.ceil(sellable / 2.0 / 100.0)) * 100
        if 0 < half < sellable:
            return {
                'type': 'SELL',
                'volume': half,
                'price': price - PRICE_ADD,
                'reason': ("\u51cf\u534a \u6536\u76ca %.2f%% \u8fbe\u5230 %.0f%% \u5356 %d"
                           % (profit * 100.0, HALF_PROFIT_PCT * 100.0, half)),
            }


    drawdown = max_profit - profit
    if drawdown >= MAX_DRAWDOWN_PCT:
        return {
            'type': 'CLEAR',
            'volume': sellable,
            'price': price - PRICE_ADD,
            'reason': ("\u56de\u64a4 %.2f%% \u8d85\u8fc7 %.0f%%"
                       % (drawdown * 100.0, MAX_DRAWDOWN_PCT * 100.0)),
        }


    floor = None
    if max_profit >= TIER_HIGH_PCT:
        floor = TIER_HIGH_FLOOR
    elif max_profit >= TIER_LOW_PCT:
        floor = TIER_LOW_FLOOR
    elif max_profit >= TIER_BREAKEVEN_PCT:
        floor = TIER_BREAKEVEN_FLOOR
    if floor is not None and profit < floor:
        return {
            'type': 'CLEAR',
            'volume': sellable,
            'price': price - PRICE_ADD,
            'reason': "\u8dcc\u7834\u4fdd\u5e95 %.0f%%" % (floor * 100.0),
        }


    upper = G.boll_up.get(code, 0.0)
    if upper > 0 and price >= upper and profit >= BOLL_MIN_PROFIT_PCT:
        return {
            'type': 'CLEAR',
            'volume': sellable,
            'price': price - PRICE_ADD,
            'reason': ("\u5e03\u6797\u4e0a\u8f68\u6b62\u76c8 \u73b0\u4ef7%.2f \u4e0a\u8f68%.2f \u6536\u76ca%.2f%%"
                       % (price, upper, profit * 100.0)),
        }
    return None


# -----------------------------------------------------------------------------
# 3.4 bollinger upper band (fallback take-profit)
# -----------------------------------------------------------------------------

def _boll_upper(df):
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


def _refresh_boll(C):
    "\u5237\u65b0\u5f53\u65e5\u5e03\u6797\u4e0a\u8f68\uff1a\u53ea\u7b97\u300c\u5f53\u5929\u53ef\u5356\u51fa\u7684\u6301\u4ed3\u300d\uff1b\u53d6\u4e0d\u5230\u5199 0\uff08\u4e0d\u53ef\u7528\uff09\u3002"
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
        log("\u5e03\u6797\u4e0a\u8f68\u53d6\u6570\u5931\u8d25\uff0c\u5f53\u65e5\u4e0d\u542f\u7528\u8be5\u515c\u5e95\u6b62\u76c8\uff1a%s" % exc)
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
    msg = "\u5e03\u6797\u4e0a\u8f68\u5237\u65b0 %d/%d \u53ea" % (len(bands), len(targets))
    if empty:
        msg += "\uff0c\u65e0\u4e0a\u8f68 %d \u53ea\uff08%s\uff09" % (len(empty), "\u3001".join(empty[:5]))
    log(msg)


# -----------------------------------------------------------------------------
# 3.5 sell order timeout and cancel
# -----------------------------------------------------------------------------

def _cancel_order(uid, C):
    "\u6309\u67dc\u53f0\u59d4\u6258\u53f7\u64a4\u5355\u3002QMT \u7684 cancel \u9700\u8981 (\u59d4\u6258\u53f7, \u8d26\u53f7, \u8d26\u53f7\u7c7b\u578b, ContextInfo)\u3002"
    order = G.pending.get(uid)
    if not order:
        return False
    sysid = order.get('sysid', '')
    if not sysid:
        log("\u64a4\u5355\u8df3\u8fc7\uff1a%s \u8fd8\u6ca1\u6709\u67dc\u53f0\u59d4\u6258\u53f7" % uid)
        return False
    try:
        ok = cancel(sysid, account, accountType, C)
    except Exception as exc:
        log("\u64a4\u5355\u5f02\u5e38 %s sysid=%s\uff1a%s" % (uid, sysid, exc))
        return False
    log("\u64a4\u5355 uid=%s sysid=%s \u4fe1\u53f7=%s" % (uid, sysid, ok))
    return bool(ok)


def _check_sell_timeout(C):
    "\u5356\u51fa\u59d4\u6258\u8d85\u65f6\uff1a\u6302\u6ee1 SELL_ORDER_TIMEOUT \u672a\u5168\u6210\u4ea4\u5219\u544a\u8b66\u5e76\u64a4\u5355\uff08\u4e0d\u91cd\u53d1\uff09\u3002"
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
        log("\u5356\u51fa\u59d4\u6258\u8d85\u65f6\u544a\u8b66 %s(%s) \u5df2\u6210\u4ea4 %d/%d \u80a1\uff0c\u6302\u6ee1 %.0f \u79d2\uff0c\u64a4\u5355\uff08\u4e0d\u91cd\u53d1\uff09"
            % (record.get('code', ''), G.target_symbols.get(record.get('code', ''), ''),
               record.get('traded', 0), record.get('vol', 0), SELL_ORDER_TIMEOUT))
        if _cancel_order(uid, C):
            record['status'] = 'done'


# -----------------------------------------------------------------------------
# 3.6 sell execution chain
# -----------------------------------------------------------------------------

def _trade_positions(C, positions):
    "\u5bf9\u6bcf\u53ea\u6709\u53ef\u5356\u91cf\u7684\u6301\u4ed3\u8dd1\u5356\u51fa\u94fe\u3002"
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
        log("\u5356\u51fa\u4fe1\u53f7 %s(%s) %s \u5356\u51fa\u4ef7\u683c %.2f \u5f53\u524d\u6536\u76ca %.2f%% \u6700\u5927\u6536\u76ca %.2f%% %s"
            % (code, name, signal['reason'], signal['price'],
               _profit_pct(code, price) * 100.0,
               G.max_profit_pct.get(code, 0.0) * 100.0,
               _fmt_context(market_level, market_score, sector_name,
                            sector_level, sector_score)))

        if signal['type'] == 'CLEAR' and _profit_pct(code, price) < 0:
            end = _record_cooldown(code, COOLDOWN_DAYS)
            log("%s \u4e8f\u635f\u79bb\u573a\uff0c\u4e70\u5165\u51b7\u5374\u81f3 %s\uff08\u542b\uff09" % (code, end))


# =============================================================================
# PART 4/5  SENTIMENT
# Whole-market tick sample -> market score + per-sector scores, every 30s.
# =============================================================================

# -----------------------------------------------------------------------------
# 4.1 built-in sector names (131 SW level-2 sectors; this client has no get_sector_list)
# -----------------------------------------------------------------------------

SW2_SECTOR_NAMES = (
    "SW2IT\u670d\u52a1",
    "SW2\u4e00\u822c\u96f6\u552e",
    "SW2\u4e13\u4e1a\u5de5\u7a0b",
    "SW2\u4e13\u4e1a\u670d\u52a1",
    "SW2\u4e13\u4e1a\u8fde\u9501",
    "SW2\u4e13\u7528\u8bbe\u5907",
    "SW2\u4e2a\u62a4\u7528\u54c1",
    "SW2\u4e2d\u836f",
    "SW2\u4e58\u7528\u8f66",
    "SW2\u4e92\u8054\u7f51\u7535\u5546",
    "SW2\u4f11\u95f2\u98df\u54c1",
    "SW2\u4f53\u80b2",
    "SW2\u4fdd\u9669",
    "SW2\u5143\u4ef6",
    "SW2\u5149\u4f0f\u8bbe\u5907",
    "SW2\u5149\u5b66\u5149\u7535\u5b50",
    "SW2\u5176\u4ed6\u5bb6\u7535",
    "SW2\u5176\u4ed6\u7535\u5b50",
    "SW2\u5176\u4ed6\u7535\u6e90\u8bbe\u5907",
    "SW2\u517b\u6b96\u4e1a",
    "SW2\u519b\u5de5\u7535\u5b50",
    "SW2\u519c\u4e1a\u7efc\u5408",
    "SW2\u519c\u4ea7\u54c1\u52a0\u5de5",
    "SW2\u519c\u5316\u5236\u54c1",
    "SW2\u519c\u5546\u884c",
    "SW2\u51b6\u94a2\u539f\u6599",
    "SW2\u51fa\u7248",
    "SW2\u52a8\u7269\u4fdd\u5065",
    "SW2\u5305\u88c5\u5370\u5237",
    "SW2\u5316\u5986\u54c1",
    "SW2\u5316\u5b66\u5236\u54c1",
    "SW2\u5316\u5b66\u5236\u836f",
    "SW2\u5316\u5b66\u539f\u6599",
    "SW2\u5316\u5b66\u7ea4\u7ef4",
    "SW2\u533b\u7597\u5668\u68b0",
    "SW2\u533b\u7597\u670d\u52a1",
    "SW2\u533b\u7597\u7f8e\u5bb9",
    "SW2\u533b\u836f\u5546\u4e1a",
    "SW2\u534a\u5bfc\u4f53",
    "SW2\u53a8\u536b\u7535\u5668",
    "SW2\u5546\u7528\u8f66",
    "SW2\u56fd\u6709\u5927\u578b\u94f6\u884c",
    "SW2\u5730\u9762\u5175\u88c5",
    "SW2\u57ce\u5546\u884c",
    "SW2\u57fa\u7840\u5efa\u8bbe",
    "SW2\u5851\u6599",
    "SW2\u591a\u5143\u91d1\u878d",
    "SW2\u5bb6\u5c45\u7528\u54c1",
    "SW2\u5bb6\u7535\u96f6\u90e8\u4ef6",
    "SW2\u5c0f\u5bb6\u7535",
    "SW2\u5c0f\u91d1\u5c5e",
    "SW2\u5de5\u4e1a\u91d1\u5c5e",
    "SW2\u5de5\u7a0b\u54a8\u8be2\u670d\u52a1",
    "SW2\u5de5\u7a0b\u673a\u68b0",
    "SW2\u5e7f\u544a\u8425\u9500",
    "SW2\u5f71\u89c6\u9662\u7ebf",
    "SW2\u623f\u5730\u4ea7\u5f00\u53d1",
    "SW2\u623f\u5730\u4ea7\u670d\u52a1",
    "SW2\u623f\u5c4b\u5efa\u8bbe",
    "SW2\u6469\u6258\u8f66\u53ca\u5176\u4ed6",
    "SW2\u6559\u80b2",
    "SW2\u6570\u5b57\u5a92\u4f53",
    "SW2\u6587\u5a31\u7528\u54c1",
    "SW2\u65c5\u6e38\u53ca\u666f\u533a",
    "SW2\u65c5\u6e38\u96f6\u552e",
    "SW2\u666e\u94a2",
    "SW2\u670d\u88c5\u5bb6\u7eba",
    "SW2\u6797\u4e1a",
    "SW2\u6a61\u80f6",
    "SW2\u6c34\u6ce5",
    "SW2\u6c7d\u8f66\u670d\u52a1",
    "SW2\u6c7d\u8f66\u96f6\u90e8\u4ef6",
    "SW2\u6cb9\u670d\u5de5\u7a0b",
    "SW2\u6cb9\u6c14\u5f00\u91c7",
    "SW2\u6d88\u8d39\u7535\u5b50",
    "SW2\u6e14\u4e1a",
    "SW2\u6e38\u620f",
    "SW2\u70bc\u5316\u53ca\u8d38\u6613",
    "SW2\u7126\u70ad",
    "SW2\u7164\u70ad\u5f00\u91c7",
    "SW2\u7167\u660e\u8bbe\u5907",
    "SW2\u71c3\u6c14",
    "SW2\u7269\u6d41",
    "SW2\u7279\u94a2",
    "SW2\u73af\u4fdd\u8bbe\u5907",
    "SW2\u73af\u5883\u6cbb\u7406",
    "SW2\u73bb\u7483\u73bb\u7ea4",
    "SW2\u751f\u7269\u5236\u54c1",
    "SW2\u7535\u529b",
    "SW2\u7535\u5b50\u5316\u5b66\u54c1",
    "SW2\u7535\u673a",
    "SW2\u7535\u6c60",
    "SW2\u7535\u7f51\u8bbe\u5907",
    "SW2\u7535\u89c6\u5e7f\u64ad",
    "SW2\u767d\u8272\u5bb6\u7535",
    "SW2\u767d\u9152",
    "SW2\u79cd\u690d\u4e1a",
    "SW2\u7eba\u7ec7\u5236\u9020",
    "SW2\u7efc\u5408",
    "SW2\u80a1\u4efd\u5236\u94f6\u884c",
    "SW2\u80fd\u6e90\u91d1\u5c5e",
    "SW2\u81ea\u52a8\u5316\u8bbe\u5907",
    "SW2\u822a\u5929\u88c5\u5907",
    "SW2\u822a\u6d77\u88c5\u5907",
    "SW2\u822a\u7a7a\u673a\u573a",
    "SW2\u822a\u7a7a\u88c5\u5907",
    "SW2\u822a\u8fd0\u6e2f\u53e3",
    "SW2\u88c5\u4fee\u5efa\u6750",
    "SW2\u88c5\u4fee\u88c5\u9970",
    "SW2\u8ba1\u7b97\u673a\u8bbe\u5907",
    "SW2\u8bc1\u5238",
    "SW2\u8c03\u5473\u53d1\u9175\u54c1",
    "SW2\u8d35\u91d1\u5c5e",
    "SW2\u8d38\u6613",
    "SW2\u8f68\u4ea4\u8bbe\u5907",
    "SW2\u8f6f\u4ef6\u5f00\u53d1",
    "SW2\u901a\u4fe1\u670d\u52a1",
    "SW2\u901a\u4fe1\u8bbe\u5907",
    "SW2\u901a\u7528\u8bbe\u5907",
    "SW2\u9020\u7eb8",
    "SW2\u9152\u5e97\u9910\u996e",
    "SW2\u91d1\u5c5e\u65b0\u6750\u6599",
    "SW2\u94c1\u8def\u516c\u8def",
    "SW2\u975e\u767d\u9152",
    "SW2\u975e\u91d1\u5c5e\u6750\u6599",
    "SW2\u98ce\u7535\u8bbe\u5907",
    "SW2\u98df\u54c1\u52a0\u5de5",
    "SW2\u996e\u6599\u4e73\u54c1",
    "SW2\u9970\u54c1",
    "SW2\u9972\u6599",
    "SW2\u9ed1\u8272\u5bb6\u7535",
)


# -----------------------------------------------------------------------------
# 4.2 scoring primitives
# -----------------------------------------------------------------------------

#
def _clamp(value):
    "\u9650\u5236\u5230 [0, 100]\u3002"
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value


def _classify(score):
    "\u5206\u6570 \u2192 \u60c5\u7eea\u8bc4\u7ea7\uff08\u4e2d\u6587\u4e32\uff0c\u4e0e\u539f\u7248 SentimentLevel.value \u4e00\u81f4\uff09\u3002"
    if score < 20.0:
        return "\u6781\u5f31"
    if score < 45.0:
        return "\u504f\u5f31"
    if score < 60.0:
        return "\u4e2d\u6027"
    if score < 80.0:
        return "\u504f\u5f3a"
    return "\u6781\u5f3a"


# -----------------------------------------------------------------------------
# 4.3 daily base data: universe / ST set / sector members
# -----------------------------------------------------------------------------

def _refresh_daily_codes(C):
    "\u65e5\u5207\u65f6\u5237\u65b0\u5168\u5e02\u573a\u4ee3\u7801\u8868\u3001ST \u96c6\u5408\u3001\u677f\u5757\u6210\u5458\u7f13\u5b58\u3002"
    G.all_codes = _sector_codes(C, UNIVERSE_SECTOR)
    if not G.all_codes:
        log("\u8b66\u544a\uff1a\u672a\u53d6\u5230\u677f\u5757\u201c%s\u201d\u6210\u5206\uff0c\u60c5\u7eea\u4e0e\u9009\u80a1\u90fd\u4f1a\u964d\u7ea7" % UNIVERSE_SECTOR)
    G.st_codes = _load_st_codes(C)
    G.sector_members = {}
    G.stock_sector = {}
    _load_sector_members(C)


def _load_sector_members(C):
    names = []
    if INDUSTRY_SECTOR:
        names.append(INDUSTRY_SECTOR)
    for extra in EXTRA_SECTOR_NAMES:
        if extra and extra not in names:
            names.append(extra)
    if not names and LOAD_ALL_SECTORS:
        names = list(SW2_SECTOR_NAMES)
    if ENABLE_SECTOR_FACTOR:
        try:
            listed = C.get_sector_list('') or []
        except Exception:
            listed = []
        for name in listed:
            text = str(name)
            if not text.startswith(SECTOR_PREFIX):
                continue
            rest = text[len(SECTOR_PREFIX):]
            if not rest or rest[0].isdigit():
                continue
            if text not in names:
                names.append(text)

    started = _now_ts()
    for name in names:
        codes = _sector_codes(C, name)
        if not codes:
            continue
        G.sector_members[name] = codes
        for code in codes:
            G.stock_sector[code] = name
    log("\u677f\u5757\u6210\u5458\u7f13\u5b58\uff1a%d \u4e2a\u677f\u5757\uff0c\u8986\u76d6\u7387 %d \u53ea\u6807\u7684\uff08\u8017\u65f6 %.2fs\uff09"
        % (len(G.sector_members), len(G.stock_sector), _now_ts() - started))
    if not G.sector_members:
        log("\u8b66\u544a\uff1a\u4e00\u4e2a\u677f\u5757\u6210\u5458\u90fd\u6ca1\u53d6\u5230 \u2014\u2014 \u884c\u4e1a\u51c6\u5165\u4f1a\u62e6\u622a\u5168\u90e8\u4e70\u5165\uff08\u4e00\u7b14\u90fd\u4e70\u4e0d\u8fdb\uff09\u3002"
            "\u8bf7\u628a INDUSTRY_SECTOR / EXTRA_SECTOR_NAMES \u8bbe\u4e3a\u672c\u673a\u771f\u5b9e\u677f\u5757\u540d"
            "\uff08\u5982 SW2\u534a\u5bfc\u4f53\uff09\uff0c\u677f\u5757\u540d\u89c1\u5ba2\u6237\u7aef\u300c\u677f\u5757 \u2192 \u7533\u4e07\u4e8c\u7ea7\u884c\u4e1a\u677f\u5757\u300d")


# -----------------------------------------------------------------------------
# 4.4 breadth and index scoring
# -----------------------------------------------------------------------------

def _breadth(codes, ticks):
    "\u7b97\u4e00\u7ec4\u6807\u7684\u7684\u5bbd\u5ea6\u7edf\u8ba1\u3002\u8fd4\u56de dict \u6216 None\uff08\u65e0\u6709\u6548\u6807\u7684\uff09\u3002"
    changes = []
    advancing = 0
    declining = 0
    for code in codes:
        tick = ticks.get(code)
        if not tick:
            continue
        last = _f(tick.get('lastPrice'), 0.0)
        pre_close = _f(tick.get('lastClose'), 0.0)
        if last <= 0 or pre_close <= 0:
            continue
        change = (last / pre_close - 1.0) * 100.0
        changes.append(change)
        if change > 0.001:
            advancing += 1
        elif change < -0.001:
            declining += 1
    valid = len(changes)
    if valid <= 0:
        return None
    total = 0.0
    for item in changes:
        total += item
    average = total / valid
    net = float(advancing - declining) / float(valid)
    return {
        'valid': valid,
        'advancing': advancing,
        'declining': declining,
        'net': net,
        'average': average,
    }


def _breadth_score(breadth):
    "\u5bbd\u5ea6\u5206\uff1a\u51c0\u6da8\u8dcc\u5360\u6bd4 75% + \u5e73\u5747\u6da8\u8dcc\u5e45 25%\u3002"
    count_score = _clamp(50.0 + breadth['net'] * 50.0)
    return_score = _clamp(50.0 + breadth['average'] / CHANGE_SCALE * 50.0)
    return count_score * 0.75 + return_score * 0.25


def _index_score(ticks):
    "\u6307\u6570\u5206\uff1a\u6709\u6548\u6307\u6570\u5e73\u5747\u6da8\u8dcc\u5e45\u6620\u5c04\uff1b\u5168\u90e8\u65e0\u6548\u8fd4\u56de None\u3002"
    changes = []
    for code in INDEX_SYMBOLS:
        tick = ticks.get(code)
        if not tick:
            continue
        last = _f(tick.get('lastPrice'), 0.0)
        pre_close = _f(tick.get('lastClose'), 0.0)
        if last <= 0 or pre_close <= 0:
            continue
        changes.append((last / pre_close - 1.0) * 100.0)
    if not changes:
        return None
    total = 0.0
    for item in changes:
        total += item
    return _clamp(50.0 + (total / len(changes)) / CHANGE_SCALE * 50.0)


# -----------------------------------------------------------------------------
# 4.5 sampling and the periodic trigger
# -----------------------------------------------------------------------------

def _sentiment_sample(C):
    "\u4e00\u6b21\u5168\u5e02\u573a\u91c7\u6837\uff1a\u7b97\u5927\u76d8\u5206 + \u5404\u677f\u5757\u5206\u3002"
    codes = G.all_codes
    if not codes:
        return
    fetch = list(codes)
    for code in INDEX_SYMBOLS:
        if code not in fetch:
            fetch.append(code)
    try:
        ticks = C.get_full_tick(fetch) or {}
    except Exception as exc:
        log("\u60c5\u7eea\u91c7\u6837\u53d6\u6570\u5931\u8d25\uff1a%s" % exc)
        return

    breadth = _breadth(codes, ticks)
    if breadth is None:
        G.sent_ts = _now_ts()
        G.sent_level = "\u4e0d\u53ef\u7528"
        G.sent_valid = 0
        G.sent_score = 0.0
        return

    components = [(_breadth_score(breadth), BREADTH_WEIGHT)]
    index_score = _index_score(ticks)
    if index_score is not None:
        components.append((index_score, INDEX_WEIGHT))


    sector_states = {}
    for name in G.sector_members:
        stat = _breadth(G.sector_members[name], ticks)
        if stat is None or stat['valid'] < MIN_SECTOR_VALID:
            continue
        score = _clamp(50.0 + stat['net'] * 50.0) * 0.75 \
            + _clamp(50.0 + stat['average'] / CHANGE_SCALE * 50.0) * 0.25
        sector_states[name] = {
            'score': score,
            'level': _classify(score),
            'valid': stat['valid'],
        }
    G.sent_sectors = sector_states

    if ENABLE_SECTOR_FACTOR and sector_states:
        ranked = sorted(sector_states.items(), key=lambda kv: kv[1]['score'], reverse=True)[:3]
        sector_total = 0.0
        for _, state in ranked:
            sector_total += state['score']
        components.append((sector_total / len(ranked), SECTOR_WEIGHT))

    weight_sum = 0.0
    weighted = 0.0
    for score, weight in components:
        weighted += score * weight
        weight_sum += weight
    total_score = _clamp(weighted / weight_sum) if weight_sum > 0 else 0.0

    G.sent_score = total_score
    G.sent_level = _classify(total_score)
    G.sent_valid = breadth['valid']
    G.sent_ts = _now_ts()


def _maybe_sample_sentiment(C):
    "\u6309\u95f4\u9694\u89e6\u53d1\u4e00\u6b21\u60c5\u7eea\u91c7\u6837\uff08\u4ec5\u5728\u5f00\u76d8\u524d 5 \u5206\u949f\u5230\u6536\u76d8\u4e4b\u95f4\uff09\u3002"
    if not G.is_trade_day:
        return
    if G.sent_date != G.day:
        G.sent_date = G.day
        _refresh_daily_codes(C)
    clock = _clock()
    if clock < REFRESH_TIME:
        return
    if clock > '15:00:00':
        return
    if _now_ts() - G.sent_ts < SENTIMENT_INTERVAL:
        return
    _sentiment_sample(C)


# -----------------------------------------------------------------------------
# 4.6 query interfaces used by the signal parts
# -----------------------------------------------------------------------------

def _sentiment_available():
    "\u5927\u76d8\u5feb\u7167\u662f\u5426\u53ef\u4fe1\uff08\u672a\u8fc7\u671f + \u6709\u6548\u6807\u7684\u5145\u8db3\uff09\u3002"
    if G.sent_valid <= 0 or G.sent_level == "\u4e0d\u53ef\u7528":
        return False
    return (_now_ts() - G.sent_ts) <= SENTIMENT_STALE_AFTER


def _market_values():
    "(\u5927\u76d8\u8bc4\u7ea7, \u5927\u76d8\u5206)\uff0c\u4f9b\u6d88\u606f\u62fc\u88c5\u3002"
    return G.sent_level, G.sent_score


def _sector_values(code):
    name = INDUSTRY_SECTOR or G.stock_sector.get(code, '')
    if not name:
        return '', "\u672a\u5206\u884c\u4e1a", 0.0
    state = G.sent_sectors.get(name)
    if state is None:
        return '', "\u672a\u5206\u884c\u4e1a", 0.0
    return name, state['level'], state['score']


def _sector_usable(code):
    "\u8be5\u6807\u7684\u7684\u677f\u5757\u60c5\u7eea\u662f\u5426\u53ef\u7528\uff08\u677f\u5757\u5728\u5c97\uff09\u3002"
    name = INDUSTRY_SECTOR or G.stock_sector.get(code, '')
    if not name:
        return False
    return name in G.sent_sectors


# =============================================================================
# PART 5/5  EVERYTHING ELSE
# Parameters, global state, screening, market data, trade infrastructure.
# =============================================================================

# -----------------------------------------------------------------------------
# 5.1 parameters
# -----------------------------------------------------------------------------

# account / accountType are injected by the client (the account selected in the
# strategy trading dialog). They are referenced directly and never redefined.
STRATEGY_NAME = 'NEARMA'


BUILD_ID = '8b2f251b'


UNIVERSE_SECTOR = "\u6caa\u6df1A\u80a1"


INDUSTRY_SECTOR = ''


EXTRA_SECTOR_NAMES = ()


LOAD_ALL_SECTORS = True


EXCLUDE_CODE_PREFIXES = ('688', '689')


ST_SECTOR_NAMES = ("\u98ce\u9669\u8b66\u793a", "ST\u677f\u5757")


FIXED_DATES = ('20220427', '20221031', '20240205', '20240918', '20250407')


MIN_BARS = 100


NEAR_THRESHOLD = 0.01


BREAK_LOOKBACK = 20


BREAK_THRESHOLD = 0.03


TOP_N = 100


MIN_WINDOW_POINTS = 5


DIVIDEND_TYPE = 'front'


REQUIRE_HALVED = True


HALF_RATIO = 0.5


HIGH_LOOKBACK = 252


WEIGHT_MODE = 'days'


NAME_CHECK_BUFFER = 20


SCREEN_BATCH = 200


SCREEN_START = '08:40:00'


SCREEN_DEADLINE = '09:20:00'


SCREEN_RETRY = 30.0


ALLOW_LATE_SCREEN = True


FIXED_SIZE = 500


PRICE_ADD = 0.05


MAX_CASH = 100000.0


CHECK_AVAILABLE_CASH = True


T1 = True


REFRESH_TIME = '09:15:00'


MARKET_OPEN_TIME = '09:30:00'


TRADE_END_TIME = '14:57:00'


SURGE_PCT = 0.03


SURGE_WINDOW = 300


MIN_SURGE_SPAN = 180


STOP_LOSS_PCT = 0.02


WIDE_STOP_LOSS_PCT = 0.04


WIDE_STOP_END_TIME = '10:00:00'


MAX_DRAWDOWN_PCT = 0.10


TIER_HIGH_PCT = 0.10


TIER_HIGH_FLOOR = 0.05


TIER_LOW_PCT = 0.05


TIER_LOW_FLOOR = 0.02


TIER_BREAKEVEN_PCT = 0.03


TIER_BREAKEVEN_FLOOR = 0.0


COOLDOWN_DAYS = 2


HALF_PROFIT_PCT = 0.10


CLEAR_PROFIT_PCT = 0.20


BOLL_WINDOW = 20


BOLL_DEV = 2.0


BOLL_MIN_PROFIT_PCT = 0.05


BOLL_LOOKBACK_DAYS = 60


BOLL_DIVIDEND_TYPE = 'front'


SELL_ORDER_TIMEOUT = 300.0


PENDING_LOST_SECONDS = 60.0


SENTIMENT_INTERVAL = 30.0


SENTIMENT_STALE_AFTER = 90.0


SECTOR_PREFIX = 'SW2'


ENABLE_SECTOR_FACTOR = True


MIN_SECTOR_VALID = 5


INDEX_SYMBOLS = {
    '000001.SH': "\u4e0a\u8bc1\u7efc\u6307",
    '399001.SZ': "\u6df1\u8bc1\u6210\u6307",
    '399006.SZ': "\u521b\u4e1a\u677f\u6307",
}


BREADTH_WEIGHT = 0.55


INDEX_WEIGHT = 0.30


SECTOR_WEIGHT = 0.15


CHANGE_SCALE = 2.0


VERBOSE_TICK = False


# -----------------------------------------------------------------------------
# 5.2 global state container and helpers
# -----------------------------------------------------------------------------

#
class G:
    "\u5168\u5c40\u53ef\u53d8\u72b6\u6001\u5bb9\u5668\u3002"


G = G()


G.day = ''


G.trade_dates = []


G.is_trade_day = False


G.pool_date = ''


G.boll_date = ''


G.sent_date = ''


G.target_symbols = {}


G.universe_data = {}        # code -> dict(score/close/high52w/near_dates/...)


G.all_codes = []


G.st_codes = set()


G.screen = {}


G.screen_retry_at = 0.0


G.entered = set()


G.entry_prices = {}


G.max_profit_pct = {}


G.cooldown_dates = {}


G.window = {}


G.boll_up = {}


G.seq = 0


G.pending = {}              # userOrderId -> dict(code/side/vol/status/sysid/traded/ts/price)


G.cash_short = 0


G.alerted = {}


G.sent_ts = 0.0


G.sent_score = 0.0


G.sent_level = "\u4e0d\u53ef\u7528"


G.sent_valid = 0


G.sent_sectors = {}


G.stock_sector = {}


G.sector_members = {}


G.last = {}                 # code -> dict(price/pre_close/ts)


G.tick_subscribed = 0


def log(msg):
    "\u7edf\u4e00\u65e5\u5fd7\u524d\u7f00\uff0c\u6253\u5230 QMT \u65e5\u5fd7\u7a97\u53e3\u3002"
    print('[NEARMA] ' + str(msg))


def _clock():
    "\u5f53\u524d\u65f6\u949f HH:MM:SS\u3002"
    return time.strftime('%H:%M:%S')


def _today():
    "\u5f53\u524d\u81ea\u7136\u65e5 YYYYMMDD\u3002"
    return time.strftime('%Y%m%d')


def _now_ts():
    "\u5f53\u524d\u65f6\u95f4\u6233\uff08\u79d2\uff0cfloat\uff09\u3002"
    return time.time()


def _in_window(start, end):
    "\u5f53\u524d\u65f6\u949f\u662f\u5426\u843d\u5728 [start, end] \u95ed\u533a\u95f4\u5185\uff08HH:MM:SS \u5b57\u7b26\u4e32\u53ef\u76f4\u63a5\u6bd4\u8f83\uff09\u3002"
    now = _clock()
    return start <= now <= end


def _is_trading_session():
    "\u662f\u5426\u5904\u4e8e\u8fde\u7eed\u7ade\u4ef7\u65f6\u6bb5\uff0809:30 \u8d77\uff09\u3002"
    return _clock() >= MARKET_OPEN_TIME


def _is_stock_code(code):
    "\u662f\u5426\u6caa\u6df1 A \u80a1\u4ee3\u7801\u3002"
    return code.endswith('.SH') or code.endswith('.SZ')


def _norm_code(code):
    "\u7edf\u4e00\u4ee3\u7801\u5927\u5c0f\u5199\u4e0e\u540e\u7f00\u5199\u6cd5\uff08QMT \u7528 .SH/.SZ\uff09\u3002"
    text = str(code).strip().upper()
    return text


def _ymd_plus(days):
    "\u4eca\u5929 + days \u4e2a\u81ea\u7136\u65e5\u7684 YYYYMMDD\uff08\u51b7\u5374\u767b\u8bb0\u7528\uff0c\u4e0d\u4f9d\u8d56\u4ea4\u6613\u65e5\u5386\uff09\u3002"
    return time.strftime('%Y%m%d', time.localtime(_now_ts() + days * 86400.0))


def _f(value, default=0.0):
    "\u5b89\u5168\u8f6c float\u3002"
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _i(value, default=0):
    "\u5b89\u5168\u8f6c int\u3002"
    return int(_f(value, default))


def _attr(obj, name, default=None):
    "\u5e26\u9ed8\u8ba4\u503c\u7684\u5c5e\u6027\u8bfb\u53d6\uff08QMT \u56de\u62a5\u5bf9\u8c61\u5b57\u6bb5\u53ef\u80fd\u7f3a\u5931\uff09\u3002"
    value = getattr(obj, name, None)
    if value is None:
        return default
    return value


def _alert_once(kind, code=''):
    "\u6309\u65e5\u53bb\u91cd\u7684\u544a\u8b66\u95f8\u95e8\uff1a\u5f53\u65e5\u540c\u4e00 (kind, code) \u53ea\u653e\u884c\u4e00\u6b21\u3002"
    key = kind + '|' + code
    if G.alerted.get(key) == G.day:
        return False
    G.alerted[key] = G.day
    return True


def _reset_daily():
    "\u65e5\u5207\u91cd\u7f6e\uff1a\u6e05\u6389\u5f53\u65e5\u72b6\u6001\uff0c\u4fdd\u7559\u9700\u8981\u8de8\u65e5\u5ef6\u7eed\u7684\u5f00\u4ed3\u8ffd\u8e2a\u4e0e\u51b7\u5374\u3002"
    G.entered = set()
    G.window = {}
    G.boll_up = {}
    G.cash_short = 0
    G.alerted = {}
    G.pending = {}
    G.last = {}
    G.screen = {}
    G.sent_sectors = {}
    G.sent_ts = 0.0
    G.sent_score = 0.0
    G.sent_level = "\u4e0d\u53ef\u7528"
    G.sent_valid = 0

    today = G.day
    expired = [code for code, end in G.cooldown_dates.items() if end < today]
    for code in expired:
        G.cooldown_dates.pop(code, None)
    if expired:
        log("\u65e5\u5207\u6e05\u7406\u8fc7\u671f\u4e70\u5165\u51b7\u5374 %d \u6761" % len(expired))


def _float_or_none(value):
    "\u8f6c float\uff1bNaN / \u65e0\u6548\u503c\u8fd4\u56de None\uff08\u7528 None \u8868\u793a\u7f3a\u5931\uff0c\u907f\u514d math.isnan \u5224\u7a7a\u9057\u6f0f\uff09\u3002"
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


# -----------------------------------------------------------------------------
# 5.3 screening: build the tradable pool before the open
# -----------------------------------------------------------------------------

#
def _sector_codes(C, sector_name):
    "\u53d6\u677f\u5757\u6210\u5206\u80a1\uff1b\u5931\u8d25\u8fd4\u56de\u7a7a\u5217\u8868\u3002"
    if not sector_name:
        return []
    try:
        codes = C.get_stock_list_in_sector(sector_name)
    except Exception as exc:
        log("\u8bfb\u53d6\u677f\u5757 %s \u5931\u8d25\uff1a%s" % (sector_name, exc))
        return []
    if not codes:
        return []
    out = []
    seen = set()
    for code in codes:
        item = _norm_code(code)
        if item in seen or not _is_stock_code(item):
            continue
        seen.add(item)
        out.append(item)
    out.sort()
    return out


def _load_st_codes(C):
    names = ST_SECTOR_NAMES if isinstance(ST_SECTOR_NAMES, tuple) else (ST_SECTOR_NAMES,)
    for name in names:
        codes = _sector_codes(C, name)
        if codes:
            log("ST \u6392\u9664\u7528\u677f\u5757\u201c%s\u201d\uff1a%d \u4e2a" % (name, len(codes)))
            return set(codes)
    log("\u672a\u53d6\u5230 ST \u677f\u5757\uff0c\u6539\u7528\u540d\u79f0\u515c\u5e95\u6392\u9664")
    return set()


def _fetch_names(C, codes):
    "\u6279\u91cf\u53d6\u6807\u7684\u540d\u79f0\uff1b\u53d6\u4e0d\u5230\u7684\u8fd4\u56de\u7a7a\u4e32\u3002"
    out = {}
    for code in codes:
        name = ''
        try:
            detail = C.get_instrument_detail(code)
            if detail:
                name = str(detail.get('InstrumentName', '') or '')
        except Exception:
            name = ''
        out[code] = name
    return out


def _score_weight(days):
    "\u5355\u6761\u5747\u7ebf\u6743\u91cd\u3002"
    if WEIGHT_MODE == 'equal':
        return 1.0
    if WEIGHT_MODE == 'log':
        return math.log(1.0 + days)
    return float(days)


def _recent_high(close_values, high_values, index_map, trade_date, lookback):
    "\u8fd1 lookback \u4e2a\u4ea4\u6613\u65e5\u6700\u9ad8\u4ef7\uff1bhigh \u7f3a\u5931\u65f6\u7528 close \u8fd1\u4f3c\u3002\u65e0\u6709\u6548\u6570\u636e\u8fd4\u56de None\u3002"
    pos = index_map.get(trade_date)
    if pos is None:
        return None
    start = max(0, pos - lookback + 1)
    best = None
    for idx in range(start, pos + 1):
        val = high_values[idx]
        if val is not None:
            if best is None or val > best:
                best = val
    if best is not None:
        return best
    for idx in range(start, pos + 1):
        val = close_values[idx]
        if val is not None:
            if best is None or val > best:
                best = val
    return best


def _is_halved(close_values, high_values, index_map, trade_date):
    high_max = _recent_high(close_values, high_values, index_map, trade_date, HIGH_LOOKBACK)
    if high_max is None or high_max <= 0:
        return True
    pos = index_map.get(trade_date)
    if pos is None:
        return True
    close_t = close_values[pos]
    if close_t is None:
        return True
    return close_t <= high_max * HALF_RATIO


def _broke_below_ma(bars, start_idx, pos):
    valid_idx = bars['valid_idx']
    cum_sum = bars['cum_sum']
    cum_cnt = bars['cum_cnt']
    closes = bars['close']
    left = bisect.bisect_left(valid_idx, start_idx)
    right = bisect.bisect_left(valid_idx, pos)
    window = valid_idx[left:right]
    if not window:
        return False
    for idx in window[-BREAK_LOOKBACK:]:
        count = cum_cnt[idx + 1] - cum_cnt[start_idx]
        if count <= 0:
            continue
        ma = (cum_sum[idx + 1] - cum_sum[start_idx]) / count
        if closes[idx] < ma * (1.0 - BREAK_THRESHOLD):
            return True
    return False


def _score_symbol(bars, trade_date, window_start):
    "\u5355\u6807\u7684\u6253\u5206\uff1b\u8fd4\u56de dict \u6216 None\uff08\u8df3\u8fc7\uff09\u3002\u53e3\u5f84\u4e0e\u539f\u7248 _score_symbol \u4e00\u81f4\u3002"
    index_map = bars['index_map']
    closes = bars['close']
    pos = index_map.get(trade_date)
    if pos is None:
        return None
    close_t = closes[pos]
    if close_t is None or close_t <= 0:
        return None
    low_t = bars['low'][pos]

    if REQUIRE_HALVED and not _is_halved(closes, bars['high'], index_map, trade_date):
        return None

    valid_idx = bars['valid_idx']
    listed_idx = valid_idx[0] if valid_idx else None
    cum_sum = bars['cum_sum']
    cum_cnt = bars['cum_cnt']

    near_dates = []
    near_days = []
    near_values = []
    near_weight = 0.0
    valid_weight_total = 0.0

    for fixed_date in FIXED_DATES:
        start_date = window_start.get(fixed_date)
        if not start_date:
            continue
        start_idx = index_map.get(start_date)
        if start_idx is None:
            continue

        if listed_idx is not None and listed_idx > start_idx:
            continue
        count = cum_cnt[pos + 1] - cum_cnt[start_idx]
        if count < MIN_WINDOW_POINTS:
            continue
        ma = (cum_sum[pos + 1] - cum_sum[start_idx]) / count
        if ma <= 0:
            continue
        weight = _score_weight(count)
        valid_weight_total += weight
        near_close = abs(close_t - ma) / ma <= NEAR_THRESHOLD
        near_low = (low_t is not None) and (abs(low_t - ma) / ma <= NEAR_THRESHOLD)
        if near_close or near_low:
            if _broke_below_ma(bars, start_idx, pos):
                continue
            near_dates.append(fixed_date)
            near_days.append(count)
            near_values.append(round(ma, 2))
            near_weight += weight

    if valid_weight_total <= 0 or not near_dates:
        return None
    score = int(round(near_weight / valid_weight_total * 100.0))
    if score <= 0:
        return None
    high52w = _recent_high(closes, bars['high'], index_map, trade_date, HIGH_LOOKBACK)
    return {
        'near_dates': near_dates,
        'near_days': near_days,
        'near_values': near_values,
        'score': score,
        'close': round(float(close_t), 2),
        'high52w': round(float(high52w), 2) if high52w is not None else 0.0,
    }


def _build_prefix(values):
    cum_sum = [0.0] * (len(values) + 1)
    cum_cnt = [0] * (len(values) + 1)
    for idx in range(len(values)):
        val = values[idx]
        if val is None:
            cum_sum[idx + 1] = cum_sum[idx]
            cum_cnt[idx + 1] = cum_cnt[idx]
        else:
            cum_sum[idx + 1] = cum_sum[idx] + val
            cum_cnt[idx + 1] = cum_cnt[idx] + 1
    return cum_sum, cum_cnt


def _normalize_bars(df):
    if df is None or len(df) == 0:
        return None

    columns = getattr(df, 'columns', None)
    if columns is None or 'close' not in columns:
        return None

    dates = []
    closes = []
    highs = []
    lows = []
    index_map = {}

    close_col = df['close']
    high_col = df['high'] if 'high' in columns else close_col
    low_col = df['low'] if 'low' in columns else close_col

    raw_index = list(df.index)

    if len(raw_index) > 0 and isinstance(raw_index[0], str):
        keys = []
        for item in raw_index:
            digits = ''.join(ch for ch in str(item) if ch.isdigit())
            keys.append(digits[:8] if len(digits) >= 8 else '')
    else:
        keys = []
        for item in raw_index:
            stamp = None
            if isinstance(item, datetime):
                keys.append(item.strftime('%Y%m%d'))
                continue
            try:
                stamp = float(item)
            except (TypeError, ValueError):
                stamp = None
            if stamp is None:
                keys.append('')
            elif stamp >= 1e12:
                keys.append(time.strftime('%Y%m%d', time.localtime(stamp / 1000.0)))
            elif stamp >= 1e9:
                keys.append(time.strftime('%Y%m%d', time.localtime(stamp)))
            else:
                keys.append('%08d' % int(stamp))

    close_vals = list(close_col.values)
    high_vals = list(high_col.values)
    low_vals = list(low_col.values)

    for idx, key in enumerate(keys):
        if not key:
            continue
        if key in index_map:
            continue
        index_map[key] = len(dates)
        dates.append(key)
        closes.append(_float_or_none(close_vals[idx]))
        highs.append(_float_or_none(high_vals[idx]))
        lows.append(_float_or_none(low_vals[idx]))

    if not dates:
        return None
    valid_idx = []
    for idx in range(len(closes)):
        if closes[idx] is not None:
            valid_idx.append(idx)
    cum_sum, cum_cnt = _build_prefix(closes)

    return {
        'dates': dates,
        'close': closes,
        'high': highs,
        'low': lows,
        'index_map': index_map,
        'valid_idx': valid_idx,
        'cum_sum': cum_sum,
        'cum_cnt': cum_cnt,
    }


def _universe_codes(C):
    names = []
    if INDUSTRY_SECTOR:
        names.append(INDUSTRY_SECTOR)
    for name in EXTRA_SECTOR_NAMES:
        if name and name not in names:
            names.append(name)
    if not names:
        return _sector_codes(C, UNIVERSE_SECTOR), UNIVERSE_SECTOR

    merged = []
    seen = set()
    for name in names:
        for code in _sector_codes(C, name):
            if code not in seen:
                seen.add(code)
                merged.append(code)
    merged.sort()
    return merged, '+'.join(names)


def _screen_start(C):
    "\u542f\u52a8\u4e00\u8f6e\u9009\u80a1\uff1a\u51c6\u5907\u7968\u6c60\u3001\u4ea4\u6613\u65e5\u5386\u3001\u5747\u7ebf\u7a97\u53e3\u8d77\u70b9\u3002"
    codes, sector = _universe_codes(C)
    if not codes:
        log("\u9009\u80a1\u542f\u52a8\u5931\u8d25\uff1a\u677f\u5757\u201c%s\u201d\u65e0\u6210\u5206\u80a1" % sector)
        return False
    kept = [code for code in codes if not code.startswith(EXCLUDE_CODE_PREFIXES)]
    if len(kept) != len(codes):
        log("\u5254\u9664\u79d1\u521b\u677f %d \u4e2a" % (len(codes) - len(kept)))
    codes = kept
    if not codes:
        log("\u9009\u80a1\u542f\u52a8\u5931\u8d25\uff1a\u8fc7\u6ee4\u540e\u65e0\u6807\u7684")
        return False

    end_date = _today()
    try:
        calendar = C.get_trading_dates('000001.SH', FIXED_DATES[0], end_date, -1, '1d')
    except Exception as exc:
        log("\u53d6\u4ea4\u6613\u65e5\u5386\u5931\u8d25\uff1a%s" % exc)
        return False
    calendar = [str(item) for item in (calendar or []) if item]
    if not calendar:
        log("\u9009\u80a1\u542f\u52a8\u5931\u8d25\uff1a\u4ea4\u6613\u65e5\u5386\u4e3a\u7a7a")
        return False

    window_start = {}
    for fixed_date in FIXED_DATES:
        after = [day for day in calendar if day >= fixed_date]
        if after:
            window_start[fixed_date] = min(after)
    if not window_start:
        log("\u9009\u80a1\u542f\u52a8\u5931\u8d25\uff1a\u5747\u7ebf\u7a97\u53e3\u8d77\u70b9\u4e3a\u7a7a")
        return False


    prev_days = [day for day in calendar if day < end_date]
    if not prev_days:
        log("\u9009\u80a1\u542f\u52a8\u5931\u8d25\uff1a\u65e5\u5386\u91cc\u6ca1\u6709\u65e9\u4e8e\u4eca\u5929\u7684\u4ea4\u6613\u65e5")
        return False
    trade_date = max(prev_days)

    G.screen = {
        'state': 'load',
        'codes': codes,
        'cursor': 0,
        'scored': [],
        'trade_date': trade_date,
        'start_min': min(window_start.values()),
        'end_date': end_date,
        'window_start': window_start,
        'skipped_bars': 0,
        'skipped_halved': 0,
        'loaded': 0,
    }
    log("\u9009\u80a1\u542f\u52a8\uff1a\u677f\u5757\u201c%s\u201d %d \u53ea\uff0cT=%s\uff0c\u6570\u636e\u533a\u95f4 %s~%s\uff0c\u6bcf\u8f6e %d \u53ea"
        % (sector, len(codes), trade_date, G.screen['start_min'], end_date, SCREEN_BATCH))
    return True


def _screen_step(C):
    "\u63a8\u8fdb\u4e00\u8f6e\u9009\u80a1\uff08\u5904\u7406\u4e00\u6279\u6807\u7684\uff09\u3002\u8fd4\u56de True \u8868\u793a\u6574\u8f6e\u5df2\u5b8c\u6210\u3002"
    state = G.screen.get('state')
    if state != 'load':
        return True

    codes = G.screen['codes']
    cursor = G.screen['cursor']
    batch = codes[cursor:cursor + SCREEN_BATCH]
    if not batch:
        _screen_finish(C)
        return True

    try:
        data = C.get_market_data_ex(
            ['close', 'high', 'low'],
            batch,
            '1d',
            G.screen['start_min'],
            G.screen['end_date'],
            -1,
            DIVIDEND_TYPE,
            False,
        )
    except Exception as exc:
        log("\u9009\u80a1\u53d6\u6570\u5931\u8d25\uff08%d~%d\uff09\uff1a%s" % (cursor, cursor + len(batch), exc))
        data = None

    if data:
        for code in batch:
            df = data.get(code)
            if df is None or len(df) == 0:
                continue
            bars = _normalize_bars(df)
            if bars is None:
                continue
            if len(bars['valid_idx']) < MIN_BARS:
                G.screen['skipped_bars'] += 1
                continue
            info = _score_symbol(bars, G.screen['trade_date'], G.screen['window_start'])
            if info is None:

                if not (REQUIRE_HALVED
                        and _is_halved(bars['close'], bars['high'], bars['index_map'],
                                       G.screen['trade_date'])):
                    G.screen['skipped_halved'] += 1
                continue
            info['code'] = code
            G.screen['scored'].append(info)
            G.screen['loaded'] += 1

    G.screen['cursor'] = cursor + len(batch)
    if G.screen['cursor'] >= len(codes):
        _screen_finish(C)
        return True
    return False


def _screen_finish(C):
    "\u9009\u80a1\u6536\u5c3e\uff1a\u6392\u5e8f\u53d6\u540d\u6b21\uff0c\u6392\u9664 ST\uff0c\u5199\u5165 G.target_symbols\u3002"
    scored = G.screen.get('scored', [])
    total = len(G.screen.get('codes', []))
    log("\u9009\u80a1\u6253\u5206\u5b8c\u6210\uff1a%d/%d \u53ea\u63a5\u8fd1\u81f3\u5c11\u4e00\u6761\u5747\u7ebf\uff0c\u65e5\u7ebf\u4e0d\u8db3\u5254\u9664 %d\uff0c\u672a\u8170\u65a9\u5254\u9664 %d"
        % (len(scored), total, G.screen.get('skipped_bars', 0),
           G.screen.get('skipped_halved', 0)))

    if not scored:
        log("\u9009\u80a1\u7ed3\u679c\u4e3a\u7a7a\uff0c\u4fdd\u6301\u73b0\u6709\u7968\u6c60")
        G.screen = {'state': 'done'}
        return

    scored.sort(key=lambda item: (-item['score'], item['code']))
    candidates = scored[:TOP_N + NAME_CHECK_BUFFER]


    kept = []
    dropped_st = 0
    for info in candidates:
        code = info['code']
        if G.st_codes and code in G.st_codes:
            dropped_st += 1
            continue
        kept.append(info)
    if not G.st_codes:
        names = _fetch_names(C, [info['code'] for info in kept[:TOP_N + NAME_CHECK_BUFFER]])
        filtered = []
        for info in kept:
            name = names.get(info['code'], '')
            if 'ST' in name.upper():
                dropped_st += 1
                continue
            filtered.append(info)
        kept = filtered
    if dropped_st:
        log("\u5254\u9664 ST/*ST %d \u53ea" % dropped_st)

    top = kept[:TOP_N]
    if not top:
        log("\u9009\u80a1\u7ed3\u679c\u4e3a\u7a7a\uff08ST \u8fc7\u6ee4\u540e\uff09\uff0c\u4fdd\u6301\u73b0\u6709\u7968\u6c60")
        G.screen = {'state': 'done'}
        return

    trade_date = G.screen.get('trade_date', '')
    targets = {}
    universe = {}
    for info in top:
        code = info['code']
        targets[code] = ''
        universe[code] = info
    G.target_symbols = targets
    G.universe_data = universe
    G.screen = {'state': 'done'}
    log("\u9009\u80a1\u5b8c\u6210\uff1a%d \u53ea\u5165\u6c60\uff0c\u6700\u9ad8\u5206 %d\uff0c\u6700\u4f4e\u5206 %d\uff0cT=%s"
        % (len(top), top[0]['score'], top[-1]['score'], trade_date))


def _maybe_screen(C):
    if not G.is_trade_day:
        return


    if not _in_window(SCREEN_START, SCREEN_DEADLINE) and not G.screen:
        late_ok = (ALLOW_LATE_SCREEN and not G.target_symbols
                   and _clock() <= TRADE_END_TIME)
        if not late_ok:
            return
        if _alert_once('late_screen'):
            log("\u5df2\u8fc7\u9009\u80a1\u65f6\u6bb5\uff08%s~%s\uff09\u4e14\u7968\u6c60\u4e3a\u7a7a\uff0c\u8865\u8dd1\u4e00\u6b21\u9009\u80a1\uff08\u4f1a\u5360\u7528\u5171\u4eab\u7ebf\u7a0b\uff0c\u7ea6 1~2 \u5206\u949f\uff09"
                % (SCREEN_START, SCREEN_DEADLINE))
    if not G.screen:

        if _now_ts() < G.screen_retry_at:
            return
        G.screen_retry_at = _now_ts() + SCREEN_RETRY
        _screen_start(C)
        return
    was_loading = G.screen.get('state') == 'load'
    if _screen_step(C) and was_loading and G.screen.get('state') == 'done':


        G.pool_date = ''


# -----------------------------------------------------------------------------
# 5.4 market data registration (tick callback + per-round batch poll)
# -----------------------------------------------------------------------------

def _make_on_quote(code):
    "\u4e3a\u5355\u4e2a\u6807\u7684\u751f\u6210\u884c\u60c5\u56de\u8c03\u95ed\u5305\u3002\u56de\u8c03\u4e0e\u7b56\u7565\u540c\u7ebf\u7a0b\uff0c\u53ea\u505a\u7f13\u5b58\uff0c\u4e0d\u505a\u5224\u5b9a\u3002"

    def on_quote(data):
        try:
            if not data:
                return
            tick = data.get(code)
            if not tick:
                return
            _on_price(code, _f(tick.get('lastPrice'), 0.0), _f(tick.get('lastClose'), 0.0))
        except Exception:

            pass

    return on_quote


def _on_price(code, price, pre_close):
    "\u767b\u8bb0\u6700\u65b0\u4ef7\u4e0e\u7a97\u53e3\u6837\u672c\uff08\u8f7b\u91cf\uff0ctick \u56de\u8c03\u4e0e\u4e3b\u5faa\u73af\u5171\u7528\uff09\u3002"
    if price <= 0:
        return
    G.last[code] = {'price': price, 'pre_close': pre_close, 'ts': _now_ts()}

    if code in G.target_symbols and code not in G.entry_prices:
        samples = G.window.get(code)
        if samples is None:
            samples = []
            G.window[code] = samples
        stamp = _now_ts()
        samples.append((stamp, price))
        cutoff = stamp - SURGE_WINDOW
        while samples and samples[0][0] < cutoff:
            samples.pop(0)


def _watch_codes(positions=None):
    codes = list(G.target_symbols.keys())
    seen = set(codes)
    if positions is None:
        positions = _pos_map()
    for code, info in positions.items():
        if info.get('volume', 0) > 0 and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _subscribe_pool(C, positions=None):
    codes = _watch_codes(positions)
    count = 0
    for code in codes:
        try:
            C.subscribe_quote(code, period='tick', result_type='dict',
                              callback=_make_on_quote(code))
            count += 1
        except Exception as exc:
            log("\u8ba2\u9605 %s \u5931\u8d25\uff08\u5df2\u8ba2\u9605 %d \u53ea\uff09\uff1a%s\uff0c\u9000\u56de\u8f6e\u8be2\u53d6\u4ef7" % (code, count, exc))
            break
    G.tick_subscribed = count
    log("tick \u8ba2\u9605\uff1a%d/%d \u53ea" % (count, len(codes)))
    return count


def _poll_pool(C, positions=None):
    "\u4e3b\u5faa\u73af\u6279\u91cf\u53d6\u4ef7\uff08\u7968\u6c60 + \u6301\u4ed3\uff09\u3002get_full_tick \u65e0\u8ba2\u9605\u6570\u9650\u5236\u4e14\u5f00\u9500\u5c0f\u3002"
    codes = _watch_codes(positions)
    if not codes:
        return
    try:
        ticks = C.get_full_tick(codes) or {}
    except Exception as exc:
        log("\u8f6e\u8be2\u884c\u60c5\u5931\u8d25\uff1a%s" % exc)
        return
    for code in codes:
        tick = ticks.get(code)
        if not tick:
            continue
        _on_price(code, _f(tick.get('lastPrice'), 0.0), _f(tick.get('lastClose'), 0.0))


# -----------------------------------------------------------------------------
# 5.5 trade infrastructure: query, order, reconcile, pool refresh
# -----------------------------------------------------------------------------

QUERY_KINDS = {
    'position': ('position', 'POSITION'),
    'order': ('order', 'ORDER'),
    'deal': ('deal', 'DEAL'),
    'account': ('account', 'ACCOUNT'),
}


def _query(kind, strategy_name=''):
    "\u67e5\u67dc\u53f0\u6570\u636e\uff08\u6301\u4ed3/\u59d4\u6258/\u6210\u4ea4/\u8d44\u91d1\uff09\uff0c\u8fd4\u56de\u884c\u5217\u8868\uff1b\u67e5\u4e0d\u5230\u8fd4\u56de []\u3002"
    for candidate in QUERY_KINDS.get(kind, (kind, kind.upper())):
        try:
            if strategy_name:
                rows = get_trade_detail_data(account, accountType, candidate, strategy_name)
            else:
                rows = get_trade_detail_data(account, accountType, candidate)
        except Exception as exc:

            if _alert_once('query_fail', kind):
                log("\u67e5\u8be2 %s\uff08datatype=%s\uff09\u5931\u8d25\uff1a%s" % (kind, candidate, exc))
            continue
        if rows:
            return rows
    return []


def _positions():
    "\u53d6\u6301\u4ed3\u5217\u8868\u3002QMT \u6301\u4ed3\u67e5\u8be2\u53ea\u8fd4\u56de\u6709\u4ed3\u7684\u884c\uff08\u5b8c\u5168\u5e73\u6389\u7684\u4e0d\u4f1a\u51fa\u73b0\uff09\u3002"
    return _query('position')


def _pos_map():
    "code -> dict(volume, can_use, open_price)\u3002QMT \u6301\u4ed3\u67e5\u8be2\u53ea\u8fd4\u56de\u6709\u4ed3\u7684\u884c\u3002"
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
    "\u6301\u4ed3\u5bf9\u8c61\u7684\u4ee3\u7801\uff1am_strInstrumentID + '.' + m_strExchangeID\u3002"
    symbol = str(_attr(pos, 'm_strInstrumentID', '') or '')
    market = str(_attr(pos, 'm_strExchangeID', '') or '')
    if not symbol or not market:
        return ''
    return _norm_code(symbol + '.' + market)


def _available_cash():
    "\u8d26\u6237\u53ef\u7528\u8d44\u91d1\u3002"
    rows = _query('account')
    if not rows:
        return 0.0
    return _f(_attr(rows[0], 'm_dAvailable', 0.0))


def _pending_buy_amount():
    "\u5728\u9014\u4e70\u5355\u5360\u7528\u91d1\u989d\u3002"
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
    if positions is None:
        positions = _pos_map()
    total = 0.0
    for info in positions.values():
        if info['open_price'] > 0 and info['volume'] > 0:
            total += info['open_price'] * info['volume']
    total += _pending_buy_amount()
    return total


def _has_active_order(code):
    "\u8be5\u6807\u7684\u662f\u5426\u6709\u5728\u9014\u59d4\u6258\u3002"
    for order in G.pending.values():
        if order.get('code') == code and order.get('status') == 'alive':
            return True
    return False


def _sync_entry_from_broker(code, info):
    "\u672c\u5730\u6ca1\u6709\u5f00\u4ed3\u4ef7\u65f6\uff0c\u7528\u5238\u5546\u6210\u672c\u4ef7\u515c\u5e95\uff08\u5238\u5546\u7ed9\u7684\u591a\u4e3a\u644a\u8584\u6210\u672c\u4ef7\uff0c\u4e0d\u53ef\u53cd\u5411\u8986\u76d6\uff09\u3002"
    if code in G.entry_prices:
        return
    if info.get('open_price', 0.0) > 0:
        G.entry_prices[code] = info['open_price']
        G.max_profit_pct.setdefault(code, 0.0)
        log("%s \u7528\u5238\u5546\u6210\u672c\u4ef7 %.3f \u515c\u5e95\u5f00\u4ed3\u4ef7" % (code, info['open_price']))


def _op_type(side):
    "\u4e70\u5356 opType\uff1a\u666e\u901a\u8d26\u6237 23/24\uff0c\u4e24\u878d\u62c5\u4fdd\u54c1 33/34\u3002"
    if accountType == 'CREDIT':
        return 33 if side == 'BUY' else 34
    return 23 if side == 'BUY' else 24


def _place_order(C, code, side, volume, price, tag):
    "\u4e0b\u5355\uff1b\u540c\u6807\u7684\u6709\u5728\u9014\u59d4\u6258\u65f6\u62d2\u7edd\uff08\u9632\u8d85\u5355\uff09\u3002\u8fd4\u56de userOrderId \u6216 None\u3002"
    if volume <= 0:
        return None
    if _has_active_order(code):
        log("\u8df3\u8fc7\u4e0b\u5355\uff1a%s \u5df2\u6709\u5728\u9014\u59d4\u6258" % code)
        return None
    G.seq += 1
    uid = '%s_%s_%d' % (tag or STRATEGY_NAME, G.day, G.seq)
    try:

        passorder(_op_type(side), 1101, account, code, 11, float(price),
                  int(volume), STRATEGY_NAME, 2, uid, C)
    except Exception as exc:
        log("\u4e0b\u5355\u5f02\u5e38 %s %s %d @%.3f\uff1a%s" % (side, code, volume, price, exc))
        return None
    G.pending[uid] = {
        'code': code, 'side': side, 'vol': int(volume), 'status': 'alive',
        'sysid': '', 'traded': 0, 'price': float(price), 'ts': _now_ts(),
    }
    log("\u4e0b\u5355 %s %s %d\u80a1 @%.3f uid=%s" % (side, code, volume, price, uid))
    return uid


ALIVE_STATUS = (48, 49, 50, 51, 52, 55, 86, 255)


FINAL_STATUS = (53, 54, 56, 57)


def _sync_orders():
    "\u8f6e\u8be2\u5bf9\u8d26\uff1a\u628a\u67dc\u53f0\u59d4\u6258\u6309 m_strRemark \u5173\u8054\u56de pending\uff08\u56de\u8c03\u4e4b\u5916\u7684\u515c\u5e95\uff09\u3002"
    orders = _query('order', STRATEGY_NAME)
    if not orders:

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
            log("\u8b66\u544a\uff1a\u59d4\u6258 %s\uff08%s\uff09\u8d85\u8fc7 %.0f \u79d2\u672a\u89c1\u67dc\u53f0\u59d4\u6258\u53f7\uff0c\u6807\u8bb0\u5f02\u5e38\uff0c\u8bf7\u4eba\u5de5\u6838\u5bf9"
                % (uid, record.get('code', ''), PENDING_LOST_SECONDS))


def _refresh_pool(C):
    "\u76d8\u524d 09:15 \u5237\u65b0\uff1a\u786e\u8ba4\u9009\u80a1\u7ed3\u679c\u53ef\u7528\u3001\u540c\u6b65\u5238\u5546\u6301\u4ed3\u515c\u5e95\u3001\u8ba2\u9605 tick\u3001\u7b97\u5e03\u6797\u4e0a\u8f68\u3002"
    if not G.target_symbols:
        log("\u7968\u6c60\u4e3a\u7a7a\uff08\u9009\u80a1\u53ef\u80fd\u672a\u5b8c\u6210\uff09\uff0c\u672c\u8f6e\u4e0d\u4ea4\u6613")
    G.pool_date = G.day


    positions = _pos_map()
    for code, info in positions.items():
        if info['volume'] > 0:
            _sync_entry_from_broker(code, info)
            G.entered.add(code)
            G.max_profit_pct.setdefault(code, 0.0)

    subscribed = _subscribe_pool(C, positions)
    _refresh_boll(C)
    log("\u7968\u6c60\u5237\u65b0\u5b8c\u6210\uff1a\u6c60\u5185 %d \u53ea\uff0c\u6301\u4ed3 %d \u53ea\uff0c\u51b7\u5374\u4e2d %d \u53ea\uff0ctick \u8ba2\u9605 %d \u53ea"
        % (len(G.target_symbols), len(positions), len(G.cooldown_dates), subscribed))


# -----------------------------------------------------------------------------
# 5.6 misc helpers
# -----------------------------------------------------------------------------

def _fmt_context(market_level, market_score, sector_name, sector_level, sector_score):
    "\u62fc\u88c5\u5927\u76d8 + \u884c\u4e1a\u60c5\u7eea\u4e0a\u4e0b\u6587\u3002"
    return ("\u5927\u76d8 \u8bc4\u7ea7 %s \u5f97\u5206 %.1f \u884c\u4e1a %s \u8bc4\u7ea7 %s \u5f97\u5206 %.1f"
            % (market_level or "\u4e0d\u53ef\u7528", market_score,
               sector_name or '-', sector_level or "\u4e0d\u53ef\u7528", sector_score))

