# ReBS 欧易行情终端

使用欧易 OKX 行情的现货看盘网页。Vue 3 + TypeScript + Element Plus，KLineChart 绘制实时 K 线，ECharts 绘制累计深度；Python FastAPI 提供 REST 和 WebSocket 中继。公开行情无需 API Key，模拟账户功能需要在服务器配置模拟盘专用 API 凭据。

## 已实现

- 现货币对搜索、计价币筛选、成交额/涨幅排序、自选股改为自选币对并保存在浏览器。
- 最新价、24 小时涨跌幅/高低价/成交量/成交额。
- 1s、1m、3m、5m、15m、30m、1H、2H、4H、1D、1W、1M K 线，缩放、拖动、历史分页、回到最新、全屏。
- MA(5/10/20)、成交量、MACD(12/26/9)；实时更新当前蜡烛，跨周期追加新蜡烛。
- OKX `books` 400 档深度，先快照后合并增量；检测序列缺口后重新连接并重建订单簿。
- 盘口显示 10/20/50 档、价格精度聚合、买卖深度比例、累计深度图。
- `trades-all` 逐笔成交、买卖方向筛选、暂停/继续显示。列表最多保留最近 300 笔，显示最多 100 笔。
- 两路实时连接状态、心跳、有限退避重连；业务连接恢复后重新补取当前 K 线窗口。
- 桌面三栏布局、窄屏适配，绿色上涨、红色下跌。
- 欧易模拟账户资产、持仓、当前/历史委托和成交查询，每 15 秒更新。
- 模拟现货限价/市价下单、逐单撤单、委托状态查询；固定模拟环境和现金交易模式，签名与密钥仅保存在后端。
- 策略工作台：可搜索的因子公式目录、完整参数表、策略模板、自定义表达式、参数与依赖校验、手动样本试算、浏览器草稿和服务器草稿保存。

模拟交易使用欧易的模拟账户和撮合服务，需要配置专用 API Key 的读取与交易权限；访问私人账户必须通过 HTTPS 或 SSH 转发，并输入独立的网页访问口令。当前不支持实盘下单、杠杆或合约下单。旧的 A 股 CSV 和后端代码保留，主页面已切换为欧易。

## 策略工作台

点击顶部「策略工作台」，或访问 `/#strategy`。先选择模板，再在因子目录中查看公式、输入字段、单位、可调参数和数据要求；把需要的因子加入策略并设置别名，使用别名编写入场、离场与过滤条件。全局参数涵盖行情窗口、逐笔处理、信号、仓位、风控、执行和回测口径，各分组可逐项展开编辑。

当前目录包含 13 类、226 个因子、171 个全局参数、18 个表达式函数和 6 个模板。完整口径见 [策略目录说明](docs/strategy-catalog.md)。

草稿自动保存在当前浏览器，可导入/导出 JSON。服务器草稿使用与模拟账户相同的网页访问口令，且需要 HTTPS 或 SSH 本地转发；口令只在内存中保留。服务器草稿带修订号，过期页面保存时会报告冲突，避免覆盖别处修改。

此版本完成的是配置工作台。目录中的公式是计算定义，尚未接入自动因子计算、历史回测或后台策略调度。手动样本试算使用用户输入的因子数值/序列验证表达式，不会拉取真实行情计算指标，也不会下单。保存和校验通过均不代表策略正在运行。

逐笔成交统计需要连续记录、去重、处理断线缺口；现有页面的短时成交缓冲不能作为完整历史。聚合盘口不能还原逐单委托、真实撤单率或队列位置。衍生品、链上等未接入数据明确列为不可用。因子目录不是所有可能公式的全集，可以通过自定义表达式组合已有因子，并扩展 `backend/strategy_catalog.json` 增加定义。

## 启动项目

在 `BuyAndSell` 根目录运行：

```powershell
.\ReBS\start.ps1
```

访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。脚本使用 `ReBS/.venv`，检查固定 Python 依赖；缺少构建时自动构建前端。首次构建需要 Node.js 20.19+ 或 22.12+ 及 npm。

前端源码修改后：

```powershell
.\ReBS\start.ps1 -Rebuild
```

脚本不会结束占用端口的其他进程。若端口已占用，请先停止自己先前启动的服务。前端依赖发生变化时，先在 `ReBS/frontend` 执行 `npm.cmd ci`。

如执行策略阻止运行脚本，可在 `ReBS` 目录直接运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Push-Location frontend
npm.cmd ci
npm.cmd run build
Pop-Location
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

不存在虚拟环境时，先执行 `python -m venv .venv`。使用 `python -m pip` 避免依赖独立的 `pip.exe` 启动器。

## 本地开发

在 `ReBS` 打开两个终端：

```powershell
# 终端一
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

```powershell
# 终端二
Set-Location frontend
npm.cmd run dev
```

访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。Vite 同时代理 `/api` 的 HTTP 与 WebSocket 请求。

## 数据连接

默认上游：

| 用途 | 地址/频道 |
| --- | --- |
| REST | `https://openapi.okx.com` |
| 最新价与盘口 | `wss://ws.okx.com/ws/v5/public`：`tickers`、`books` |
| 实时 K 线与逐笔 | `wss://ws.okx.com/ws/v5/business`：`candle{周期}`、`trades-all` |

使用标准 443 端口，不使用欧易将停止接受的 8443 端口。REST 遇到临时连接/服务错误最多重试一次；默认全球公开域名失败时重试 `www.okx.com`。显式配置的区域域名不会自动切换。

本地默认继承当前进程的代理配置，不修改系统代理。服务器已使用欧易专用的 `OKX_PROXY_URL`：只对 OKX REST / WebSocket 生效，应用与代理两层只放行三个指定欧易域名，不改变其他应用、系统路由或 DNS。网络需要能访问欧易公开接口；未启用专用代理时，可按部署区域设置官方对应端点：

```powershell
$env:OKX_REST_URL = 'https://openapi.okx.com'
$env:OKX_WS_URL = 'wss://ws.okx.com/ws/v5'
.\start.ps1
```

REST 负责初始窗口、历史分页和快照，WebSocket 负责实时更新。每个浏览器标签页针对当前币对创建两路上游订阅，切换币对/周期时关闭旧订阅。当前适合个人本地看盘；多人部署需增加共享订阅和广播，以控制上游连接数。

## 字段与范围

- 现货成交量和挂单数量以基础币计，例如 BTC；成交额以计价币计，例如 USDT。小额成交保留小数，价格精度来自该币对的 `tickSz`。
- 24h 涨跌幅使用欧易 `open24h` 与 `last` 计算，属于滚动 24 小时统计。
- K 线响应中的 `confirmed=false` 表示尚在形成。1D/1W/1M 使用欧易 UTC+8 开盘口径，页面时间也显示 UTC+8。
- 最近窗口最多取 300 根，向前每页最多 100 根；分页游标严格排除边界时间戳，防止重复。
- 1 秒历史 K 线的回溯范围由欧易限制，当前官方说明为最近三个月；其他周期的可用历史以接口实际返回为准，不保证覆盖全部上市历史。
- 初始 REST 近期成交可能经过聚合，页面标记为 `REST · 近期成交（可能聚合）`；收到 `trades-all` 后清空这批快照，切换为逐笔成交，避免混用两种口径。
- 逐笔成交展示主动成交方向。`books` 是价位聚合的订单簿，无法由它还原每张委托的订单号、完整撤单记录或个人队列位置。
- 币对列表每 15 秒刷新；当前选中币对的最新价、盘口、成交与 K 线通过 WebSocket 更新。成交面板“暂停”只冻结显示，后台仍接收并保留最近数据。
- 服务只使用短时内存缓存并合并相同的并发 REST 请求；不会把旧 A 股数据或模拟数据用于数字货币行情。尚未建立长期逐笔数据库。

官方依据：[OKX API 文档](https://www.okx.com/docs-v5/en/)、[行情与增量盘口说明](https://www.okx.com/docs-v5/trick_en/)、[API 更新日志](https://www.okx.com/docs-v5/log_en/)。

## 本地 API

| 接口 | 用途 |
| --- | --- |
| `GET /api/health` | 本地服务状态；不代表外部欧易一定连通 |
| `GET /api/okx/markets` | 活跃现货币对、价格精度与 24h 行情 |
| `GET /api/okx/candles?instId=BTC-USDT&period=15m` | K 线；使用可选 `before` 毫秒游标加载更早历史 |
| `GET /api/okx/snapshot?instId=BTC-USDT` | 最新价、400 档盘口及近期成交快照 |
| `WS /api/okx/stream?instId=BTC-USDT&period=15m` | `status`、`ticker`、`book`、`trades`、`candle`、`heartbeat` 消息 |
| `GET /api/okx/demo/status` | 仅返回模拟账户是否配置，不返回凭据 |
| `GET /api/okx/demo/overview` | 资产、持仓、当前委托、近 7 日历史委托及近 3 日成交 |
| `POST /api/okx/demo/order` | 现货模拟下单；数量以基础币计，支持限价/市价 |
| `GET /api/okx/demo/order?instId=BTC-USDT&clOrdId=...` | 按唯一客户端委托号查询提交结果 |
| `POST /api/okx/demo/cancel` | 按币对及订单号撤销模拟现货委托 |
| `GET /api/strategy/catalog` | 因子、参数、函数和模板目录 |
| `POST /api/strategy/validate` | 校验策略参数、别名、公式、依赖；不执行交易 |
| `POST /api/strategy/preview` | 使用手动样本数值/序列试算条件；不是回测 |
| `GET/POST /api/strategy/drafts` | 鉴权查询/创建服务器草稿 |
| `GET/PUT/DELETE /api/strategy/drafts/{id}` | 鉴权读取/更新/删除指定草稿；更新及删除校验修订号 |

HTTP 参数与响应见运行后的 [API 文档](http://127.0.0.1:8000/docs)。WebSocket 的 `book` 消息已在后端合并为完整快照，包含盘口序列号与交易所时间戳。前端每 100ms 合并一次显示更新。

## 检查

在 `ReBS` 运行后端离线测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖行情字段、增量盘口、历史游标、缓存和错误处理，以及模拟盘签名、模拟标识、访问鉴权、精度校验、提交去重、超时查询和重启后避免重复提交；同时保留原 A 股后端回归测试。离线测试不验证第三方当前连通性，不会向欧易发送订单。

在 `ReBS/frontend` 运行类型检查和正式构建：

```powershell
npm.cmd run build
```
