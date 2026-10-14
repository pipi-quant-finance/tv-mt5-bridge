# TV→MT5 桥接服务・Windows VPS 部署图文教程

本教程手把手带你在 **Windows VPS**（或 Windows 服务器 / 电脑）上，把「TradingView 信号 → MT5 自动下单」整套系统跑起来，并通过浏览器访问管理后台。

> ⚠️ 为什么必须 Windows？
>
> `MetaTrader5`
>
>  官方 Python 库
>
> **仅支持 Windows**
>
> 。本机 macOS/Linux 只能开发调试，
>
> **正式部署请在 Windows 上完成**
>
> 。



***

## 1. 系统架构与端口



```
  TradingView 付费版 Alert
        │  (Webhook POST，JSON)
        ▼
   ┌──────────────────────────┐   Windows VPS   ┌──────────────────┐
   │  桥接服务 tv_mt5_bridge.py │────────────────▶│  MT5 终端         │
   │  (端口 5000 /webhook)     │  用 MetaTrader5  │  (登录 FTMO)     │
   │  品种映射 → FTMO1Step风控  │   库下单/平仓     │  自动交易=开       │
   │  → 交易时段 → 单日次数     │                  │                  │
   │  → 新闻过滤 → 开平仓       │                  │                  │
   └──────────┬───────────────┘                  └──────────────────┘
              │ 读写 DuckDB (trading_log.duckdb)
              │  HTTP /api/*（账户/持仓/历史）
   ┌──────────▼───────────────┐
   │  Dashboard 管理后台        │  ← 浏览器访问 http://<IP>:8080
   │  (端口 8080)              │     (总览/信号/订单/分析/管理)
   └──────────────────────────┘
```



| 端口     | 用途                                 | 是否对公网开放     |
| ------ | ---------------------------------- | ----------- |
| `5000` | 桥接服务 Webhook（接收 TradingView Alert） | ✅ 必须        |
| `8080` | Dashboard 管理后台（浏览器访问）              | ✅ 建议（也可仅内网） |



***

## 2. 前置准备



| 项目     | 要求                                                         |
| ------ | ---------------------------------------------------------- |
| 服务器    | Windows Server 2019/2022 或 Win10/11，能联网，有公网 IP（或内网 + 端口转发） |
| Python | 3.10+（安装时**务必勾选 Add to PATH**）                             |
| MT5    | 已安装 MetaTrader5 终端，并登录你的 **FTMO 账户**                       |
| 权限     | 服务器管理员权限（部署脚本需要）                                           |

**建议内存 ≥ 4GB**，MT5 + 桥接 + 后台常驻，太小容易卡。



***

## 3. 部署步骤（共 6 步，约 10 分钟）

### 第 1 步：把项目放到服务器

将本项目整个文件夹 `tv-mt5-bridge/` 拷贝到服务器，例如：



```
C:\ai\tv-mt5-bridge\
```

> 保持目录内文件完整：
>
> `tv_mt5_bridge.py`
>
> 、
>
> `dashboard.py`
>
> 、
>
> `templates/`
>
> 、
>
> `static/`
>
> 、
>
> `deploy.ps1`
>
> 、
>
> `guard_mt5.ps1`
>
> 、
>
> `.env.example`
>
> 、
>
> `requirements.txt`
>
> 。

### 第 2 步：打开 PowerShell，进入目录

右键开始菜单 → **Windows PowerShell (管理员)**，执行：



```
cd C:\ai\tv-mt5-bridge
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

> `Bypass`
>
>  只对当前窗口生效，不会改系统安全策略，安全。

### 第 3 步：一键部署



```
.\deploy.ps1
```

脚本会自动完成：检查 Python → 建虚拟环境并装依赖 → 生成 `.env` → 语法检查 → 注册 Windows 服务（开机自启）。

**首次运行**会在生成 `.env` 后停下，提示你填写真实账号：



```
[3/6] 配置 .env...
已从 .env.example 生成 .env，请用编辑器填写真实 FTMO 账号信息。
注意：填完后请再次运行本脚本完成部署。
```

### 第 4 步：编辑 `.env` 填账号

用记事本打开 `C:\ai\tv-mt5-bridge\.env`，填写（其余保持默认即可）：



```
# FTMO / MT5 账户信息
MT5_LOGIN=你的FTMO账号登录名
MT5_PASS=你的FTMO密码
MT5_SERVER=FTMO-MT5
```

如需启用新增风控，可追加：



```
# 只做欧盘+美盘（UTC 13:00-21:00 = 北京 21:00-次日05:00）
TRADING_OPEN_DAYS=1,2,3,4,5
TRADING_OPEN_HOURS=13:00-21:00
# 单日最多开 5 单
MAX_OPEN_PER_DAY=5
```

> 完整配置说明见 
>
> `.env.example`
>
>  与 
>
> `README.md`
>
> 。

### 第 5 步：再次运行部署（完成注册服务）



```
.\deploy.ps1
```

这次 `.env` 已存在，会继续往下执行：语法检查 → 提示确认 MT5 已打开并开启**自动交易** → 注册 `TvMt5Bridge`(:5000) 与 `TvMt5Dashboard`(:8080) 两个 Windows 服务 + 守护计划任务。

> 确认提示处，请先在服务器上打开 
>
> **MT5 终端**
>
> ，登录 FTMO，确认工具栏「自动交易」为
>
> **绿色**
>
> ，且 
>
> `工具 → 选项 → EA交易 → 允许算法交易`
>
>  已勾选，再回车。

### 第 6 步：防火墙放行端口

在**防火墙**中放行 `5000`（Webhook）与 `8080`（Dashboard）。管理员 PowerShell：



```
netsh advfirewall firewall add rule name="TV_Mt5_5000" dir=in action=allow protocol=TCP localport=5000
netsh advfirewall firewall add rule name="TV_Mt5_8080" dir=in action=allow protocol=TCP localport=8080
```

**云服务器（阿里云 / 腾讯云 / AWS 等）还需在「安全组」中放行这两个入站端口**（TCP 5000 / 8080），否则公网无法访问。



***

## 4. 浏览器访问 Dashboard

部署完成后，在本机浏览器打开：



```
http://<服务器公网IP>:8080
```

看到深色管理后台即部署成功。界面（演示数据）：

**总览页** —— 账户 KPI、FTMO 1-Step 双红线占用、资金曲线、每日盈亏、新闻过滤状态



![总览](docs/screenshots/overview.png)

**信号流页** —— 实时信号表（含风控 / 新闻拦截记录）



![信号流](docs/screenshots/signals.png)

**订单记录页** —— MT5 成交明细



![订单记录](docs/screenshots/orders.png)

**分析页** —— 品种 / 方向分布、历史已实现盈亏



![分析](docs/screenshots/analysis.png)

**管理 & 测试页** —— 风控规则实时状态、品种映射、实时持仓、测试信号



![管理&测试](docs/screenshots/manage.png)



***

## 5. 配置 TradingView Alert

在 TradingView 的 Alert 中，**Webhook URL** 填：



```
http://<服务器公网IP>:5000/webhook
```

消息体用 JSON，需包含 `signal_id`（唯一，用于去重）、`action`（buy/sell/close）、`symbol`（TV 品种名，如 `XAUUSD`），**必须带 `token`**（即 `.env` 里的 `WEBHOOK_TOKEN`，缺失或错误返回 401），建议含 `lot/sl/tp`：



```
{
  "signal_id": "xau-buy-001",
  "action": "buy",
  "symbol": "XAUUSD",
  "token": "你的WEBHOOK_TOKEN",
  "lot": 0.02,
  "sl": 2592,
  "tp": 2640
}
```

> 必须使用 
>
> **TradingView 付费版**
>
> （Webhook 为付费功能）。



***

## 6. 验证全链路

打开 Dashboard「管理 & 测试」页 → 填写测试信号（如 `buy` / `XAUUSD` / lot `0.01`）→ 发送：



1. 若信号通过所有风控，MT5 会真实开仓，信号流页出现「开仓成功 retcode=0」

2. 若被拦截，信号流页会记录原因（如 `FTMO 1-Step 风控拦截`、`新闻过滤拦截`、`交易时段拦截`、`单日开仓次数限制`）

3. 可在「管理 & 测试」页实时持仓列表看到持仓，并可**按 ticket 精确平仓**



***

## 7. 常见问题（FAQ）



| 现象                  | 排查                                                                   |
| ------------------- | -------------------------------------------------------------------- |
| 浏览器打不开 Dashboard    | ① `8080` 是否已在安全组 + 防火墙放行；② 服务是否 Running：`Get-Service TvMt5Dashboard` |
| Webhook 收到但 MT5 不下单 | ① MT5 自动交易是否绿色；② `工具→选项→EA交易→允许算法交易`；③ 看 `service.err.log`           |
| 桥接服务反复重启            | 查看 `C:\ai\tv-mt5-bridge\service.err.log`；多为 MT5 未登录 / 端口占用           |
| 端口被占用               | \`netstat -ano                                                       |
| 重复信号重复下单            | 检查 Alert 是否带唯一 `signal_id`（去重依赖它）                                    |
| 时区不对                | 交易时段用 **UTC**。中国 VPS 为 UTC+8：`13:00-21:00` = 北京 `21:00-次日05:00`      |
| 改了 `.env`/ 代码不生效    | 需重启服务：`Restart-Service TvMt5Bridge, TvMt5Dashboard`                  |
| nssm 下载失败           | 手动下载 nssm-2.24 放到 `%TEMP%\nssm\nssm.exe` 后重跑 deploy.ps1              |



***

## 8. 更新与维护



```
# 更新代码后重启服务
Copy 新代码覆盖到 C:\ai\tv-mt5-bridge
Restart-Service TvMt5Bridge, TvMt5Dashboard
```



```
# 查看信号/订单/风控日志（DuckDB 查询工具）
cd C:\ai\tv-mt5-bridge
.\venv\Scripts\python.exe query_duckdb.py
```



```
# 手动卸载服务（如需重装）
nssm remove TvMt5Bridge confirm
nssm remove TvMt5Dashboard confirm
```

进程守护：`guard_mt5.ps1` 自动检测 MT5 终端与桥接服务，异常自动重启（部署时已注册为计划任务）。