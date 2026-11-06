# -*- coding: utf-8 -*-
"""市场/行业情绪：全市场宽度 + 指数 + 板块合成大盘分，行业评级供准入与情绪离场。
"""

from .config import BREADTH_WEIGHT, CHANGE_SCALE, ENABLE_SECTOR_FACTOR, EXTRA_SECTOR_NAMES, INDEX_SYMBOLS, INDEX_WEIGHT, INDUSTRY_SECTOR, LOAD_ALL_SECTORS, MIN_SECTOR_VALID, REFRESH_TIME, SECTOR_PREFIX, SECTOR_WEIGHT, SENTIMENT_INTERVAL, SENTIMENT_STALE_AFTER, UNIVERSE_SECTOR
from .sectors import SW2_SECTOR_NAMES
from .state import G, _clock, _f, _now_ts, log
from .screen import _load_st_codes, _sector_codes



# =============================================================================
# 五、情绪（每 SENTIMENT_INTERVAL 秒全市场采样，自算）
#
# 原版是 vnpy_marketsentiment 后台线程（30s 全市场）；内置端禁多线程，
# 改为在主循环里节流调用。算法与原版一致：
#   大盘分 = 宽度 0.55 + 指数 0.30 + 板块 0.15（缺项按可用权重归一）
#   宽度分 = clamp(50 + 净涨跌占比*50)*0.75 + clamp(50 + 平均涨幅/2*50)*0.25
#   指数分 = clamp(50 + 有效指数平均涨幅/2*50)
#   板块分 = 最强 3 个板块分数均值
#   分档： <20 极弱 / <45 偏弱 / <60 中性 / <80 偏强 / >=80 极强
# =============================================================================


def _clamp(value):
    """限制到 [0, 100]。"""
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value



def _classify(score):
    """分数 → 情绪评级（中文串，与原版 SentimentLevel.value 一致）。"""
    if score < 20.0:
        return '极弱'
    if score < 45.0:
        return '偏弱'
    if score < 60.0:
        return '中性'
    if score < 80.0:
        return '偏强'
    return '极强'



def _refresh_daily_codes(C):
    """日切时刷新全市场代码表、ST 集合、板块成员缓存。"""
    G.all_codes = _sector_codes(C, UNIVERSE_SECTOR)
    if not G.all_codes:
        log('警告：未取到板块“%s”成分，情绪与选股都会降级' % UNIVERSE_SECTOR)
    G.st_codes = _load_st_codes(C)
    G.sector_members = {}
    G.stock_sector = {}
    _load_sector_members(C)



def _load_sector_members(C):
    """构建板块成员与反向映射（当日内缓存）。

    板块清单优先级：INDUSTRY_SECTOR（单行业）→ EXTRA_SECTOR_NAMES（多行业）→
    全市场模式下的内置 SW2 清单（LOAD_ALL_SECTORS）→ ContextInfo.get_sector_list
    （本机 QMT 没有该接口，其他客户端可能有，能取到就当额外补充）。
    """
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
    log('板块成员缓存：%d 个板块，覆盖率 %d 只标的（耗时 %.2fs）'
        % (len(G.sector_members), len(G.stock_sector), _now_ts() - started))
    if not G.sector_members:
        log('警告：一个板块成员都没取到 —— 行业准入会拦截全部买入（一笔都买不进）。'
            '请把 INDUSTRY_SECTOR / EXTRA_SECTOR_NAMES 设为本机真实板块名'
            '（如 SW2半导体），板块名见客户端「板块 → 申万二级行业板块」')



def _breadth(codes, ticks):
    """算一组标的的宽度统计。返回 dict 或 None（无有效标的）。"""
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
    """宽度分：净涨跌占比 75% + 平均涨跌幅 25%。"""
    count_score = _clamp(50.0 + breadth['net'] * 50.0)
    return_score = _clamp(50.0 + breadth['average'] / CHANGE_SCALE * 50.0)
    return count_score * 0.75 + return_score * 0.25



def _index_score(ticks):
    """指数分：有效指数平均涨跌幅映射；全部无效返回 None。"""
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



def _sentiment_sample(C):
    """一次全市场采样：算大盘分 + 各板块分。"""
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
        log('情绪采样取数失败：%s' % exc)
        return

    breadth = _breadth(codes, ticks)
    if breadth is None:
        G.sent_ts = _now_ts()
        G.sent_level = '不可用'
        G.sent_valid = 0
        G.sent_score = 0.0
        return

    components = [(_breadth_score(breadth), BREADTH_WEIGHT)]
    index_score = _index_score(ticks)
    if index_score is not None:
        components.append((index_score, INDEX_WEIGHT))

    # 板块分：最强 3 个板块分数均值，同时刷新各板块状态（供行业准入/离场判定）
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
    """按间隔触发一次情绪采样（仅在开盘前 5 分钟到收盘之间）。"""
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



def _sentiment_available():
    """大盘快照是否可信（未过期 + 有效标的充足）。"""
    if G.sent_valid <= 0 or G.sent_level == '不可用':
        return False
    return (_now_ts() - G.sent_ts) <= SENTIMENT_STALE_AFTER



def _market_values():
    """(大盘评级, 大盘分)，供消息拼装。"""
    return G.sent_level, G.sent_score



def _sector_values(code):
    """该标的所属板块的 (板块名, 评级, 分数)。

    单行业模式下池内标的都属 INDUSTRY_SECTOR；全市场模式下按反向映射查。
    """
    name = INDUSTRY_SECTOR or G.stock_sector.get(code, '')
    if not name:
        return '', '未分行业', 0.0
    state = G.sent_sectors.get(name)
    if state is None:
        return '', '未分行业', 0.0
    return name, state['level'], state['score']



def _sector_usable(code):
    """该标的的板块情绪是否可用（板块在岗）。"""
    name = INDUSTRY_SECTOR or G.stock_sector.get(code, '')
    if not name:
        return False
    return name in G.sent_sectors



def _sector_buyable(code):
    """行业准入判定；返回 (buyable, blocked_market, 板块名, 评级, 分数, 大盘评级, 大盘分)。

    规则（与原版 SectorBuySignal 一致）：
      大盘偏弱/极弱 → 一票否决（不查行业）
      大盘可用且偏强/极强 → 行业需 中性及以上
      其余（大盘中性 / 快照不可用）→ 行业需 中性以上（不含中性）
    """
    level = G.sent_level
    score = G.sent_score
    if level in ('偏弱', '极弱'):
        return False, True, '', '', 0.0, level, score
    name, sector_level, sector_score = _sector_values(code)
    if not name:
        return False, False, '', sector_level, sector_score, level, score
    if _sentiment_available() and level in ('偏强', '极强'):
        ok = sector_level in ('中性', '偏强', '极强')
    else:
        ok = sector_level in ('偏强', '极强')
    return ok, False, name, sector_level, sector_score, level, score



def _sector_sellable(code):
    """行业情绪离场判定；返回 (sellable, 板块名, 评级, 分数)。"""
    name, sector_level, sector_score = _sector_values(code)
    if not name:
        return False, '', sector_level, sector_score
    return sector_level in ('偏弱', '极弱'), name, sector_level, sector_score
