# Tidewise 中低频期货 CTA 交易系统 技术方案

> 日期：2026-09-29
> 状态：Confirmed（2026-09-30 架构修订为以 vn.py 为主体；P1~P2 已完成）

## 0. 已冻结约束

| 维度 | 决策 |
|---|---|
| 市场 | 国内商品/金融期货，CTP 通道 |
| 运行内核 | vn.py 为主体（`vnpy` + `vnpy_portfoliostrategy`，P4 加 `vnpy_ctp`），只自建 vn.py 缺少的部分 |
| 策略周期 | 日线为主，数据层与执行层预留小时线扩展 |
| 资金量级 | 按 10~50 万设计，品种池与风控参数全部配置化（按权益百分比） |
| 部署 | 国内云服务器 Linux + systemd（可选 Docker），macOS 可本地开发 |
| 自动化 | 半自动：调仓单需飞书卡片确认，超时不执行 |
| 时序 | T 日收盘后出信号 → 人工确认 → 下一交易时段开盘后执行（夜盘品种 21:05，无夜盘 9:05） |
| 回测口径 | T 日收盘信号、T+1 开盘价 + 滑点成交，与实盘一致 |

## 1. 需求概述

### 用户场景

每个交易日 15:30 系统被定时拉起：同步行情与账户 → 更新日线与主力合约映射 → 策略计算各品种目标持仓 → 风控裁剪 → 与实际持仓求差生成调仓单（含换月移仓）→ 飞书卡片推送。用户在卡片上确认或拒绝。执行窗口（21:05 / 9:05）到达时，系统仅执行已确认的调仓单，以限价 + 追价方式完成，结果推送。收盘后自动与结算单对账并生成日报。任何异常（登录失败、持仓不一致、熔断触发）立即告警并停止交易。

### 功能边界

| 包含（MVP） | 不包含（后续迭代） |
|---|---|
| 日线数据采集、清洗、主力连续与换月 | 分钟/小时线策略 |
| 单一多品种趋势策略（参数可配） | 多策略组合与资金分配 |
| 策略代码回测/实盘统一 | 参数自动优化、机器学习 |
| 波动率定仓 + 账户级风控 + kill switch | 期权、跨市场品种 |
| 目标持仓驱动执行（限价追价、平今平昨） | 算法拆单（TWAP 等） |
| 飞书推送 + 卡片确认 | Web 管理后台 |
| 持久化、重启恢复、盘后对账、日报 | 多账户 |
| 仿真环境（openctp / SimNow）先行，后切实盘 | 全自动模式（第二阶段开启） |

---

## 2. 技术方案

### 架构设计（2026-09-30 修订：以 vn.py 为主体）

> 修订原因：读 vn.py 4.4 源码确认，目标持仓调仓（`vnpy_portfoliostrategy.StrategyTemplate.set_target/rebalance_portfolio`）、
> 组合回测（`BacktestingEngine`，T+1 按 min(委托价, 开盘价) 撮合）、平今平昨拆单（`vnpy.trader.converter.OffsetConverter`）、
> CTP 接入（`vnpy_ctp`）均已有实盘验证的实现。自建会重复调试最危险的部分，故改为：**vn.py 为主体，只自建它缺的部分**。

| 能力 | 来源 |
|---|---|
| 交易所/方向/开平/K线/持仓等类型 | 直接使用 `vnpy.trader.constant` / `vnpy.trader.object`（不再自建 core 类型） |
| 目标持仓 → 委托（先平后开） | `vnpy_portfoliostrategy.StrategyTemplate.rebalance_portfolio` |
| 组合回测、逐日盯市统计 | `vnpy_portfoliostrategy.BacktestingEngine`（仅覆盖 `load_data` 从 Parquet 注入） |
| 平今平昨、CTP、订单级风控 | `OffsetConverter`、`vnpy_ctp`、`vnpy_riskmanager`（P4） |
| **自建**：逐合约数据、主力判定、后复权连续 | `tidewise.data` |
| **自建**：EWMAC 信号 + 波动率定仓 + 持仓缓冲 | `tidewise.strategy.ewmac`（公式参考 Carver 公开著作，未用 pysystemtrade GPL 代码） |
| **自建**：换月成本补记 | `tidewise.backtest`（连续合约上无真实换月成交） |
| **自建**：半自动审批状态机 | `tidewise.plan` |
| **自建**：账户级熔断、对账、飞书、定时启停 | P4~P5 |

**进程模型**：短生命周期任务由 systemd timer 拉起，任务间仅通过 SQLite 中的调仓单状态通信；仅审批进程常驻。

```
systemd timer 15:30      → tidewise signal     数据更新 → 信号 → 风控 → 生成调仓单 → 推送
常驻 service             → tidewise approval   飞书长连接接收确认/拒绝 → 更新调仓单状态
systemd timer 21:05/9:05 → tidewise execute    vn.py MainEngine + CtpGateway → 载入已批准目标 → rebalance_portfolio
systemd timer 15:45      → tidewise reconcile  结算单 vs 本地 → 日报 → 不一致则锁定交易
```

审批进程使用飞书自建应用**长连接模式**，服务器不开放公网入站端口。

**存储**：K 线用 Parquet（`var/bars/raw` 逐合约、`var/bars/continuous` 连续）；状态（调仓单、审计）用 SQLite。

### 模块

| 模块 | 职责 | 状态 |
|---|---|---|
| `config/` | YAML schema（未知字段拒绝、密钥字段拒绝）+ 环境变量密钥 | 完成 |
| `plan.py` | 调仓单状态机（TargetPosition 以 vt_symbol 表示；PlannedOrder 用 vn.py Direction/Offset） | 完成 |
| `data/` | 新浪逐合约日线（akshare）→ Parquet 增量缓存 → 主力判定 → 加法后复权连续 | 完成 |
| `strategy/` | `ewmac.py` 纯函数（回测/实盘信号共用）；`trend.py` vn.py 组合策略封装 | 完成 |
| `backtest/` | vn.py 回测 + 换月成本 + 报告；与独立模拟逐日对账的回归测试 | 完成 |
| `portfolio/` | 账户级风控：保证金占用、单品种上限、回撤熔断、kill switch | P3 |
| `live/` | 信号任务（连续序列 → 预测 → 主力合约目标手数）、执行任务（vn.py + CTP） | P4 |
| `ops/` | SQLite 持久化、飞书审批、对账、systemd | P5 |

### 数据口径（`data/`）

- 来源：`akshare.futures_zh_daily_sina(合约)`，约 2018 年起上市的合约；已到期合约下载一次永久缓存。
- 主力：合格合约（有行情、未到强制换月日）中，远月持仓量连续 `roll_confirm_days` 日第一则切换；只向远月切；
  当前主力到达「交割月前一月 `force_roll_day` 日」强制切换；当日主力无行情（数据源缺口）不做判定。
- T 日收盘判定的主力在 T+1 持有；换月价差取判定日两合约收盘价差，加法后复权、锚定最新合约。
  出现非正价格时整体平移（不影响价差与盈亏）；真实价格保存在 `raw_close`，定仓/保证金必须用它。

### 调仓单状态机（唯一跨进程状态）

```
DRAFT → PENDING_APPROVAL → APPROVED → EXECUTING → DONE
  │                     ↘ REJECTED   │          ↘ PARTIAL（不自动补单，告警待人工）
  │                     ↘ EXPIRED    ↘ EXPIRED   ↘ FAILED
  └→ DONE（仅限无委托的空计划）
```

- `APPROVED → EXPIRED`：已确认但错过执行窗口的计划不得在之后的时段执行（信号已陈旧）。
- 终态：REJECTED / EXPIRED / DONE / PARTIAL / FAILED。任何非法流转抛 `InvalidTransitionError`。

---

## 3. 核心接口契约

**信号与定仓（`strategy.ewmac`，纯函数）**

```python
class EwmacState:                      # 逐根推进，无未来函数
    def update(self, close: float) -> float | None   # 预测值 [-20, 20]，未预热返回 None
    daily_vol: float                   # 日价格波动（点数）

def optimal_position(forecast, daily_vol_points, multiplier, equity, vol_target) -> float
def buffered_target(optimal, current, avg_position, buffer_fraction) -> int
```

**vn.py 策略（`strategy.trend.EwmacTrendStrategy(StrategyTemplate)`）**：`on_bars` 中逐品种计算目标 → `set_target` → `rebalance_portfolio`。

**回测（`backtest.run_backtest(cfg, products) -> BacktestResult`）**：stats（vn.py 口径，已扣换月成本）、daily、trades、roll_cost。

**持久化与通知（`ops`，P5）**

```python
class PlanRepository(Protocol):
    def save(self, plan: RebalancePlan) -> None
    def get(self, plan_id: str) -> RebalancePlan | None
    def latest(self, trading_date: date) -> RebalancePlan | None
    def compare_and_set(self, plan_id: str, expected: PlanStatus, new: RebalancePlan) -> bool

class Notifier(Protocol):
    def send_plan(self, plan: RebalancePlan) -> None     # 半自动模式带确认/拒绝按钮
    def send_text(self, level: Literal["info", "warn", "alert"], text: str) -> None
```

`compare_and_set` 是跨进程并发的唯一保护：审批进程与执行进程对同一计划的状态更新必须以期望旧状态为前提。

---

## 4. 外部依赖与约束

### SDK/API 依赖

| API | 来源 | 注意事项 |
|---|---|---|
| CTP（经 `vnpy_ctp`） | 期货公司柜台 | 需穿透式认证（AppID/AuthCode）；查询流控约 1 次/秒；每日需确认结算单 |
| openctp TTS / SimNow | 仿真 | openctp 7x24；SimNow 仅交易时段；实盘前两者至少跑通其一 |
| akshare | 免费数据 | 仅用于研究期日线；字段口径与稳定性不保证，入库前必须校验 |
| 飞书自建应用 | 通知/审批 | 长连接模式接收卡片回调；不可达时计划自然 EXPIRED，绝不默认执行 |

### 已知约束

- 夜盘归属下一交易日：所有 `trading_date` 以交易所交易日为准，不用自然日。
- 上期所/能源中心平仓必须区分平今/平昨，且平今手续费常高于平昨。
- 涨跌停价位可能无法成交：执行器以涨跌停价为限价边界，未成部分标 PARTIAL。
- 交易所异常交易认定（撤单次数、自成交）：追价次数设上限，单日撤单计数纳入风控。
- 程序化交易需向期货公司报备，实盘前完成。
- 密钥（CTP 账号/密码/AuthCode、飞书 app secret）仅来自环境变量，配置文件 schema 禁止出现。

---

## 5. 风险与未决问题

| 风险/问题 | 影响 | 应对策略 |
|---|---|---|
| 主力合约判定口径与交易所/数据源不一致 | 换月时点偏差、回测失真 | P2 自建主力规则（持仓量连续 N 日领先且只向远月切换），回测与实盘共用 |
| 免费数据缺失/错误 | 信号错误 | 入库校验（OHLC 一致性、跳空阈值、缺交易日），异常品种当日跳过并告警 |
| 执行窗口进程未启动（服务器故障） | 漏调仓 | 计划 EXPIRED + 告警；下一信号日按新目标重新对齐，不补旧单 |
| 部分成交后持仓与目标不一致 | 敞口偏差 | 不自动补单；下一信号日由"目标 vs 实际"自然对齐 |
| 盘后对账不一致 | 状态失真 | 置交易锁（kill switch），人工解锁前拒绝执行 |
| vn.py/CTP 在 macOS 的兼容性 | 本地无法联调 CTP | macOS 仅跑回测/单测/SimBroker；CTP 联调在 Linux 服务器 |

---

## 6. 验证策略

### 单元测试

| 测试 | 覆盖内容 |
|---|---|
| `tests/core/*` | 价格对齐、保证金/手续费、持仓净额、Bar 校验、状态机合法/非法流转 |
| `tests/config/*` | 示例配置可加载、未知字段/重复品种拒绝、缺失密钥报错、密钥 repr 脱敏 |
| `tests/data/*`（P2） | 主力切换规则、连续合约拼接、交易日历 |
| `tests/strategy|portfolio/*`（P3） | 无未来函数、定仓取整、风控裁剪与熔断 |
| `tests/execution/*`（P4） | 先平后开、平今平昨拆分、换月、追价与 PARTIAL |

### 集成验证

- P3：10+ 品种、≥5 年日线回测报告（含手续费、滑点、换月成本）。
- P4：openctp 仿真完成一轮调仓并与柜台持仓一致。
- P5：仿真环境连续跑通 ≥10 个交易日（信号 → 审批 → 执行 → 对账），零人工修数据。
