# xaus — Bitget 黄金永续价差可视化工具

完整使用说明见：[USAGE.md](USAGE.md)

**简介**

- **用途**: 从 Bitget 拉取三个与黄金相关的永续合约（XAU、XAUT、PAXG）的历史价格，计算两两之间的价差并绘制时间序列图，便于观察价差行为与异常。
- **入口文件**: [xaus.py](xaus.py)

**依赖**
- **Python**: 3.8+
- **第三方库**: `ccxt`, `pandas`, `matplotlib`, `requests`, `numpy`

安装依赖示例：

```bash
pip install ccxt pandas matplotlib requests numpy
```

**快速开始**

运行默认配置（默认直连，周期 `1h`，拉取 500 根 K 线）：

```bash
python xaus.py
```

常用选项示例：

- 使用 1 小时周期并拉取 1000 根 K 线：

```bash
python xaus.py --timeframe 1h --limit 1000
```

- 需要代理时，在命令行显式启用：

```bash
python xaus.py --proxy http://127.0.0.1:7890
```

- 不弹出图表（只打印数据摘要）：

```bash
python xaus.py --no-plot
```

- 持续运行（每 60 秒一轮），并把图覆盖保存到 `results/spreads_latest.png`：

```bash
python xaus.py --loop
```

- 使用阈值配置做报警（超阈值会打印 `ALERT`）：

```bash
python xaus.py --alert-config alert_config.json
```

更多可用参数请运行：

```bash
python xaus.py --help
```

**环境变量**
- 脚本不会读取 `BITGET_PROXY`、`HTTP_PROXY`、`HTTPS_PROXY` 作为默认代理；默认始终直连。

**阈值报警配置**
- 模板文件：`alert_config.example.json`
- 使用方式：复制为 `alert_config.json` 后填写阈值数值（正数）。
- 字段说明：
  - `enabled`: 是否启用报警
  - `use_absolute`: 是否按绝对值比较（建议 `true`）
  - `thresholds`: 三组价差对应阈值，`null` 表示该项不报警

**脚本行为说明**
- 会创建一个 ccxt 的 `bitget` 客户端（以 swap/永续市场为目标）。
- 拉取 OHLCV 数据后按时间对齐（仅保留三者都有数据的时间点），并计算三组价差：
  - `XAU_minus_XAUT`
  - `XAU_minus_PAXG`
  - `PAXG_minus_XAUT`
- 默认会用 `matplotlib` 绘图显示价差曲线；如需仅获取数据可使用 `--no-plot`。

**注意事项 & 排错**
- 本脚本只使用公共行情接口，不需要 API Key。
- 如果遇到网络或请求失败，脚本内置有限次数重试（可通过 `--retries` 与 `--retry-delay` 调整）。
- 若你的网络需要代理，请在命令行显式传入 `--proxy http://127.0.0.1:7890` 一类参数；未传时始终按直连处理。
- 如果拉取不到数据，请检查交易对是否在 Bitget swap 市场可用，或调整 `timeframe` 与 `limit`。

**文件**
- 主脚本：[xaus.py](xaus.py)
