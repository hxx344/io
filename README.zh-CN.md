# Lighter RH – Entropy 固定四腿交易

基于 [your-quantguy/entropy-arb](https://github.com/your-quantguy/entropy-arb) 修改；四腿执行顺序及虚拟触价规则参考 [hxx344/perp 的 aster_lighter_cycle](https://github.com/hxx344/perp/blob/76d8e8e620ca65a0af9c90163a93b0d53f319468/strategies/aster_lighter_cycle.py)。[English](README.md)

每轮独立按 50/50 随机选择追多或追空，选定方向后整轮保持不变。LEG1、LEG3 固定是 Lighter Robinhood 的本地虚拟限价单；LEG2、LEG4 固定只在 Entropy（Hyperliquid `io` dex）执行带滑点保护的真实市价单。

| 腿 | 随机选中追多 | 随机选中追空 |
| --- | --- | --- |
| LEG1：RH 虚拟入场 | 在 RH 卖四挂虚拟卖单，等待 RH 买一 ≥ 挂价 | 在 RH 买四挂虚拟买单，等待 RH 卖一 ≤ 挂价 |
| LEG2：Entropy 开仓 | 市价买入开多 | 市价卖出开空 |
| LEG3：RH 虚拟出场 | LEG2 确认后，在当时 RH 买四挂虚拟买单，等待卖一 ≤ 挂价 | LEG2 确认后，在当时 RH 卖四挂虚拟卖单，等待买一 ≥ 挂价 |
| LEG4：Entropy 平仓 | `reduce_only` 市价卖出平多 | `reduce_only` 市价买入平空 |

这里的“虚拟成交”是公共盘口触价模拟，不向 RH 提交订单，也不模拟排队优先级或真实成交量。虚拟买单取 RH 买盘按价格从高到低排列的第 N 档，虚拟卖单取卖盘从低到高排列的第 N 档；`cycle.virtual_depth` 默认是 4，可改成 1、2、5、10 等正整数。直接使用该档真实价格，不叠加偏移；对应盘口不足 N 档时等待补足，不退回买一／卖一。挂价固定到触发或超时，超时后按最新第 N 档重新挂价。只有挂单之后的新盘口更新可以触发；断线或盘口 nonce 缺口后重新挂价。

LEG2 和 LEG4 均为市价执行，分别由 `execution.leg2_slippage_bps`、`execution.leg4_slippage_bps` 控制滑点。Entropy 使用 Hyperliquid 接口，其[官方市价开仓／平仓实现](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/exchange.py)通过带价格保护的 IOC 立即吃单，未成交部分撤销，不留挂单。本项目沿用这个协议实现，保留异步成交确认。每条实盘腿在首次提交前读取 Entropy 自身盘口：买入保护价不高于 `ask × (1 + 滑点bps / 10000)`，卖出保护价不低于 `bid × (1 - 滑点bps / 10000)`，价格精度向保护范围内取整。同一腿的重试沿用首次参考价和滑点上限，不因行情移动扩大允许滑点；下一条腿重新取价。超过保护范围的数量可能不成交，耗尽重试后停机并保留状态。

例如，LEG1 虚拟卖单挂在 RH 卖四；RH 买一涨到挂价后，LEG2 在 Entropy 追多。确认实际成交数量后，才在 RH 当时的买四挂 LEG3 虚拟买单；RH 卖一下跌到挂价后，LEG4 平掉本轮多仓。平仓不额外等待价差、盈利或手续费门槛。

## 安装与运行

Python 3.10+。在仓库目录执行：

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell 改用：.\.venv\Scripts\Activate.ps1
python -X utf8 -m pip install -r requirements-live.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

`.env` 只填写 `HL_PRIVATE_KEY`、`HL_ACCOUNT_ADDRESS`。使用 API agent 时，前者是 agent 私钥，后者是主账户地址。无需 Lighter RH 账户、API key 或签名 SDK。REST 与 WebSocket 支持环境中的代理配置。

先在 `config.yaml` 设置数量、挂价距离和循环次数，再启动。`SNDK` 是示例，`--symbol` 必须是两个交易所共同支持的符号，程序启动时动态解析市场 ID 与精度。

```bash
# 公共行情采集；不运行策略、不需要凭证、不下单
python main.py --symbol SNDK --record-only --cn

# 实盘四腿循环
python main.py --symbol SNDK --cn

# 纯日志输出
python main.py --symbol SNDK --no-dashboard
```

`--hedge lighter-rh` 可省略；其他交易所会被拒绝。原版 `thresholds`、`inventory`、`hedge` 配置块不再使用，旧配置会明确报错，请从新示例复制。上一版的 `cycle.virtual_offset_bps` 请删除，改为 `cycle.virtual_depth: 4`。

## 主要配置

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `cycle.direction` | `random` | 每轮随机多空；也支持 `long`、`short` |
| `cycle.cycles` | `0` | `0` 持续循环；正整数限制完成轮数 |
| `cycle.virtual_depth` | `4` | 虚拟买单取买 N、虚拟卖单取卖 N；从 1 开始计数 |
| `cycle.virtual_requote_sec` | `3.0` | 虚拟单等待多久后重挂；重挂不改变本轮方向 |
| `cycle.max_hold_sec` | `0` | `0` 一直等 LEG3；正数表示持仓超时直接走 LEG4 平仓 |
| `sizing.quantity` | `0` | 大于 0 时固定基础币数量；0 时按美元名义金额计算 |
| `sizing.order_notional_usd` | `50` | 按 Entropy 当前可执行价格计算数量 |
| `sizing.max_order_notional_usd` | `500` | 单次开仓上限 |
| `entropy.max_position_usd` | `1000` | Entropy 开仓名义金额上限 |
| `execution.leg2_slippage_bps` | `20` | LEG2 市价开仓滑点上限；20 bps = 0.20% |
| `execution.leg4_slippage_bps` | `20` | LEG4 市价平仓滑点上限；20 bps = 0.20% |
| `execution.max_order_attempts` | `3` | 已明确未成交的开仓、部分平仓的最大尝试次数 |
| `execution.cooldown_sec` | `2` | 完成一轮后间隔 |

例如改为买六／卖六，开仓允许 0.10%、平仓允许 0.15%：

```yaml
cycle:
  virtual_depth: 6
execution:
  leg2_slippage_bps: 10
  leg4_slippage_bps: 15
```

修改配置后重启生效。旧的 `execution.leg_slippage_bps` 仍可用作公共默认值；未单独设置的腿使用这个默认值（20 bps）。

数量按 Entropy 数量精度向下取整，不受 RH 虚拟腿最小下单量约束。LEG2 部分成交后直接使用确认成交量执行 LEG3/LEG4，不补足开仓。LEG4 每次只平剩余数量，始终设置 `reduce_only`；低于交易所最低名义金额的残仓仍尝试 reduce-only 关闭，若交易所拒绝且重试耗尽则停机保留状态。

## 成交确认与停止

启动时要求所选 Entropy 市场仓位为零且没有已有挂单；同一账户/品种只运行一个实例，也不要同时手工交易该仓位。每个实盘订单确认后核对账户仓位，持仓期间定期复核。RH 不存在真实持仓，也没有净敞口对冲逻辑。

IOC 同步响应明确成交后才推进下一腿。网络超时、5xx 或异常响应会按唯一 `cloid` 查询订单状态；仍无法确认时停机，不盲目重发。`logs/cycle.json` 在提交订单之前写入检查点，异常退出后的未完成检查点会阻止直接重启；核实 Entropy 订单终态并处理残仓后，归档该状态文件再启动。日志保留订单 `cloid` 便于查询。状态文件附带进程锁；使用同一个状态文件的第二个进程会被拒绝。

按 Ctrl+C 或发送 SIGTERM：LEG1 等待中直接停止；已有确认仓位时等待进行中的下单完成，再通过 LEG4 做有限次平仓。只有 Entropy 行情仍可用、订单结果明确时才能确认平仓完成；未知订单、仓位不一致、平仓重试耗尽时会非零退出并保留未完成状态。`kill -9` / 强制结束进程无法执行平仓。

`logs/legs.csv` 记录每腿方向、虚拟/真实、数量、挂价、状态与退出原因；`logs/minutes.csv` 保留原分钟行情格式，`hedge_*` 列代表 RH 参考行情。`tools/analyze.py` 只输出行情统计，不生成策略阈值。虚拟两腿不提供真实对冲，实际盈亏取决于 Entropy 两次成交价、手续费与资金费。

## LEG2 / LEG4 成交额统计

仪表盘显示 `本次成交额 | LEG2 $… | LEG4 $… | 合计 $…`，最近执行列表显示每笔真实订单的成交额。每次实盘订单确认后、周期状态日志以及程序退出时都会输出累计值。

统计公式为 `实际成交数量 × 实际成交均价`，LEG2 和 LEG4 的金额相加，开仓和平仓都计入；多空方向不影响金额的正负。部分成交与多次平仓分别累计已成交部分，虚拟 LEG1/LEG3、撤单未成交量不计入，不扣手续费或资金费，也不表示盈亏。例如开仓成交 $50、平仓成交 $51，合计为 $101。

统计范围是**本次进程启动以来**，跨轮累加，重启清零；状态检查点保留本次统计快照，`logs/legs.csv` 继续保存实际成交数量与均价供事后核对。通过订单状态补确认的成交会尝试读取真实成交明细计算均价；均价仍缺失时，面板标记“合计不完整”并列出缺少均价的已成交数量，不使用挂价或滑点保护价代替成交价。

## 验证

```bash
python -X utf8 -m pip install pytest
python -X utf8 -m pytest -q
```

测试覆盖随机多空完整顺序、买卖档位选择、深度不足、虚拟触价与重挂、断线/旧消息、部分成交、限频、订单结果不明、仓位不一致、停止与残仓、状态恢复、Windows/Unix 进程锁，以及官方 Hyperliquid SDK 的市价 IOC / reduce-only 签名格式、买卖滑点边界及跨重试保护价。所有订单测试均使用模拟传输。

API 依据：[Hyperliquid Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)、[Lighter RH WebSocket](https://apidocs.rh.lighter.xyz/docs/websocket)。保留上游 MIT 许可证。
