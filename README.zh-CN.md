# Entropy 价差触发与定时平仓

策略只连接 Entropy（Hyperliquid `io` dex），不再连接 Lighter RH，也不执行 LEG1/LEG3 虚拟单。每轮以相同概率随机多空，价差满足条件后执行 LEG2 市价开仓，再由 LEG4 reduce-only 市价平仓。

`价差 bps = (卖一 - 买一) / ((卖一 + 买一) / 2) × 10000`，默认不超过 **2 bps（0.02%）**，等于阈值也允许。行情过期、无效或盘口交叉时不开仓；每次开仓及重试前重新检查价差，平仓不受价差阈值限制。

**50ms 从实际成交的 LEG2 尝试发起时计时。** 成交确认在 50ms 内到达则等待到点；超过 50ms 则确认后立即平仓，不再额外等待 50ms。未确认成交数量时不会猜数量平仓。正常路径两腿之间没有额外 REST 仓位查询；每次 LEG4 后核对剩余仓位，下一轮开仓前确认空仓。网络、限频、行情过期、持久化和调度可能使实际发送晚于目标，这不是交易所成交时间保证。

保留独立滑点上限和带保护价的 IOC 市价执行。同一腿重试沿用首次参考价，下一条腿重新取价。LEG2 部分成交后只平实际数量，不补仓；LEG4 部分成交后只重试剩余数量。开仓为平仓预留一个下单额度，`entropy.max_orders_per_min` 至少为 2。

## 配置

修改 `config.yaml` 后重启生效：

```yaml
cycle:
  direction: random
  cycles: 0
  max_spread_bps: 2.0
  close_delay_ms: 50.0
sizing:
  quantity: 0
  order_notional_usd: 50
execution:
  leg2_slippage_bps: 20
  leg4_slippage_bps: 20
  cooldown_sec: 2
```


## 安装与运行

Python 3.10+，安装 `requirements-live.txt`，将 `config.example.yaml` 复制为 `config.yaml`，`.env.example` 复制为 `.env`。使用 API agent 时，`HL_PRIVATE_KEY` 填 API 钱包私钥，`HL_ACCOUNT_ADDRESS` 填有资金的主账户地址。`--symbol` 只需 Entropy 支持。

```bash
python main.py --symbol SNDK --record-only --cn  # 仅采集，不下单
python main.py --symbol SNDK --cn               # 实盘
python main.py --symbol SNDK --no-dashboard     # 纯日志
```

方向固定为 `random`。旧 `virtual_depth`、`virtual_requote_sec`、`max_hold_sec` 暂时接受但提示已忽略，请删除。旧 `--hedge lighter-rh` 仅兼容参数，不建立连接。旧 `execution.leg_slippage_bps` 仍为未单独设置的腿提供滑点默认值。REST 与 WebSocket 支持环境代理。

## 停止与恢复

启动要求所选市场空仓且无已有挂单，同一账户/品种仅运行一个实例并避免同时手工交易。订单结果不明时按唯一 `cloid` 查询；无法确认则停机，不盲目重发。发单前保存 `logs/cycle.json` 检查点，未完成检查点阻止直接重启；核实终态、处理残仓后再归档。状态文件具有进程锁。

Ctrl+C / SIGTERM 在等待价差时直接停止；已开仓时中断剩余计时并尝试平仓。未知订单、仓位不一致或平仓重试耗尽时保留未完成状态并报错。强制结束进程无法平仓。

`logs/legs.csv` 只新增 LEG2/LEG4 实际订单，`limit_px` 是滑点保护价。`logs/minutes.csv` 保留原表头，只采集 Entropy 买一卖一，RH/跨所溢价列留空。`tools/analyze.py` 统计分钟末 Entropy 买卖价差，不推算策略盈亏。

## LEG2 / LEG4 成交额统计

仪表盘显示 `本次成交额 | LEG2 $… | LEG4 $… | 合计 $…`，最近执行列表显示每笔真实订单的成交额。每次实盘订单确认后、周期状态日志以及程序退出时都会输出累计值。

统计公式为 `实际成交数量 × 实际成交均价`，LEG2 和 LEG4 的金额相加，开仓和平仓都计入；多空方向不影响金额的正负。部分成交与多次平仓分别累计已成交部分，撤单未成交量不计入，不扣手续费或资金费，也不表示盈亏。例如开仓成交 $50、平仓成交 $51，合计为 $101。

统计范围是**本次进程启动以来**，跨轮累加，重启清零；状态检查点保留本次统计快照，`logs/legs.csv` 继续保存实际成交数量与均价供事后核对。通过订单状态补确认的成交会尝试读取真实成交明细计算均价；均价仍缺失时，面板标记“合计不完整”并列出缺少均价的已成交数量，不使用挂价或滑点保护价代替成交价。

## 余额与本次策略净盈亏

仪表盘显示 Entropy 余额、权益、可提取金额，以及本次运行的策略总净盈亏：`已实现盈亏 - 实际手续费 + 资金费收支 + 当前持仓浮盈亏`。资金费收入为正、支出为负。盈亏跨轮次累计，重启归零；只统计本程序 LEG2/LEG4 订单及当前品种，转入转出不计入盈亏。手续费取实际成交记录（已含 builder fee），不使用配置费率估算。

普通账户显示 `io USDC` 保证金余额；统一账户/组合保证金账户显示 `shared USDC` 共享 USDC 余额，其他抵押资产不折算入该值，也不把现货 hold 推算为可提取金额。余额通过实时账户接口读取，浮盈亏取所选 Entropy 仓位。

`logging.account_refresh_sec` 控制只读刷新间隔，默认 10 秒。成交明细延迟、缺失订单 ID、账户持仓尚未同步时，净盈亏显示“暂无数据 / 同步中”；刷新失败保留上次余额并标记“数据过期”。退出平仓完成后再刷新并保留最终仪表盘。仅采集模式配置 `HL_ACCOUNT_ADDRESS` 后可显示余额，策略盈亏不适用。API 依据：[账户与资金费](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)、[统一账户余额](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot)。



## 验证

`python -X utf8 -m pytest -q`

测试覆盖价差边界、无效及过期行情、开仓前二次检查、随机多空、50ms 起算点与慢回报、部分成交、平仓不受价差限制、预留额度、停止、未知订单、仓位核对、滑点、统计及无 RH 启动。所有订单测试使用模拟传输。

[Hyperliquid API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint). Forked from [your-quantguy/entropy-arb](https://github.com/your-quantguy/entropy-arb); upstream MIT license retained.
