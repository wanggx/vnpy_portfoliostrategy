# -*- coding: utf-8 -*-
"""盘前选股：全 A 股前复权日线 → 5 条成本均线贴近度打分 → 腰斩过滤 → TOP N 入池。
"""

import bisect
from datetime import datetime
import math
import time

from .config import BREAK_LOOKBACK, BREAK_THRESHOLD, DIVIDEND_TYPE, EXCLUDE_CODE_PREFIXES, EXTRA_SECTOR_NAMES, FIXED_DATES, HALF_RATIO, HIGH_LOOKBACK, INDUSTRY_SECTOR, MIN_BARS, MIN_WINDOW_POINTS, NAME_CHECK_BUFFER, NEAR_THRESHOLD, REQUIRE_HALVED, SCREEN_BATCH, SCREEN_DEADLINE, SCREEN_RETRY, SCREEN_START, ST_SECTOR_NAMES, TOP_N, UNIVERSE_SECTOR, WEIGHT_MODE
from .state import G, _float_or_none, _in_window, _is_stock_code, _norm_code, _now_ts, _today, log



# =============================================================================
# 四、选股（盘前分片状态机）
#
# 原版是盘后 16:00 的外部脚本 + 落库；内置端禁 IO，改为盘前 08:40 起在内存里跑，
# 每轮定时器只处理 SCREEN_BATCH 只，避免长时间占用共享线程。
# =============================================================================


def _sector_codes(C, sector_name):
    """取板块成分股；失败返回空列表。"""
    if not sector_name:
        return []
    try:
        codes = C.get_stock_list_in_sector(sector_name)
    except Exception as exc:
        log('读取板块 %s 失败：%s' % (sector_name, exc))
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
    """加载 ST/*ST 代码集合（板块差集）。

    取不到就留空集合，由 `_fetch_names` 的名称兜底接手。
    """
    names = ST_SECTOR_NAMES if isinstance(ST_SECTOR_NAMES, tuple) else (ST_SECTOR_NAMES,)
    for name in names:
        codes = _sector_codes(C, name)
        if codes:
            log('ST 排除用板块“%s”：%d 个' % (name, len(codes)))
            return set(codes)
    log('未取到 ST 板块，改用名称兜底排除')
    return set()



def _fetch_names(C, codes):
    """批量取标的名称；取不到的返回空串。"""
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
    """单条均线权重。"""
    if WEIGHT_MODE == 'equal':
        return 1.0
    if WEIGHT_MODE == 'log':
        return math.log(1.0 + days)
    return float(days)



def _recent_high(close_values, high_values, index_map, trade_date, lookback):
    """近 lookback 个交易日最高价；high 缺失时用 close 近似。无有效数据返回 None。"""
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
    """T 日收盘价相对近 HIGH_LOOKBACK 个交易日最高价是否已腰斩。

    窗口无有效数据视为无法判定 → 返回 True（保守不剔，与原版一致）。
    """
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
    """均线窗口 [start_idx, pos) 内、T 日之前近 BREAK_LOOKBACK 个有效日是否跌破当时均线。

    当时均线 = 窗口起点到当天的收盘价累计均值。用前缀和取 O(1) 均值，
    避免每个窗口逐日重算（全市场 5000 只 × 5 窗口时这笔开销很可观）。
    """
    valid_idx = bars['valid_idx']
    cum_sum = bars['cum_sum']
    cum_cnt = bars['cum_cnt']
    closes = bars['close']
    left = bisect.bisect_left(valid_idx, start_idx)
    right = bisect.bisect_left(valid_idx, pos)      # 不含 T 日
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
    """单标的打分；返回 dict 或 None（跳过）。口径与原版 _score_symbol 一致。"""
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
        # 起点当天还没上市：该成本均线不成立（不能退化成「上市以来均价」）
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
    """前缀和 / 前缀有效计数：cum[i] 表示前 i 个元素（不含 i）的统计。

    用于把「窗口均值」降到 O(1)。None 表示缺失（停牌 / 未上市 / NaN）。
    """
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
    """把单标的行情 DataFrame 转成打分用的扁平结构；不可用返回 None。

    返回 dict(dates, close, high, low, index_map, valid_idx, cum_sum, cum_cnt)。
    index 兼容 'YYYYMMDD' / int 毫秒时间戳 / datetime 三种；无效值统一取 None。
    """
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
    # 统一成 YYYYMMDD 字符串
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
    # 已按 source 顺序（时间升序）写入
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
    """当日选股池：INDUSTRY_SECTOR + EXTRA_SECTOR_NAMES 的并集；都没设则用全市场板块。

    本机 QMT 没有 `get_sector_list`，板块清单只能来自配置（刻意为之：写清楚比
    靠接口探测更可靠）。返回 (codes, 描述)。
    """
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
    """启动一轮选股：准备票池、交易日历、均线窗口起点。"""
    codes, sector = _universe_codes(C)
    if not codes:
        log('选股启动失败：板块“%s”无成分股' % sector)
        return False
    kept = [code for code in codes if not code.startswith(EXCLUDE_CODE_PREFIXES)]
    if len(kept) != len(codes):
        log('剔除科创板 %d 个' % (len(codes) - len(kept)))
    codes = kept
    if not codes:
        log('选股启动失败：过滤后无标的')
        return False

    end_date = _today()
    try:
        calendar = C.get_trading_dates('000001.SH', FIXED_DATES[0], end_date, -1, '1d')
    except Exception as exc:
        log('取交易日历失败：%s' % exc)
        return False
    calendar = [str(item) for item in (calendar or []) if item]
    if not calendar:
        log('选股启动失败：交易日历为空')
        return False

    window_start = {}
    for fixed_date in FIXED_DATES:
        after = [day for day in calendar if day >= fixed_date]
        if after:
            window_start[fixed_date] = min(after)
    if not window_start:
        log('选股启动失败：均线窗口起点为空')
        return False

    # 计算交易日 T：盘前跑，取最后一个「小于今天」的交易日；数据缺失的标的在打分时跳过
    prev_days = [day for day in calendar if day < end_date]
    if not prev_days:
        log('选股启动失败：日历里没有早于今天的交易日')
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
    log('选股启动：板块“%s” %d 只，T=%s，数据区间 %s~%s，每轮 %d 只'
        % (sector, len(codes), trade_date, G.screen['start_min'], end_date, SCREEN_BATCH))
    return True



def _screen_step(C):
    """推进一轮选股（处理一批标的）。返回 True 表示整轮已完成。"""
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
        log('选股取数失败（%d~%d）：%s' % (cursor, cursor + len(batch), exc))
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
                # 区分「未腰斩被剔除」与「无均线接近/数据问题」，只影响计数口径
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
    """选股收尾：排序取名次，排除 ST，写入 G.target_symbols。"""
    scored = G.screen.get('scored', [])
    total = len(G.screen.get('codes', []))
    log('选股打分完成：%d/%d 只接近至少一条均线，日线不足剔除 %d，未腰斩剔除 %d'
        % (len(scored), total, G.screen.get('skipped_bars', 0),
           G.screen.get('skipped_halved', 0)))

    if not scored:
        log('选股结果为空，保持现有票池')
        G.screen = {'state': 'done'}
        return

    scored.sort(key=lambda item: (-item['score'], item['code']))
    candidates = scored[:TOP_N + NAME_CHECK_BUFFER]

    # 排除 ST：优先用板块差集，其次查名称
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
        log('剔除 ST/*ST %d 只' % dropped_st)

    top = kept[:TOP_N]
    if not top:
        log('选股结果为空（ST 过滤后），保持现有票池')
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
    log('选股完成：%d 只入池，最高分 %d，最低分 %d，T=%s'
        % (len(top), top[0]['score'], top[-1]['score'], trade_date))



def _maybe_screen(C):
    """盘前推进选股状态机（每轮只做一小步）。

    正常只在 SCREEN_START~SCREEN_DEADLINE 内跑；若已过时段但当天还没跑过选股、
    票池仍为空（中途启动策略），在 ALLOW_LATE_SCREEN 开启时补跑一次 ——
    否则启动晚了当天就完全不会交易。
    """
    if not G.is_trade_day:
        return
    # 已过选股时段、且当天还没跑过选股（中途启动策略）→ 补跑一次。
    # G.screen 一旦被写入（含 {'state': 'done'}）就代表今天跑过，不再补跑，
    # 这样「选股结果为空」也不会被反复重扫。
    # 注意：这里只能拦「是否启动」—— 补跑一旦开始（G.screen 非空），
    # 后续轮次必须能继续推进，否则会永远停在第一只标的。
    if not _in_window(SCREEN_START, SCREEN_DEADLINE) and not G.screen:
        late_ok = (ALLOW_LATE_SCREEN and not G.target_symbols
                   and _clock() <= TRADE_END_TIME)
        if not late_ok:
            return
        if _alert_once('late_screen'):
            log('已过选股时段（%s~%s）且票池为空，补跑一次选股（会占用共享线程，约 1~2 分钟）'
                % (SCREEN_START, SCREEN_DEADLINE))
    if not G.screen:
        # 启动失败（如板块名错 / 数据未下载）时隔 SCREEN_RETRY 秒再试，避免日志刷屏
        if _now_ts() < G.screen_retry_at:
            return
        G.screen_retry_at = _now_ts() + SCREEN_RETRY
        _screen_start(C)
        return
    was_loading = G.screen.get('state') == 'load'
    if _screen_step(C) and was_loading and G.screen.get('state') == 'done':
        # 选股刚完成：把票池标记为「未刷新」，让下一轮走正常的票池刷新流程
        # （订阅 tick / 算布林上轨 / 同步持仓兜底），与盘前启动的行为一致
        G.pool_date = ''
