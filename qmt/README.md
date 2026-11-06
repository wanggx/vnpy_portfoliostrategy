# 近均线快速拉升策略 —— QMT 内置策略版

外接（miniQMT / xtquant RPC 桥）被封禁后，把 `vnpy_portfoliostrategy` 里的
`NearMaSurgeStrategy` 整体搬进 **QMT 客户端内置策略**，只用内置 API，
**零文件 IO、零网络、Python 3.6**。

**单文件交付**：QMT 只能往策略编辑器里粘一个文件，所以整个策略就是
`qmt/near_ma_surge_qmt.py` —— 9 个模块（config / sectors / state / screen /
sentiment / market / signals / trade / entry）已合成在这一份里，**它本身就是源码**，
改代码直接改它。

## 文件

| 文件 | 作用 |
|---|---|
| `near_ma_surge_qmt.py` | **唯一文件**：整个策略（9 个模块已合成）。**纯 ASCII**，全文粘进 QMT 即可 |
| `README.md` | 本文档 |

> 曾经是「`src/nearma/` 多文件源码 + `build.py` 合成 + `test_sim.py` 离线仿真 +
> `check_py36.py` 语法闸门」，已按「只留交付件」的方式移除。
> 需要找回：`qmt-src-backup-20261014.zip`（仓库根目录，确认不再需要可删）。

> **为什么要纯 ASCII（踩过的坑）**：国金 QMT 客户端把模型加密落盘，exec 源码时按
> `#coding:gbk` 解码。实测粘贴带中文的 **GBK** 交付件报
> `'gbk' codec can't decode byte 0xfe in position 52635` —— GBK 里 `0xFE` 是合法**尾**字节，
> 客户端的粘贴/再编码链路一旦错位就把它当成非法**前导**字节。纯 ASCII 源码在任何编码下
> 都是同一串字节（文件里没有任何 `>= 0x80` 的字节），从根上消掉这类问题。
> 中文日志字符串在文件里是 `\uXXXX` 转义，**运行时照样打印中文**。
> 因此**改代码时不要引入非 ASCII 字符**，中文一律写成 `\uXXXX`。

## 维护（单文件模式）

1. 在 QMT 编辑器里直接改，或在本地改 `qmt/near_ma_surge_qmt.py` 后**全文重新粘贴**。
2. 改完看 `init` 的日志行 `策略初始化完成 build=<BUILD_ID> ...`，确认跑的是改动后的副本。
3. 约束（写错了 QMT 里是静默异常，很难查）：

- **Python 3.6 语法**：f-string 可用，但**不能**用 walrus（`:=`）、`list[str]` /
  `str | None`、`dataclass`、`match`，也不能写 `from __future__ import annotations`。
- **纯 ASCII**：中文只写 `\uXXXX`，不要引入 `>= 0x80` 的字节。
- **状态只能放 `G`**：QMT 的 `ContextInfo` 有逐 K 线深拷贝回滚，存进去会丢。
- **时钟统一走 `_clock()` / `_now_ts()`**，不要直接调 `time.strftime`。
- **顶层函数/变量不能重名**：单文件里同名是静默覆盖，不报错。
- **参数集中在文件开头的 `config` 段**（`UNIVERSE_SECTOR` / `MAX_CASH` / `FIXED_SIZE` 等）。

## 在 QMT 里跑起来（首次部署）

### 本机实况（已核实，2026-10-14）

| 项 | 值 |
|---|---|
| QMT 客户端 | `D:\国金QMT交易端模拟\bin.x64\XtItClient.exe` ← **内置策略跑在这个里** |
| miniQMT（外接，已封禁） | `D:\国金QMT交易端模拟\bin.x64\XtMiniQmt.exe` |
| 内嵌 Python | **3.6**（`bin.x64\python36.dll`）—— 本策略的 py3.6 降级是必需的，不是保守 |
| 策略/模型目录 | `D:\国金QMT交易端模拟\python\`（编辑器保存的模型都在这里） |
| 申万二级板块 | 共 132 个，名称为 `SW2<行业名>`；已核实存在 `SW2半导体`、`SW2贵金属`、`SW2小金属`、`SW2汽车零部件` |
| 板块清单文件 | `<QMT>\datadir\Sector\申万二级行业板块\sectorConfig.xml` |

### 本机实测到的 API 差异（代码已适配）

| # | 实测 | 影响 / 处理 |
|---|---|---|
| 1 | **没有 `ContextInfo.get_sector_list`** | 拿不到板块清单 → 131 个申万二级板块名改为**内置在交付件里**（从客户端 `sectorConfig.xml` 生成）；`LOAD_ALL_SECTORS = True` 时逐个取成分股，建「股票→行业」映射（实测 131 个板块耗时 0.01s），行业准入据此判定 |
| 2 | `get_trade_detail_data` 的 datatype **只认大写**（`"POSITION"`/`"ORDER"`/`"DEAL"`），手册写的小写会报错 | `trade._query` 两种都试，取到非空为止 |
| 3 | `cancel` 是 **4 参**：`cancel(委托号, 账号, 账号类型, ContextInfo)` | 已传 `ContextInfo`（少了它撤单必失败） |
| 4 | `passorder(opType, 1101, 账号, 代码, prType, 价格, 数量, strategyName, 2, userOrderId, C)` | 与本实现一致（对照本机 `bigqmt_signal_trader/adapters/order_bigqmt.py`） |
| 5 | `passorder`/`cancel`/`get_trade_detail_data` 由客户端**注入到策略模块的全局命名空间** | 所以策略必须是一个文件里的模块级函数—— 这也是「源码多文件、交付单文件」的原因 |
| 6 | 模型**加密落盘**，exec 源码时按 `#coding:gbk` 解码 | 带中文的 GBK 文件报 `can't decode byte 0xfe` → 交付件改成**纯 ASCII**（见上文） |

### 步骤

1. **参数预检**（参数都在 `near_ma_surge_qmt.py` 开头的 config 段，改完要重新粘贴）：
   - 标的池模式（当前为**全市场**）：
     - 全市场（沪深A股）：`INDUSTRY_SECTOR = ''` + `EXTRA_SECTOR_NAMES = ()` + `LOAD_ALL_SECTORS = True`
       —— 行业按其所属申万二级行业逐个判定
     - 单行业：`INDUSTRY_SECTOR = 'SW2半导体'`
     - 多行业：`EXTRA_SECTOR_NAMES = ('SW2半导体', 'SW2贵金属', 'SW2小金属')`
   - 核对 `MAX_CASH`（策略总占用上限）、`FIXED_SIZE`（单笔股数）
2. **补历史日线**：客户端「数据管理」下载日线，覆盖 2022-04-27 至今（选股要 5 条成本均线）
3. **交付件**：`qmt/near_ma_surge_qmt.py`（纯 ASCII，**不需要**再转 GBK）
4. **在客户端里跑**：
   - 启动 `D:\国金QMT交易端模拟\bin.x64\XtItClient.exe` 并登录
   - 打开内置策略编辑器（模型/策略 → Python 策略 → **新建**）
     - 上次编译失败的 `NEARMA` 模型先删掉重建，避免残留内容干扰
   - 主图周期选 **日线**（策略不依赖主图，但必须绑一个代码）
   - 把 `near_ma_surge_qmt.py` 的**全部内容**粘进去（约 2266 行 / 73 KB）
   - 编译 → 先选「**模拟信号模式**」运行
   - 选交易账号（普通 `STOCK`／两融 `CREDIT`；模拟端用 `STOCK`）
5. **核对日志**（关键词见「排障」）：
   - `策略初始化完成 build=xxxxxxxx …` ← 即文件内 `BUILD_ID` 的值；**改完本地文件
     必须重新全文粘贴**，否则 QMT 里跑的还是旧副本（日志里 `build=` 不变即证明没生效）
   - `交易日历 N 个，今天 ... 是交易日`
   - `板块成员缓存：1 个板块，覆盖率 N 只标的` ← 若打「警告：一个板块成员都没取到」说明板块名写错
   - `选股完成：N 只入池` ← 若 `选股结果为空` 且日志大量「日线不足剔除」，就是第 2 步没补数据
6. **跑通一天后**切「实盘交易模式」（`order_callback` / `deal_callback` 只在实盘模式触发）

> 只想先验证板块名对不对：同一客户端新建一个最小模型，跑
> `print(ContextInfo.get_stock_list_in_sector('SW2半导体'))`，能打印出代码列表即可。

## 上线前剩下的待确认项

以下只能靠真机日志确认：

1. **标的池模式**：现在跑的是**全市场（沪深A股）**——`INDUSTRY_SECTOR` / `EXTRA_SECTOR_NAMES`
   都为空 + `LOAD_ALL_SECTORS = True`，行业按其所属申万二级行业逐个判定（映射由文件内置的
   131 个板块名建）。要改成单行业就填 `INDUSTRY_SECTOR`。注意：如果
   `LOAD_ALL_SECTORS` 也关掉且不填任何板块，行业准入会拦截全部买入（日志会打警告）。
2. **历史日线是否齐**：选股要读 2022-04-27 以来的前复权日线（约 1160 根）。
   先在「数据管理」补全日线，否则选股会大面积因 `MIN_BARS` 不足被剔除。
3. **全市场取数耗时**：`C.get_full_tick(全市场代码)` 每 30 秒一次（情绪）+
   每 3 秒一次（票池约 100 只）。看盘前日志里「板块成员缓存」与情绪首次采样耗时，
   如果全市场那一步太慢，把 `ENABLE_SECTOR_FACTOR = False` 并考虑把
   `SENTIMENT_INTERVAL` 调到 60。
4. **选股分片是否跑得完**：`SCREEN_BATCH`（默认 200）决定每轮处理多少只；
   若盘前 `08:40 ~ 09:20` 跑不完（日志里 `screen` 进度停在中间），
   调大 `SCREEN_BATCH` 或把 `SCREEN_START` 提前。
5. **本机是模拟端**：`D:\国金QMT交易端模拟` 是模拟交易端（行情/撮合都是模拟的），
   先在这里跑通流程；上实盘要另接实盘客户端，参数一致但务必按真实资金调小 `MAX_CASH`。

## 参数速查

### 账户与标的池
| 参数 | 默认 | 说明 |
|---|---|---|
| `account` / `accountType` | 界面注入 | 客户端按「策略交易界面选定的账号」注入的**全局变量**，代码里**直接引用**，不要自己定义同名变量；策略界面上必须选定账号 |
| `UNIVERSE_SECTOR` | `沪深A股` | 选股/情绪的全市场板块名 |
| `INDUSTRY_SECTOR` | `''` | 非空则只在该申万板块内选股（单行业模式）；留空 = 全市场，行业按其所属申万二级行业逐个判定 |
| `EXTRA_SECTOR_NAMES` | `()` | 多行业模式：与 `INDUSTRY_SECTOR` 取并集作为选股池 |
| `LOAD_ALL_SECTORS` | `True` | 全市场模式（上面两个都为空）时加载全部申万二级板块，建「股票→行业」映射；代价是每天首刷多 131 次 `get_stock_list_in_sector`（约 1~3 秒） |
| `EXCLUDE_CODE_PREFIXES` | `688/689` | 不做科创板 |
| `ST_SECTOR_NAMES` | `风险警示`、`ST板块` | 优先用板块差集排 ST，取不到则按名称兜底 |

### 选股（口径与原 `select_near_ma_xtquant.py` 一致）
| 参数 | 默认 | 说明 |
|---|---|---|
| `FIXED_DATES` | 5 个固定日期 | 每个日期到 T 的前复权均价 = 一条成本均线 |
| `MIN_BARS` / `MIN_WINDOW_POINTS` | 100 / 5 | 日线不足剔除；单条均线有效点下限 |
| `NEAR_THRESHOLD` | 0.01 | 收盘价**或**最低价落在均线 ±1% 即算靠近 |
| `BREAK_LOOKBACK` / `BREAK_THRESHOLD` | 20 / 0.03 | 近 20 日跌穿 3% 则不算靠近 |
| `REQUIRE_HALVED` / `HALF_RATIO` / `HIGH_LOOKBACK` | True / 0.5 / 252 | 要求相对近 252 日最高价腰斩 |
| `WEIGHT_MODE` / `TOP_N` | `days` / 100 | 天数即权重；取前 100 只 |
| `SCREEN_BATCH` / `SCREEN_START` / `SCREEN_DEADLINE` | 200 / 08:40 / 09:20 | 分片跑，避免阻塞共享线程 |

### 交易
| 参数 | 默认 | 说明 |
|---|---|---|
| `FIXED_SIZE` / `PRICE_ADD` | 500 / 0.05 | 单笔股数；买卖价格滑动 |
| `MAX_CASH` | 100000 | 策略总占用上限（持仓成本 + 在途买单） |
| `CHECK_AVAILABLE_CASH` | True | 另加账户可用资金校验，避免柜台拒单刷屏 |
| `SURGE_PCT` / `SURGE_WINDOW` / `MIN_SURGE_SPAN` | 0.03 / 300 / 180 | 窗口涨幅或昨收涨幅 ≥3% |
| `STOP_LOSS_PCT` / `WIDE_STOP_LOSS_PCT` / `WIDE_STOP_END_TIME` | 0.02 / 0.04 / 10:00 | 10:00 前宽档，之后常规档 |
| `HALF_PROFIT_PCT` / `CLEAR_PROFIT_PCT` | 0.10 / 0.20 | 减半（+100 股向上取整）／清仓 |
| `MAX_DRAWDOWN_PCT` | 0.10 | 相对最大收益回撤 |
| `TIER_*` | 10%→保底 5%、5%→2%、3%→0% | 保底收益线；3% 档同时是情绪离场阈值 |
| `COOLDOWN_DAYS` | 2 | 亏损离场额外锁定的自然日数（当天一律禁买） |
| `BOLL_*` | 20 / 2.0 / 5% / 60 天 / 前复权 | 布林上轨兜底止盈 |
| `SELL_ORDER_TIMEOUT` | 300 | 卖单挂满 5 分钟未全成交则撤单（不重发） |

### 情绪
| 参数 | 默认 | 说明 |
|---|---|---|
| `SENTIMENT_INTERVAL` / `SENTIMENT_STALE_AFTER` | 30 / 90 | 采样间隔与过期阈值 |
| `BREADTH_/INDEX_/SECTOR_WEIGHT` | 0.55 / 0.30 / 0.15 | 三项因子权重（缺项按可用权重归一） |
| `MIN_SECTOR_VALID` | 5 | 板块有效家数下限 |
| `SECTOR_PREFIX` / `ENABLE_SECTOR_FACTOR` | `SW2` / True | 板块清单前缀；关掉则板块因子降级 |

分档口径（与原 `vnpy_marketsentiment` 一致）：`<20 极弱 / <45 偏弱 / <60 中性 / <80 偏强 / >=80 极强`。

## 与原版的行为差异

1. **状态不落盘**（禁 IO）：策略重启后 `entry_prices` 改用券商成本价 `m_dOpenPrice` 兜底、
   `max_profit_pct` 从 0 重算、`cooldown_dates` 全部丢失。日内重启会放宽保护，
   **重启后请人工确认持仓标的的止盈基准**。
2. **不能回测**：`run_time` 与 `get_full_tick` 在回测模式都无效；只能用模拟信号模式验证。
3. **T+1 簿记整块删除**：改用 `get_trade_detail_data('position')` 的 `m_nCanUseVolume`，
   锚点为券商实时数据；"本地有、经纪商没报"的幽灵仓问题从根上不存在（不再有本地缓存）。
4. **ST 排除口径**：优先板块差集，其次对候选（`TOP_N + 20`）查名称；与原版"先剔 ST 再打分"
   相比，极端情况下排名会差一位。
5. **接管账户持仓**：卖出链按**账户**持仓执行——票池内被手工买入或其它策略持有的标的
   也会被本策略卖出（与 vnpy 版行为一致）。**建议为策略分配独立账户或独立客户端**，
   否则多策略共用一个账户会互相干扰。

## 排障

| 日志关键词 | 含义 / 处置 |
|---|---|
| `NameError: name 'account' is not defined` | 客户端**没注入账号**：策略交易界面上没选账号（或账号未加载完）。选好账号再启动策略 |
| `选股启动失败：板块“x”无成分股` | 板块名不对，核对 `UNIVERSE_SECTOR` / `INDUSTRY_SECTOR` |
| `选股结果为空` | 日线不全或当天没有符合"贴均线 + 腰斩"的标的；后者属正常 |
| `未取到 ST 板块，改用名称兜底排除` | 正常降级；只需确认 QMT 有 `风险警示` 板块 |
| `大盘情绪拦截 … 大盘中性以下不买入任何标的` | 策略设计如此（大盘偏弱/极弱一票否决） |
| `行业情绪拦截 … 未达准入，不买入` | 该标的行业评级不达标；当日该标的不再重试买入 |
| `资金不足，跳过买入` | 触到 `MAX_CASH` 或账户可用资金不足 |
| `订阅 x 失败（已订阅 n 只）` | 非 VIP 订阅数超限；**不影响交易**，取价走每轮批量拉 |
| `卖出委托超时告警 … 撤单（不重发）` | 挂满 5 分钟未全成交，已撤；条件仍成立下一轮会重新下单 |
| `超过 60 秒未见柜台委托号` | 委托可能未被柜台受理，需人工核对 |
| `策略停止：仍有 n 笔在途委托` | 停止时未终结的委托，需人工处理 |

## 真机验证清单（离线仿真已移除）

曾经用 `test_sim.py`（桩掉 QMT 接口 + 虚拟时钟，58 条断言）验证过：选股（贴均线/腰斩/打分/
入池）、情绪（大盘分按公式精确校验 76.09、评级、行业准入）、买入（窗口拉升 4%、价格与数量、
`quickTrade=2`、不重复下单、tick 回调登记）、卖出（券商成本价兜底、宽档 4% 止损、亏损冷却
登记与 `COOLDOWN_DAYS` 后释放、清仓清理）、分档边界（减半向上取整 300 股、取整等于全量时
跳过、保底线严格小于、回撤、常规档、布林兜底）、环境适配（datatype 只认大写时的回退、
卖单超时撤单且 `cancel` 带 4 参、多板块并集）。

**此保护已随 `test_sim.py` 一起移除**，以下只能靠真机日志确认：
QMT 回调时序、真实 `passorder` 回报、`m_*` 字段名差异、全市场取数耗时、板块名有效性。
