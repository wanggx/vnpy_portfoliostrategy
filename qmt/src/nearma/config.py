# -*- coding: utf-8 -*-
"""参数与常量：账户、选股口径、交易参数、时段、情绪权重、日志开关。
"""



# =============================================================================
# 一、参数
# =============================================================================

# --- 账户（由策略交易界面注入，勿自行定义同名变量）---
try:
    ACCOUNT = account
except NameError:
    ACCOUNT = ''

try:
    ACCOUNT_TYPE = accountType
except NameError:
    ACCOUNT_TYPE = 'STOCK'


STRATEGY_NAME = 'NEARMA'          # passorder 的 strategyName，用于过滤查询


# 源码指纹：由 qmt/build.py 合成时替换成本次源码的短哈希。
# 作用：策略启动时打进 QMT 日志（build=xxxxxxxx），用来核对「粘进去的是不是最新版」。
# 源码里保持 'dev'，只有合成产物会被替换。
BUILD_ID = 'dev'


# --- 选股：标的池与过滤 ---
UNIVERSE_SECTOR = '沪深A股'        # 全市场板块名（QMT 标准板块）

INDUSTRY_SECTOR = ''              # 非空则只在申万板块内选股，如 'SW2半导体'；空 = 全市场
# 多个行业时填这里：与 INDUSTRY_SECTOR 取并集作为选股池，行业准入按各标的所属行业。
# 本机 QMT（国金交易端）的申万二级板块名格式为 'SW2<行业名>'，已核实存在的有：
#   SW2半导体 / SW2贵金属 / SW2小金属 / SW2汽车零部件
# 完整清单：客户端「板块 → 申万二级行业板块」，或
#   <QMT安装目录>\datadir\Sector\申万二级行业板块\sectorConfig.xml
EXTRA_SECTOR_NAMES = ()
# 全市场模式（INDUSTRY_SECTOR 与 EXTRA_SECTOR_NAMES 都为空）时，是否加载全部申万二级板块。
# 为什么要这样：本机 QMT 没有 `get_sector_list`，而行业准入 / 情绪离场需要「股票 → 行业」
# 反向映射；全市场模式下只能按内置清单（sectors.SW2_SECTOR_NAMES，131 个）逐个取成分股。
# 代价：每天首次刷新板块时多 131 次 get_stock_list_in_sector（进程内调用，约 1~3 秒）。
LOAD_ALL_SECTORS = True

EXCLUDE_CODE_PREFIXES = ('688', '689')   # 不做科创板

ST_SECTOR_NAMES = ('风险警示', 'ST板块')  # 用板块差集排除 ST，取不到时退化为查名称


# --- 选股：打分口径（与原版 select_near_ma_xtquant.py 一致）---
FIXED_DATES = ('20220427', '20221031', '20240205', '20240918', '20250407')

MIN_BARS = 100                    # 可用前复权日线少于该值不考虑（上市太近/长期停牌）

NEAR_THRESHOLD = 0.01             # 接近带：|price - ma| / ma <= 1%

BREAK_LOOKBACK = 20               # 近 N 个交易日跌穿均线则不算接近

BREAK_THRESHOLD = 0.03            # 跌穿幅度 3%

TOP_N = 100                       # 取分数前 N 只

MIN_WINDOW_POINTS = 5             # 单条均线窗口有效点数下限

DIVIDEND_TYPE = 'front'           # 前复权

REQUIRE_HALVED = True             # 要求相对近 252 日最高价腰斩

HALF_RATIO = 0.5

HIGH_LOOKBACK = 252

WEIGHT_MODE = 'days'              # days=天数即权重 / equal=等权 / log=对数压缩

NAME_CHECK_BUFFER = 20            # ST 名称兜底检查时多取多少个候选


SCREEN_BATCH = 200                # 每轮定时器处理的标的数（分片，避免阻塞）

SCREEN_START = '08:40:00'         # 盘前选股开始时刻

SCREEN_DEADLINE = '09:20:00'      # 选股必须在此前完成

SCREEN_RETRY = 30.0               # 选股启动失败后的重试间隔（秒），避免日志刷屏

# 中途启动策略（已过选股时段）且当天还没跑过选股时，补跑一次。
# 否则只要启动晚于 09:20，当天就一直是「票池为空」，完全不会交易。
# 补跑会占用共享线程跑全市场扫描（约 1~2 分钟），盘中跑之前请心里有数。
ALLOW_LATE_SCREEN = True


# --- 交易参数 ---
FIXED_SIZE = 500                  # 单笔买入股数

PRICE_ADD = 0.05                  # 买卖价格滑动

MAX_CASH = 100000.0               # 本策略总占用资金上限（持仓成本 + 在途买单）

CHECK_AVAILABLE_CASH = True       # 额外校验账户可用资金（避免资金不足被柜台拒单刷屏）

T1 = True                         # A 股现货：只卖可卖量（m_nCanUseVolume）


# --- 时段 ---
REFRESH_TIME = '09:15:00'         # 盘前刷新票池 / 布林上轨

MARKET_OPEN_TIME = '09:30:00'     # 连续竞价开始，之前不做买卖判定

TRADE_END_TIME = '14:57:00'       # 尾盘停止下单（留出撮合余量）


# --- 拉升检测 ---
SURGE_PCT = 0.03                  # 拉升阈值 3%

SURGE_WINDOW = 300                # 拉升窗口（秒）

MIN_SURGE_SPAN = 180              # 窗口至少覆盖 3 分钟才判定


# --- 止损 / 移动止盈（与原版同口径）---
STOP_LOSS_PCT = 0.02              # 常规档：相对开仓价亏 2%

WIDE_STOP_LOSS_PCT = 0.04         # 开盘宽档：亏 4%

WIDE_STOP_END_TIME = '10:00:00'   # 宽档截止

MAX_DRAWDOWN_PCT = 0.10           # 相对最大收益回撤 10% 卖出

TIER_HIGH_PCT = 0.10              # 最大收益 >= 10% → 保底 5%

TIER_HIGH_FLOOR = 0.05

TIER_LOW_PCT = 0.05               # 最大收益 >= 5% → 保底 2%

TIER_LOW_FLOOR = 0.02

TIER_BREAKEVEN_PCT = 0.03         # 最大收益 >= 3% → 不赔钱；亦作情绪卖出阈值

TIER_BREAKEVEN_FLOOR = 0.0

COOLDOWN_DAYS = 2                 # 亏损离场额外锁定的自然日数

HALF_PROFIT_PCT = 0.10            # 收益 10% 减半

CLEAR_PROFIT_PCT = 0.20           # 收益 20% 清仓


# --- 布林上轨止盈 ---
BOLL_WINDOW = 20

BOLL_DEV = 2.0

BOLL_MIN_PROFIT_PCT = 0.05        # 收益不足 5% 不按上轨卖

BOLL_LOOKBACK_DAYS = 60           # 取数回溯自然日

BOLL_DIVIDEND_TYPE = 'front'


# --- 委托监控 ---
SELL_ORDER_TIMEOUT = 300.0        # 卖单挂满 5 分钟未全成交则撤单（不重发）

PENDING_LOST_SECONDS = 60.0       # 委托超过该秒数仍无柜台委托号 → 标记异常


# --- 情绪 ---
SENTIMENT_INTERVAL = 30.0         # 全市场采样间隔（秒）

SENTIMENT_STALE_AFTER = 90.0      # 超过该秒数视为过期

SECTOR_PREFIX = 'SW2'             # 板块因子的板块名前缀（申万二级）

ENABLE_SECTOR_FACTOR = True       # 关掉则板块因子降级（大盘分按剩余权重归一）

MIN_SECTOR_VALID = 5              # 板块有效家数下限

INDEX_SYMBOLS = {                 # 指数因子用；QMT 全推含指数
    '000001.SH': '上证综指',
    '399001.SZ': '深证成指',
    '399006.SZ': '创业板指',
}

BREADTH_WEIGHT = 0.55

INDEX_WEIGHT = 0.30

SECTOR_WEIGHT = 0.15

CHANGE_SCALE = 2.0                # 涨跌幅 → 分数的缩放（±2% 映射到 0/100 附近）


# --- 日志 ---
VERBOSE_TICK = False              # True 时每轮主循环都打点，调试用
