# longbridge-statement-skill

*[English](README.md) | 中文*

一个 Claude/agent **skill** + 独立 Python 工具，通过**长桥官方 CLI** 拉取**长桥证券（Longbridge）**
账户结单，生成可直接报税的 CSV —— 面向**个人所得税·境外所得（个税 境外所得）**、CRS 申报、券商盈亏对账。

是 **[futu-statement-skill](https://github.com/BH-M87/futu-statement-skill)** 的姊妹篇（另一家券商）。
两家券商的已实现盈亏合并后，才是最终的财产转让所得。

## 为什么需要它

和多数券商一样，长桥结单：

- **没有「已实现盈亏」栏** —— 本工具用移动加权平均从 `stock_trades` 重建，并以**上年 12 月期末持仓**
  （`equity_holdings.cost_price`）做开仓成本，跨年持仓才不会算错；
- **分红在 `corps`（公司行动）里**，不是单独的「派息」栏；
- **利息在 `interests`** —— 是你付出的融资成本，不是收入。

方法见 [`SKILL.md`](SKILL.md)。

## 前置：长桥官方 CLI

本工具**只调用 `longbridge` 命令**，不碰你的任何凭证。**如果没装 CLI，脚本会直接停下并打印下面这些
安装/登录步骤**（不会自动安装）。一次性配置：

```bash
# 安装（按平台任选其一）
brew install --cask longbridge/tap/longbridge-terminal                                   # macOS
curl -sSL https://open.longbridge.com/longbridge/longbridge-terminal/install | sh        # macOS/Linux
scoop install https://open.longbridge.com/longbridge/longbridge-terminal/longbridge.json # Windows

# 登录认证（会打开浏览器走 OAuth）
longbridge auth login

# 验证能跑
longbridge statement --type monthly --format json
```

如果调用因未登录失败，脚本会提示你运行 `longbridge auth login`。文档：
https://open.longbridge.com/zh-CN/skill/ 。本工具用的是 **CLI**（不是长桥 MCP）。
无需第三方 Python 包，Python 3.10+。

## 用法

```bash
python3 longbridge_tax.py --year 2025 -o 输出/ --rate 0.90322
```

`--rate` 可选 —— HKD→RMB 年末中间价；给了就额外输出人民币列并计算应纳税额。

### 输出（UTF-8-BOM，Excel 直接打开不乱码）

| 文件 | 内容 |
|---|---|
| `longbridge_<年>_成交明细.csv` | 股票/期权/基金成交；`清算金额` 已扣手续费 |
| `longbridge_<年>_股息利息现金流.csv` | 分红（corps）、融资利息、出入金 |
| `longbridge_<年>_已实现盈亏_按标的.csv` | 按标的已实现盈亏（移动加权平均） |
| `longbridge_<年>_账户净值.csv` | 每月资产总值（交叉校验） |
| `longbridge_<年>_税务汇总.csv` | 税务汇总：财产转让/分红/利息 + 应纳税额（需 `--rate`） |

脚本会打印已实现盈亏合计、分红、利息，方便核对。

## 已实现盈亏怎么算

按标的移动加权平均（长桥现金账户，主要做多）。`clear_amount` 带符号且已扣费；已实现只在**卖出**时
确认，年末仍持有的标的实现记 0（属未实现，不计税）。开仓成本用上年 12 月 `equity_holdings.cost_price`
带入。货币基金会显示但不并入已实现口径。**财产转让税须跨券商合并**——这里的单账户数要与（如）富途
在「财产转让所得」内相抵后再算。详见 [`SKILL.md`](SKILL.md)。

## 作为 Claude Code skill 使用

把本目录放进 skills 目录（如 `~/.claude/skills/longbridge-statement-tax/`）或用插件管理器安装。
当你让 Claude 算长桥盈亏/分红/税时，会自动加载 [`SKILL.md`](SKILL.md)。

## 隐私

仓库内**不含任何个人数据**。工具从你已认证的长桥 CLI 实时拉取、把 CSV 写到本地；`.gitignore`
已拦截 `*.csv`/`*.json`，避免误提交结果。

## 许可证

MIT
