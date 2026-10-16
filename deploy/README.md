# SSH 部署

当前部署：`http://124.220.52.44/`，通过 `ssh ubuntu` 维护。已验证公网网页、静态资源、健康接口与 WebSocket 反向代理；`rebs` 和 `nginx` 均已设为开机启动。

2026-10-15 已使用用户提供的 VMess 节点配置欧易专用代理。上游配置只保存在服务器私有文件，不进入发布包。

当前应用目录由 `/opt/rebs/current` 软链接决定；用 `readlink -f /opt/rebs/current` 查看实际版本。

部署目标由用户指定的 `ssh ubuntu` 别名决定。使用 Ubuntu 24.04、Nginx 和 systemd；后端只监听 `127.0.0.1:18080`，Nginx 通过 80 端口提供网页并转发 WebSocket。

发布包包含后端、已构建前端、前端源码、依赖声明、测试与部署配置；不包含本地虚拟环境、行情 CSV、缓存、日志或密钥。

## 服务器布局

- `/opt/rebs/releases/<release-id>`：独立版本及 Python 虚拟环境。
- `/opt/rebs/current`：当前版本软链接，便于回滚。
- `/etc/rebs/rebs.env`：可选的服务环境变量，权限 `0640`，不放入发布包。
- `/var/lib/rebs`、`/var/cache/rebs`：运行状态与缓存。
- `/etc/systemd/system/rebs.service`：以独立 `rebs` 用户运行。
- `/etc/nginx/sites-available/rebs`：反向代理与静态资源缓存。

安装系统依赖后，把发布包及 `install.sh` 上传到 `/tmp`，执行：

```bash
sudo bash /tmp/rebs-install.sh RELEASE_ID /tmp/rebs-RELEASE_ID.tar.gz
```

安装器先安装依赖并运行离线测试，再切换当前版本；启动验证失败时尝试恢复上一个版本。不会删除历史发布目录。

## 运行维护

```bash
sudo systemctl status rebs nginx
sudo journalctl -u rebs -n 100 --no-pager
sudo systemctl restart rebs
curl -fsS http://127.0.0.1/api/health
```

`/api/health` 只验证应用运行。欧易行情需另行测试 `/api/okx/markets` 与 `/api/okx/stream`。服务器需能连接欧易 REST 和公共/业务 WebSocket。

服务器目前使用项目专用的 HTTP CONNECT 代理。`/etc/rebs/rebs.env` 仅设置应用专用变量，不设置全局 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`：

```text
OKX_PROXY_URL=http://127.0.0.1:18081
```

应用的 OKX 客户端显式使用此代理，并关闭该客户端对通用代理环境变量的继承。配置专用代理后，仅允许 `openapi.okx.com`、`www.okx.com`、`ws.okx.com` 的标准 TLS 端口；其他目标会被应用拒绝。

代理自身的路由也只放行上述三个完整域名，其余目标使用 `blackhole` 拒绝。没有 TUN、透明代理、路由表或系统 DNS 修改。

专用代理维护：

```bash
sudo systemctl status rebs-okx-proxy
sudo systemctl restart rebs-okx-proxy
sudo journalctl -u rebs-okx-proxy -n 50 --no-pager
```

- 代理核心位于 `/opt/rebs-proxy/vendor/usr/bin/v2ray`，来自 Ubuntu 安全更新仓库，只解压到专用目录，未启用发行包的全局代理服务。
- 代理只监听 `127.0.0.1:18081`，以无网络管理能力的独立 `rebs-proxy` 用户运行，并设置开机自启。
- 节点配置位于 `/etc/rebs-proxy/config.json`，权限 `0640`，只允许 root 和代理服务组读取。不要打印到日志或复制进项目。
- 配置生成器 `configure-okx-proxy.py` 可在服务器上读取用户提供的节点二维码；需要 `zbarimg`，当前支持 VMess JSON 和旧版分享 URI。
- `/var/lib/rebs-proxy/network-baseline.json` 保存配置前的环境文件、DNS 文件哈希和默认路由，用于核对全局网络未改变。
- `rebs` 服务的 systemd drop-in 声明代理启动顺序。应用停止不会关闭专用代理；代理中断时网页会按既有逻辑重连。

正式启用域名后，可在 Nginx 上另行配置对应 HTTPS 证书。

## 欧易模拟账户

策略工作台入口为 `/#strategy`。浏览器草稿无需口令；服务器保存沿用下述网页访问口令和 HTTPS/SSH 访问要求。服务将策略配置持久保存在 `/var/lib/rebs/strategies.sqlite3`（`REBS_STRATEGY_STATE_PATH`），与发行目录分离，部署和重启保留草稿。备份时应使用 SQLite 备份机制，避免在写入中只复制主文件而遗漏 WAL。此阶段不会启动策略、回测或自动下单。

账户功能固定发送 `x-simulated-trading: 1`，只连接欧易全球 REST 官方域名。支持资产、持仓、当前委托、历史委托与成交查询，以及现货 `cash` 模式的限价/市价下单和逐单撤销。市价单的数量固定为基础币数量（`tgtCcy=base_ccy`），不自动缩减委托数量。现货持有量查看资产；持仓接口用于衍生品持仓查询，当前页面不支持衍生品下单。

在欧易「模拟交易 → 个人中心 → 模拟交易 API」创建专用 API Key，开启读取和交易权限。不要把真实账户密钥用于此处，也不要把密钥发到聊天、代码仓库或浏览器。按欧易要求设置 API IP 白名单；该项目访问欧易时的出口为专用代理出口。

在本机终端执行交互式配置，输入不会回显，也不会写入命令历史：

```powershell
ssh -t ubuntu "sudo python3 /opt/rebs/current/deploy/configure-demo.py"
```

依次输入 Demo API Key、Secret Key、Passphrase，再设置网页访问口令（24–128 个可打印 ASCII 字符，建议使用密码管理器生成）。三个 API 凭据保存在 `/etc/rebs/rebs.demo.env`，权限 `root:rebs 0640`；网页只使用单独的访问口令，不接收 API 密钥。配置脚本重启服务后只验证账户查询，不发送测试订单。

账户状态和行情可通过公网 HTTP 查看；私人资产、委托和交易接口要求 HTTPS 或 SSH 本地转发。当前服务器尚未配置域名与 HTTPS，可在本机运行：

```powershell
ssh -N -L 127.0.0.1:18000:127.0.0.1:18080 ubuntu
```

保持该终端运行，在浏览器访问 `http://127.0.0.1:18000/`，点击顶部「模拟交易」，用刚设置的网页访问口令解锁。访问口令只在页面内存中保留；刷新或锁定后需要重新输入。账户查询每 15 秒更新；页面隐藏时暂停轮询。历史委托为近 7 天最近 50 条，成交为近 3 天最近 50 条，当前委托最多 100 条；此处不保证完整分页归档。

下单与撤单提交前展示确认窗口。写请求不会因网络超时而自动重发；下单使用唯一客户端委托号，并在 `/var/lib/rebs/demo-orders.sqlite3` 持久保存提交记录，防止重试或服务重启造成同号重复提交。结果不明时页面保留委托号，可查询或到欧易模拟盘核对。撤单回执只表示请求已受理，最终状态以刷新后的委托记录为准。应用与代理不修改全局流量设置。
