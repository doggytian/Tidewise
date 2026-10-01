# Tidewise

中低频国内期货 CTA 交易系统：日线趋势跟踪、波动率定仓、账户级风控、半自动执行（飞书确认后才下单），通过 CTP 接入期货公司柜台。

**以 [vn.py](https://github.com/vnpy/vnpy) 为主体**：目标持仓调仓、组合回测、平今平昨、CTP 接入都直接用 vn.py 已在实盘中验证过的实现。Tidewise 只补 vn.py 没有的部分：逐合约数据、主力合约判定与复权、波动率定仓、换月成本、半自动审批、账户级风控、对账与运维。

定位：**系统化交易参谋 + 风控纪律工具**。每天收盘后给出信号、目标手数、风险占用和调仓清单，由人确认后执行。

- 路线图、各阶段验收标准、资金规则：[docs/ROADMAP.md](docs/ROADMAP.md)
- 技术设计：[docs/plans/2026-09-29-tidewise-design.md](docs/plans/2026-09-29-tidewise-design.md)

## 开发进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1 | 配置与密钥、调仓单状态机 | 完成 |
| P2 | 数据：新浪逐合约日线 → 主力判定 → 加法后复权连续合约；vn.py 回测 + 换月成本 | 完成 |
| A | 研究可信化：延长历史、多速度信号 + carry、小资金适配、账户级风控 | 待开始 |
| B | 每日决策报告（`tidewise report`） | 完成（控制台/Markdown；飞书推送待接） |
| C | 前向跟踪 6 个月 | 待开始 |
| D | 自动执行（可选）：vn.py + CTP | 待开始 |

## 快速开始

需要 [uv](https://docs.astral.sh/uv/)，Python 版本在 3.11 到 3.13 之间（vn.py 4.4 依赖的 PySide6 还不支持 3.14）。

```bash
uv sync
cp config/example.yaml config/local.yaml      # 本地配置，已加入 gitignore
uv run tidewise config check

uv run tidewise data update -p rb             # 下载单个品种（首次约 1 分钟，之后增量更新只需几秒）
uv run tidewise data update                   # 下载全部启用品种
uv run tidewise report                        # 每日决策报告（需先在 var/positions.yaml 填持仓）
uv run tidewise backtest -p rb                # 单品种回测
uv run tidewise backtest                      # 组合回测，报告写到 var/reports/
uv run pytest
```

## 数据与回测口径

- **数据**：通过 akshare 获取新浪逐合约日线，大约从 2018 年上市的合约开始。已到期合约的数据只下载一次，之后永久缓存在 `var/bars/raw/`。
- **主力合约**：合格合约里，远月合约持仓量连续 N 天排第一就切换过去；只往远月切，不回切；到交割月前一个月的 15 日强制切换。T 日收盘时判定的主力合约，从 T+1 开始持有。
- **复权**：用加法后复权，所以持仓盈亏（点数 × 乘数）和真实持仓一致；序列最新一段的价格等于真实价格。
- **撮合**：T 日收盘出信号，T+1 以开盘价成交（vn.py 原生撮合逻辑）。回归测试会用一个独立的模拟程序逐日核对毛盈亏，保证两边一致。
- **成本**：手续费按比例收取；按手收费的品种折算成比例。每手单边滑点 1 跳。换月成本单独补记，按"持仓 × 2 腿 ×（手续费 + 滑点）"计算。

## 密钥

密钥只从环境变量读取。配置文件里一旦出现 `password`、`secret`、`auth_code`、`token` 这类字段，程序会直接报错。变量清单见 [.env.example](.env.example)。

## 目录结构

```
src/tidewise/
  config/    YAML 配置结构、加载器、环境变量密钥
  plan.py    调仓单状态机（半自动审批）
  data/      合约代码规范、新浪数据源与缓存、主力判定与连续合约、数据流水线
  strategy/  ewmac/ensemble 纯函数信号与定仓；trend.py vn.py 组合策略；replay.py 决策链复放
  live/      持仓文件（positions.yaml）与每日决策报告
  backtest/  vn.py 回测封装、换月成本、报告
  cli.py     命令行入口
```

注意：导入 vn.py 时，它会在 `~/.vntrader/` 下写入日志和配置，这是 vn.py 自身的行为。

## 免责声明

本项目仅供个人研究使用，不构成投资建议。期货交易风险很高，实盘前请完成程序化交易报备，并自行承担全部风险。
