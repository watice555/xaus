# xaus 使用说明

## 1. 运行前准备

- Python 3.8 及以上
- 安装依赖（在项目根目录执行）：

```bash
pip install -r requirements.txt
```

如果你没有用 `requirements.txt`，也可以手动安装：

```bash
pip install ccxt pandas matplotlib requests numpy
```

## 2. 目录与启动位置

请在项目根目录 `D:\Projects\xaus` 启动脚本，避免路径错误。

```bash
cd D:\Projects\xaus
python xaus.py --help
```

## 3. 基础用法

单次运行（默认直连，且会弹图）：

```bash
python xaus.py
```

需要代理时，在命令行显式启用：

```bash
python xaus.py --proxy http://127.0.0.1:7890
```

指定周期与数量：

```bash
python xaus.py --timeframe 1m --limit 500
```

不弹图（只看日志）：

```bash
python xaus.py --no-plot
```

## 4. 持续运行模式（每 60 秒）

循环模式：

```bash
python xaus.py --timeframe 1m --loop
```

说明：

- 每 60 秒执行一轮
- 图像会保存到 `results/spreads_latest.png`
- 每轮覆盖同一文件，不会堆积大量图片
- 按 `Ctrl+C` 退出

## 5. 阈值报警配置

### 5.1 创建配置文件

复制模板：

```bash
copy alert_config.example.json alert_config.json
```

### 5.2 编辑阈值

`alert_config.json` 示例：

```json
{
  "enabled": true,
  "use_absolute": true,
  "thresholds": {
    "XAU_minus_XAUT": 80,
    "XAU_minus_PAXG": 120,
    "PAXG_minus_XAUT": 70
  }
}
```

字段说明：

- `enabled`: 是否启用报警
- `use_absolute`: `true` 表示按绝对值比较（推荐）
- `thresholds`: 三组价差阈值，必须是正数；写 `null` 表示该项不报警

### 5.3 启动带报警运行

单次检查：

```bash
python xaus.py --timeframe 1m --alert-config alert_config.json --no-plot
```

循环检查：

```bash
python xaus.py --timeframe 1m --loop --alert-config alert_config.json
```

未传 `--alert-config` 时不会读取报警配置，也不会报警；当超阈值时，会在日志里出现 `ALERT` 的 `WARNING` 记录。

## 6. 常用参数

- `--timeframe`: K 线周期，如 `1m/5m/1h/4h/1d`
- `--limit`: 拉取 K 线数量
- `--timeout-ms`: 请求超时（毫秒）
- `--retries`: 网络失败重试次数
- `--retry-delay`: 重试间隔秒数
- `--proxy`: 代理地址；只有显式传入时才启用，默认直连
- `--no-plot`: 单次模式不弹图
- `--loop`: 持续运行（每 60 秒）
- `--alert-config`: 报警配置文件路径

## 7. 常见问题

### Q1: 报错 `can't open file ...\\results\\xaus.py`

原因：你在 `results` 目录执行了 `python xaus.py`。
解决：回到项目根目录运行，或用 `python ..\\xaus.py ...`。

### Q2: 没有报警输出

- 检查是否传了 `--alert-config alert_config.json`
- 检查 `enabled` 是否为 `true`
- 检查阈值是否是正数，且不是 `null`

### Q3: 网络失败或拉不到数据

- 默认不会读取环境变量代理；如果网络需要代理，请显式传入 `--proxy`
- 增大 `--timeout-ms`
- 提高 `--retries` 并适当增加 `--retry-delay`
