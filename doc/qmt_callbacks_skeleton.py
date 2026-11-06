# -*- coding: utf-8 -*-
"""QMT 内置策略 —— 回调骨架

只有「框架会调用的回调」，没有任何业务逻辑。写新策略时抄这个文件。

★ 名字必须一字不差。QMT 是 `handlebar`，**不是** `handle_bar`
  （官方手册里 `handlebar` 出现 130 次，`handle_bar` 出现 0 次；
    名字写错框架不会报错，只是永远不调用——静默失效。）

★ 策略必须是**单文件 + 模块级函数**：客户端把全局变量/函数注入到「模块全局」这一层，
  所以不能用包、不能用 `from .x import`、不能把策略包在类实例里。

真实策略：qmt/near_ma_surge_qmt.py
业务/算法口径参考：doc/near_ma_surge_skeleton.py
"""

import datetime as dt
import time


# =============================================================================
# 一、客户端注入到模块全局的东西
# =============================================================================
#   全局变量
#       account          策略交易界面选定的资金账号
#       accountType      账号类型：'STOCK' / 'CREDIT' / 'FUTURE' / 'OPTION' ...
#   全局函数（直接调用，不要 import）
#       passorder(...)              下单
#       cancel(...)                 撤单
#       get_trade_detail_data(...)  查持仓/委托/成交/资金
#       get_value_by_order_id(...)  按委托号取单笔对象
#       get_last_order_id(...)      最新委托号
#
#   注意：这两个账号变量**不需要也不要**在代码里定义 / 赋默认值。
#         界面没选账号时注入的是空串，所以只需判空告警。


# =============================================================================
# 二、回调总览
# =============================================================================
#  ┌───────────────────────┬──────────────────────────────┬───────────┬──────────────────────┐
#  │ 名字                   │ 什么时候被调                  │ 必须吗     │ 本策略怎么用          │
#  ├───────────────────────┼──────────────────────────────┼───────────┼──────────────────────┤
#  │ init                  │ 整个策略开始时一次             │ ★必须      │ 绑定账号 + 注册定时器 │
#  │ after_init            │ init 之后、handlebar 之前      │ 否         │ 取交易日历            │
#  │ handlebar             │ 每根 K 线；实时状态下先逐根历  │ ★必须      │ **留空**              │
#  │                       │ 史 K 线，再每个 tick 驱动一次   │            │                      │
#  │ stop                  │ 策略停止前                     │ 否         │ 报告在途委托          │
#  │ on_timer（自定义名）   │ 定时器到点                     │ 否         │ main_loop，3 秒心跳   │
#  │ order_callback        │ 委托主推（仅实盘模式）          │ 否         │ 维护委托状态          │
#  │ deal_callback         │ 成交主推（仅实盘模式）          │ 否         │ 记开仓价 / 禁买冷却    │
#  │ orderError_callback   │ 下单错误                       │ 否         │ 标记失败              │
#  │ position_callback     │ 持仓主推（仅实盘模式）          │ 否         │ 留空（要用就现查）     │
#  │ account_callback      │ 资金主推（仅实盘模式）          │ 否         │ 留空（要用就现查）     │
#  │ task_callback         │ 账号任务状态变化主推            │ 否         │ 未用                 │
#  │ credit_account_callback  │ 两融账户明细（query_credit_account 的回调）│ 否 │ 未用          │
#  │ credit_opvolume_callback │ 两融最大可下单量回调          │ 否         │ 未用                 │
#  └───────────────────────┴──────────────────────────────┴───────────┴──────────────────────┘
#
#  手册原文（别自己猜）：
#    init      —— "初始化函数,只在整个策略开始时调用运行到一次,PY策略必须有该函数."
#    handlebar —— "行情事件函数,每根K线运行一次；实时行情获取状态下,先每根历史K线运行一次,
#                  再在每个TICK数据来后驱动运行一次,PY策略必须有该函数."
#    after_init—— "系统会在init函数执行完后和执行handlebar之前调用after_init,
#                  有些init里不支持的函数比如 ContextInfo.get_trading_dates 可以在 after_init 里调用"
#    stop      —— "PY策略模型关闭停止前运行到的函数 ... PY策略可不用该函数."
#
#  ⚠ 两个必须有的函数里，handlebar 是"必须存在"但不是"必须干活"：
#     定时器型策略里，handlebar 写成 `return` 就行。一旦在里面写逻辑，
#     它会被「每根历史 K 线 + 每个 tick」驱动 —— 等于把主循环从另一条路又跑一遍，委托翻倍。
#
#  ⚠ 实时回调只在**实盘交易模式**触发；模拟信号模式没有任何推送。


# =============================================================================
# 三、可直接复制的空壳
# =============================================================================

def init(C):
    """启动一次。"""
    # 界面没选账号 -> 注入空串
    if not account:
        print('警告：未取到 account（策略交易界面需选定账号），下单将失败')

    # ← 必须先调这个，实时回调（order/deal/position/account）才会生效
    C.set_account(account)

    # 注册定时器（见第四节）
    C.run_time('main_loop', '3nSecond', '2019-01-01 09:00:00')


def after_init(C):
    """init 之后、handlebar 之前。init 里取不到的数据在这里取。"""
    # 例：ContextInfo.get_trading_dates 在 init 里不可用
    # C.get_trading_dates('000001.SH', '20220101', today, -1, '1d')


def handlebar(C):
    """每根 K 线 + 每个 tick 都会被调 —— 定时器型策略必须留空。"""
    return


def stop(C):
    """停止前。此时交易连接已断：只能收尾、报告，不能报撤单。"""
    pass


def main_loop(C):
    """定时器回调（名字任意，只要和 run_time 里传的字符串一致）。
       签名固定：只接收 ContextInfo，无返回值。"""
    pass


def order_callback(C, orderInfo):
    """委托主推。字段：m_strOrderSysID / m_nVolumeTraded / m_nOrderStatus
                   / m_strRemark（== 你 passorder 时填的 userOrderId）
       撤单要用 m_strOrderSysID，所以必须在这里存下来。"""
    pass


def deal_callback(C, dealInfo):
    """成交主推。字段：m_strInstrumentID + '.' + m_strExchangeID 拼代码
                   / m_dPrice / m_nVolume / m_nOffsetFlag / m_nOpType"""
    pass


def orderError_callback(C, orderArgs, errMsg):
    """下单错误。部分错误不走 order_callback，必须有这个才不漏。"""
    pass


def position_callback(C, positionInfo):
    """持仓主推。建议留空：每次要用时现查 get_trade_detail_data，避免本地缓存与柜台不一致。"""
    pass


def account_callback(C, accountInfo):
    """资金主推。同上，留空。"""
    pass


def task_callback(C, taskInfo):
    """账号任务状态变化主推（未用）。"""
    pass


# =============================================================================
# 四、定时器：让代码周期跑（这是"回调"里唯一需要注册的）
# =============================================================================
#
# ── A. run_time —— 简单，不可取消 ────────────────────────────────────────────
#    ContextInfo.run_time(funcName, period, startTime)
#      funcName   回调函数名，**字符串**
#      period     重复间隔，'5nSecond' / '5nDay' / '500nMilliSecond'
#                 （周期单元只有 nMilliSecond、nSecond、Day 三种）
#      startTime  第一次启动时间；填历史时间 = 立刻启动
#    手册注意点：
#      · 模型回测时无效
#      · 定时器没有结束方法，随策略结束而结束
#      · "部分周期下定时器函数在第一次运行之前会先等待一个 period"
#        => '3nSecond' 的首轮可能启动后 3 秒才来，别依赖"立刻跑一次"
#
# ── B. schedule_run —— 可取消、可指定重复次数 ───────────────────────────────
#    ContextInfo.schedule_run(func, time_point, repeat_times=0, interval=None, name='')
#      func          回调函数（对象，不是字符串），签名 def on_timer(C): ...
#      time_point    第一次触发时间；str 格式 'yyyymmddHHMMSS'（如 '20231231235959'）；
#                    已过预定时间 => 立即执行
#      repeat_times  预定时间触发后再按 interval 重复多少次；0=不重复；-1=一直重复
#      interval      datetime.timedelta 实例，如 dt.timedelta(seconds=30)
#      name          任务组名，便于整组取消
#      返回 seq      本次任务号，可用于取消
#
#    ContextInfo.cancel_schedule_run(key)     key = seq(int) 或 name(str)
#
# ── 本策略的选择 ───────────────────────────────────────────────────────────
#    用一个 3 秒心跳 + 内部节流（间隔判断），而不是注册多个定时器：
#      所有定时器与策略共用同一个 Python 线程，注册多个并不能并行，
#      只是让"谁在什么时候跑"更难推理；一个心跳 + 节流，执行顺序完全确定。
#
#    C.run_time('main_loop', '3nSecond', '2019-01-01 09:00:00')
#
#    def main_loop(C):
#        ... 日切
#        ... 选股推进
#        ... 情绪采样（内部节流：满 30 秒才采）
#        ... 盘前刷新 / 对账 / 取价 / 卖出链 / 买入链
#
#    情绪那两个"周期条件"是分开的：
#      · 采样间隔 30 秒  -> 心跳里用 `now - last_ts < 30` 节流
#      · 每天首次要重建基础数据（全市场代码表 / ST / 131 板块成员缓存）
#        -> 心跳里用 `if G.sent_date != G.day` 挡住，保证一天只做一次
#
#    如果哪天想改成"真定时器"，用 B 就行（30 秒一次、可取消）：
#
#      def on_sentiment(C):
#          sample_sentiment(C)
#
#      def init(C):
#          C.schedule_run(on_sentiment, '20261014' + '091500', -1,
#                         dt.timedelta(seconds=30), 'sentiment')
#
#      def stop(C):
#          C.cancel_schedule_run('sentiment')


# =============================================================================
# 五、名字易错点（写错了都是"静默不生效"，不报错）
# =============================================================================
#   handlebar            不是 handle_bar / HandleBar / handleBar
#   orderError_callback  注意中间那个大写 E，不是 order_error_callback
#   after_init           不是 afterInit
#   run_time             period 是 '3nSecond' 这种带 n 的写法，不是 '3s' / 3（数字）
#   run_time             funcName 传的是**字符串**，不是函数对象
#                         （schedule_run 的 func 才是传函数对象——两个 API 不一致，容易混）
#   全局函数             直接调用 passorder / cancel / get_trade_detail_data，不要 import
