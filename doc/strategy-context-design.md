# StrategyContext 设计（策略运行上下文）

## 1. 背景与目标

`StrategyTemplate`（`vnpy_portfoliostrategy/template.py`）是所有组合策略的基类，目前只有
`parameters` / `variables` 两个列表：`parameters` 用于界面可配置参数，`variables` 用于界面
展示 + 持久化（`portfolio_strategy_data.json`）。除此之外的策略状态——持仓、订单、T+1 昨今仓、
以及各策略自有的独特变量——都散落在策略实例的普通 `self.xxx` 属性里：

- 持仓信息：`pos_data`（持久化变量）+ `yd_pos_data` / `td_pos_data` / `sell_frozen_data`
  （运行期，重启由经纪商持仓回报重建）。
- 订单信息：`orders` / `active_orderids`（运行期缓存），权威数据在 `MainEngine`。
- 资金信息：**目前没有**，账户数据只在 `MainEngine.get_all_accounts()`（全局）；策略侧
  要么不查，要么像 `near_ma_surge_strategy._deployed_cash()` 那样按需现算。

目标是收敛「策略全局运行状态」到一个对象 `StrategyContext`：

1. **资金/账户信息进基类**：`balance / available / frozen` 等直接作为 `context` 一等字段，
   界面可显示、可维护。
2. **通用状态统一访问**：订单、持仓沿用现状（已在 `StrategyTemplate`），`context` 提供
   只读聚合视图，后续信号统一 `strategy.context.xxx` 取全局信息。
3. **策略独特变量走 `extra_data`**：各策略把独有的状态放进 `context.extra_data`（dict，
   结构不限），不再新增散落的 `self.xxx`。

## 2. StrategyContext 结构

新类 `StrategyContext`，放 `vnpy_portfoliostrategy/template.py`（或独立 `context.py`，
最终实现时定）。**作为 `StrategyTemplate` 的属性存在**：每个策略实例持有
`self.context = StrategyContext(self)`，信号/策略逻辑都经它访问全局状态。

| 字段 | 类型 | 来源 | 持久化 |
|---|---|---|---|
| `account_id` | str | 账户事件 `AccountData.accountid` | ❌ 重启重建 |
| `gateway_name` | str | 账户事件 `AccountData.gateway_name` | ❌ 重启重建 |
| `balance` | float | 账户事件 `AccountData.balance`（总资产/权益） | ❌ 重启重建 |
| `available` | float | 账户事件 `AccountData.available`（可用资金） | ❌ 重启重建 |
| `frozen` | float | 账户事件 `AccountData.frozen`（冻结资金） | ❌ 重启重建 |
| `market_value` | float | 派生：Σ 持仓 × 最新价 | ❌ 按需现算 |
| `deployed_cash` | float | 派生：持仓成本 + 在途买单 | ❌ 按需现算/事件维护 |
| `pnl` | float | 派生：市值 − 成本 | ❌ 按需现算 |
| `extra_data` | dict | 各策略自己赋值（任意结构） | ✅ 随策略落盘（仅 JSON-safe 部分） |

反向引用：`context.strategy`，用于只读聚合 `pos_data / orders / active_orderids / sellable`。

序列化：`to_dict()` / `update_from_dict()`。`extra_data` 中 JSON-safe 的值落盘；非 JSON-safe
（`set` / `deque` / 自定义对象）标记为运行时，重启后在 `on_init` 重建。

### 资金口径说明

`AccountData` 在 vnpy_xt 网关里被显式覆盖：

```python
balance=xt_asset.total_asset    # 总资产（含持仓市值）
frozen=xt_asset.frozen_cash     # 冻结资金
account.available = xt_asset.cash  # 可用资金（不含持仓市值涨跌）
```

因此 `balance / available / frozen` 直接来自账户事件，含义与用户认知一致：
`available` 是「可用资金」，不受持仓市值涨跌影响。

## 3. 与 parameters / variables 的关系

- `parameters` 不变：可配置参数，界面可编辑，存 `portfolio_strategy_setting.json`。
- `variables` 保持「界面展示 + 持久化」机制不变，本轮**不迁移** `pos_data` 等字段。
- `context` 在界面上新增一块「资金」展示区（`balance / available / frozen /
  deployed_cash / market_value / pnl`），`extra_data` 作为独立块以 key-value / JSON 展示。
- 持久化扩展一处：`sync_strategy_data` 落盘时把 `context.extra_data` 一起保存；重启
  `_init_strategy` 恢复进 `context.extra_data`。`balance / available / frozen /
  deployed_cash` 与 T+1 字段一样**不落盘**，重启重建。

### 同步规则（重要）

`StrategyContext` 是**运行态的唯一来源**；`parameters` / `variables` 是**界面投影**，两者必须
受控同步，避免出现两份副本漂移：

- **parameters → context（只读透传，不复制）**：参数仍存在策略实例（`self.xxx`，UI 经
  `update_setting` 修改），`context` 以只读属性透传参数，**不另存副本**。UI 改参数后信号
  立即读到新值，不会出现「改了界面、策略还在用旧值」。
- **variables ← context（展示从 context 读）**：`get_variables()` 把需要展示的 context
  字段（资金、`extra_data` 的 JSON-safe 部分）**并进返回结果**，每次 `put_event` 刷新；
  `variables` 不再为这些值另存一份。
- **派生字段刷新时机**：`market_value / deployed_cash / pnl` 等派生值在 `put_event` /
  `sync_data` 前统一 `context.refresh()` 现算一次，保证界面显示与运行态一致。
- **持久化边界**：`extra_data` 的 JSON-safe 部分随 `variables` 落盘/恢复；运行期结构
  （set/deque/对象）不进持久化，重启由 `on_init` 重建。

## 4. 策略入口文件瘦身（信号化模式）

有了 `StrategyContext` 统一承载全局状态后，策略入口文件只保留「协调者」职责，不再堆业务逻辑：

```python
class XxxStrategy(StrategyTemplate):
    parameters = [...]
    variables = [...]

    def on_init(self):
        self.buy_signal = BuyAggregator(self)
        self.sell_signal = SellAggregator(self)

    def on_tick(self, tick):
        # 只做：日切刷新 → 分发 tick → 取 SignalResult → 下单 → 通知
        self.buy_signal.on_tick(tick)
        result = self.buy_signal.signal_result(tick.vt_symbol)
        if result.type == SignalType.BUY:
            self.buy(...)
```

- 判定逻辑（拉升/情绪/止损/止盈/布林）全部下沉到 signal 子信号；子信号经
  `self.strategy.context` 取持仓/资金/独特变量，不再直连策略私有字段。
- 入口文件只保留：参数/变量声明、`on_init` 组装信号链、`on_tick` 编排下单与通知、
  需要跨信号共享的状态读写（经 `context`）。
- 好处：新策略 = 「声明参数 + 选信号链 + 编排下单」，业务规则在 signal 里可复用/可单测。

## 5. 资金同步链路（引擎侧）

最小改动，在 `vnpy_portfoliostrategy/engine.py`：

1. `register_event` 增加 `EVENT_ACCOUNT` 注册。
2. 新增 `process_account_event`：按 `account.gateway_name` 找到「订阅了该网关标的」的策略，
   把 `balance / available / frozen` 写入其 `context`。
3. `_init_strategy` 末尾从 `main_engine.get_all_accounts()` 兜底同步一次（等价现有
   `init_t1_position` 的思路）。

`deployed_cash` 不建全局账本，保留「按需现算」：把现有
`near_ma_surge_strategy._deployed_cash()` 提为 `context` 或基类方法
`calc_deployed_cash()`，买入判定前刷新进 `context.deployed_cash` 供展示与复用。

## 6. extra_data 使用约定

策略独特变量从散落的 `self.xxx` 迁入 `context.extra_data`：

```python
context.extra_data = {
    "entered": set(),            # 运行期集合（非 JSON-safe，不落盘）
    "entry_prices": {},          # JSON-safe，落盘
    "max_profit_pct": {},        # JSON-safe，落盘
    "cooldown_dates": {},        # JSON-safe，落盘
    # ...
}
```

规则：**JSON-safe 的进 `extra_data` 并落盘；`set / deque / 对象` 这类运行期结构也放
`extra_data`，但标注不落盘**，重启后由策略在 `on_init` 重建。所有「策略私有状态」统一
归口到 `extra_data`，不再新增 `self.xxx`。

## 7. 信号访问方式

信号子类一般**接收 `StrategyContext`**（也可接收 `StrategyTemplate`，两者皆可）：

```python
class SomeSubSignal(SubSignal):
    def __init__(self, vt_symbol: str, context: StrategyContext) -> None:
        super().__init__(vt_symbol, context)
        self.context = context      # 子信号持有 context，不再直连 strategy 私有字段

    def on_tick(self, tick, prev):
        self.context.available              # 可用资金
        self.context.deployed_cash          # 已占用资金
        self.context.pos                    # 持仓（只读聚合）
        self.context.extra_data["entered"]  # 策略独特变量
```

- 工厂签名由 `Callable[[str, StrategyTemplate], SubSignal]` 改为
  `Callable[[str, StrategyContext], SubSignal]`；`SignalAggregator` 构造时把
  `strategy.context` 传给工厂，需要策略级能力（`write_log` / `send_wecom` / 下单）时
  经 `context.strategy` 反向引用。
- 兼容性：子信号 `__init__` 同时接受 `StrategyContext` 或 `StrategyTemplate`
  （内部 `context = strategy.context if isinstance(strategy, StrategyTemplate)
  else strategy`），老信号无需改动。

## 8. 兼容性

- 老策略不引用 `context`，照常运行；`context` 是新增对象，不影响
  `pos_data / orders / active_orderids` 现状。
- `pos_data / orders / active_orderids` 本轮**不迁移**进 context（避免动引擎/UI/持久化），
  只加资金字段 + `extra_data` + 只读聚合。

## 9. 待确认决策

1. **范围**：本轮只加「资金字段 + extra_data + 只读聚合」，`pos_data / orders /
   active_orderids` 继续留在 `StrategyTemplate`，不迁入 context —— 默认按此执行。
2. **extra_data 持久化边界**：只落 JSON-safe 的值、非 JSON-safe 的当运行时重建 ——
   默认按此执行（更宽松，策略可自由放任意结构）。
3. **资金字段展示**：`balance / available / frozen / deployed_cash` 是否也进 `variables`
   （策略卡片直接显示），还是只在 `context` 的「资金」栏展示 —— 待定。
4. **deployed_cash**：保持「按需现算」（简单、无账本漂移）还是「下单/成交/撤单事件维护
   计数器」（实时精确但多一条状态流）—— 待定。

确认以上决策后，再开始实现代码。
