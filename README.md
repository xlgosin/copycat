# CopyCat

独立 Python 服务 + 原生 HTML/CSS/JavaScript，跟随熬鹰资本（5075281354358777856）的新成交。

## 本地启动

需要 Python 3.10+（macOS/Linux）。默认使用本项目自带的独立采集器，不需要再运行旧的 `binance-copy-monitor`。

```bash
cd /Users/gosinzheng/Documents/WinQuant/CopyCat
sh start.sh
```

首次自动创建虚拟环境并安装依赖。打开 http://127.0.0.1:8010 ，输入启动日志中的控制口令。口令保存在 `data/admin-token`。默认模拟、默认暂停；源仓位基线验证通过后，点击“开始跟单”接收新信号。模拟无需币安密钥，但需要访问币安公开合约行情接口。

本项目的进程、页面、配置和交易账本独立。当前部署使用下面的自带爬虫和 `data/source.db`，无需同时保留旧采集容器。CopyCat 每5秒读取数据库，自带网页采集器默认每10秒发起一轮采集；实际完成时间还取决于网页加载和接口响应，并非交易所实时推送。

## 不依赖旧项目的独立采集

先按上述方式创建环境，再安装可选浏览器依赖：

```bash
.venv/bin/python -m pip install -r requirements-collector.txt
.venv/bin/python -m playwright install chromium
```

Linux 缺少系统浏览器依赖时按 Playwright 提示安装；可使用 `python -m playwright install --with-deps chromium`。在 `.env` 配置：

```dotenv
SOURCE_DB=data/source.db
STANDALONE_SOURCE_DB=data/source.db
SOURCE_POLL_SECONDS=10
SOURCE_RESTART_AFTER_SECONDS=60
```

先运行 `.venv/bin/python collect.py --once` 验证页面和金额可读取；成功后，一个终端运行 `.venv/bin/python collect.py`，另一个终端运行 `sh start.sh`。自带爬虫沿用原项目的公开网页+订单JSON采集方式，只采集熬鹰。首次从本周或更早的基线时间开始读取；后续采集覆盖上次成功时间并重叠15分钟，跨周和停机后也保留该边界。每轮最多2,000条，超过时拒绝发布不完整快照。连续60秒没有成功采集时，采集进程主动退出，并由 systemd 重启容器；CopyCat 在数据过期期间仍保持暂停。公开接口本身仍可能延迟或遗漏，不能保证完整跟随。它不绕过登录、地区和反爬要求；目标服务器的页面可达性需要实际验证。

### 源仓位基线（启动必需）

本周历史无法证明周初没有持仓。独立采集器明确发布 `history_complete: false`，不会仅凭本周记录启用交易。必须先从可信来源核实该交易员在某个时刻的**全部源持仓**，将快照保存为 `data/source-baseline.json`，然后配置 `SOURCE_BASELINE_FILE=data/source-baseline.json`：

```json
{
  "portfolio_id": "5075281354358777856",
  "as_of": "2026-09-05T00:00:00+00:00",
  "positions": {"ETHUSDT:LONG": "600", "BTCUSDT:SHORT": "2"}
}
```

以上是格式示例，不是真实持仓。`as_of` 必须带时区，数量是源账户的合约数量；快照应包含截至该时刻的成交，未列出的方向表示已核实为零。只有确认全部空仓时才能使用空对象。采集窗口必须覆盖该时间，系统将以快照加上之后的成交建立基线，快照中已有的持仓等待归零后才参与。不要用本系统持仓代替源账户持仓，也不要为了启动而填写猜测数据。

原爬虫接入需在 `runtime_state` 的采集状态中提供可信的 `history_complete: true`（仅限从已知空仓起点开始的完整历史），或在账户 `data_json` 中提供上述 `position_baseline`，并在采集状态中提供带时区的 `history_window_start` / `history_window_end`。缺少完整性依据会保持暂停。

### 升级与记录保存

旧 JSON 账本中仍保留的成交、已处理事件和通知会自动迁移到独立表，迁移与状态更新在同一事务中完成。已有版本此前截掉的早期记录无法恢复。新记录不再受1,000条上限影响，页面可查看更早记录，累计毛盈亏使用 Decimal 字符串保存。

旧账本缺少已验证源基线时会锁定启动；已有持仓及待确认订单仍保留，待确认订单仍可查询。请先按下文人工恢复流程核对旧账户，保留数据库备份，再用可靠源基线重新初始化。不要直接删除有持仓的账本，也不要把新版本迁移后的数据库交给旧版本运行。

2026-09-05本机实测：网页带单余额可以读取，订单JSON接口返回 `11012005`（系统繁忙），因此没有发布可用于执行的新信号。需要等源接口恢复并在目标服务器验证；目前没有完成测试网/真实账户下单验证。

## 资金与比例

- 本金默认100 USDT；比例放大由 `COPY_MULTIPLIER` 决定（最多3）；合约杠杆由 `FUTURES_LEVERAGE` 决定（最多5）；开仓名义敞口上限由 `MAX_GROSS_NOTIONAL` 决定，不得超过本金×杠杆（100U×5倍=500U）。这些是开仓前限制，价格上涨后的名义金额可以超过该上限。
- 开仓数量 = 源成交数量 × 100 / 源带单保证金余额 × 3。这里使用爬虫字段 `margin_balance`，并非包括跟单资金的 `aum`，也不能保证网页数值与交易所逐秒权益一致。
- 开仓与平仓均使用 **MARKET**；平仓带 `reduceOnly`。不再使用源成交均价限价 IOC，也不再因价格偏离跳过开仓；市价可能有滑点。
- 市价未完全成交等异常终态仍会暂停并查询原订单号。
- 平仓数量 = 本系统该方向剩余数量 × 源平仓数量 / 源平仓前推算数量。向下取整满足数量规则；不足最小数量或源记录不完整时不强行下单。有剩余仓位但漏平信号时会锁定自动运行，须人工处理。
- 实盘只支持 USDT 永续、单向持仓、单资产保证金，并将跟出的合约设置为逐仓3倍。同币种已有反向仓位时跳过新开仓。
- 市价订单可能有滑点，3倍放大不保证收益或100U最大亏损。请使用仅为 CopyCat 准备、约100U余额的独立合约账户，不与手工或其他机器人交易混用。

例：熬鹰余额300,000U，开仓名义金额30,000U，本系统按1倍跟单开仓约10U名义金额；5倍合约杠杆需约2U保证金，另需费用。

## 配置密钥和实盘

将 `.env.example` 复制成 `.env`（勿提交版本库），填写：

```dotenv
BINANCE_API_KEY=你的Key
BINANCE_API_SECRET=你的Secret
COPYCAT_MODE=testnet
```

测试网需要测试网账户密钥。服务仅向 Binance 官方固定域名发送密钥。服务器时间须同步。密钥只开启所需的合约读取/交易权限，不开提现权限，建议设置服务器IP白名单。不要把密钥发在聊天或填入网页。

确认后实盘配置：

```dotenv
COPYCAT_MODE=live
LIVE_TRADING_ENABLED=true
```

重启后仍暂停，需要在网页确认启动实盘跟单。修改模式/密钥后务必先结束旧模式仓位。本版不是托管服务，尚未使用你的真实账户验证交易权限或真实成交。

默认监听 `0.0.0.0:8010`（部署脚本会写入服务器 `.env`），浏览器访问 `http://服务器IP:8010`。公开部署请保留控制口令并视情况加 HTTPS/防火墙；当前内置 Web 面向单用户控制台。只能运行一个 `python app.py`，不要配置多个执行进程；启动时文件锁防止重复执行。

## 信号处理与异常

- 首次将已有历史记录设为基线，不追单。源已有仓位的周期等待归零后再参与。源仓位来自公开历史推算，无法保证完整。
- 重启默认暂停，但已由操作员启用的会话会在源恢复后自动继续。采集器与跟单暂停相互独立，采集器持续保存源订单；尚未完全成交的源订单暂不发布，变成 FILLED 后再按同一订单 ID 发布。每个源订单以订单 ID 幂等处理：已处理 ID 忽略，未处理 ID 在恢复后按顺序执行；余额、精度、保证金或敞口不足时只记录跳过，不暂停后续订单。暂停请求即时设置停止标志，不等待网络调用；已经进入提交阶段的单笔订单仍须按唯一客户订单号确认结果，避免重复下单。
- 每次下单前把意图和确定性客户订单号写入SQLite；网络超时不重复发单，后台按原订单号自动查询，无需手动核对。部分成交按实际成交数量入账；IOC正常结束可继续，其他异常终态按实际成交量入账后自动恢复。
- 如果查询返回“订单不存在”，保持暂停；不要靠重新发送市价单来试探。须在币安确认该订单和当前持仓，再按下述人工恢复流程处理。
- 平仓仅部分成交且仍有剩余仓位时，锁定自动启动，人工核对后恢复。账本写入失败时停止执行并禁止本进程继续核对/启动；恢复存储后重启，按最后成功保存的订单意图核对，不重新发送原订单。
- 接口429/418/403进入冷却；数据超时、缺少余额或执行异常暂停。源开仓价偏离最新标记价超过1%跳过开仓。该检查不能限制实际市价单滑点。
- 为了100U预算，不向上凑最小下单金额，低于合约规则会跳过，可能与源交易产生差异。
- 页面利润是按成交账本计算的平仓毛盈亏，不含手续费、资金费；模拟限价买单要求标记价≤委托价，卖单要求标记价≥委托价，满足时按标记价全额模拟，否则未成交自动取消；不模拟清算/订单簿深度/费用。

人工恢复：停止 CopyCat；在币安检查该独立账户的真实持仓、普通/条件委托和客户订单号，手动处理遗留仓位与委托；确认账户空仓无委托后，将 `data/live.db`（模拟为 `paper.db`，测试网为 `testnet.db`）改名备份，再启动建立新基线。不要在仍有持仓时清空账本。改变账户、本金或比例需同样核对并归档旧账本。

## 钉钉通知

直接在 CopyCat 自己的 `.env` 中配置，不再读取其他项目的 `.env`，也不向界面返回密钥：

```dotenv
DINGTALK_ENABLED=true
DINGTALK_WEBHOOK=你的钉钉机器人Webhook
DINGTALK_SECRET=你的加签Secret
```

部署时将配置保存在服务器的 CopyCat `.env` 中，修改后重启生效。未填写 Webhook 时不发送通知。

通知覆盖开仓成交、平仓成交、跳过/拒绝订单、异常暂停和待确认订单。每条包含 CopyCat、熬鹰跟单、运行模式、合约方向、原因；已确认成交附数量、价格、客户订单号，平仓附本次毛盈亏。模拟通知明确标记“模拟”，待确认通知不会把请求数量写成成交数量。历史基线不补发通知。

通知与交易结果一起保存到SQLite，独立线程每4秒至多发送一条，失败退避重试；发送失败不改变交易状态。重复的持续异常、同一笔待确认订单不重复入队。通知服务超时后重试可能重复到达，可用消息中的通知编号识别。队列会跨重启保留，模式切换后需要运行原模式才能继续发送该模式的待发通知。

页面“运行控制”显示钉钉是否启用、待发条数及发送错误。不自动发送测试成交消息；要停通知可配置 `DINGTALK_ENABLED=false`。

## HTTP 接口

所有 `/api/*` 要求 `Authorization: Bearer 控制口令`。POST 需 `Content-Type: application/json`。

- `GET /api/status`：运行模式、源金额、持仓、最近记录、异常。
- `GET /api/records?limit=100&before=序号`：完整历史分页；首次省略 `before`，后续使用返回的 `next_before`，为 `null` 时无更多记录。
- `POST /api/start`：启动跟单。
- `POST /api/stop`：暂停所有自动交易，不平仓。
- `POST /api/reconcile`：兼容旧客户端的订单结果查询接口；正常运行由后台自动查询，无需页面操作。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

### 部署为 systemd 服务

本机需有 `ssh` 与 `rsync`（密码从 `deploy.env` 自动喂给 SSH，无需 sshpass）。凭证写在 gitignore 的 `scripts/deploy.env`：

```bash
cp scripts/deploy.env.example scripts/deploy.env
# 编辑 deploy.env：DEPLOY_HOST / DEPLOY_USER / DEPLOY_PASSWORD
bash scripts/deploy.sh
# 覆盖服务器 .env： bash scripts/deploy.sh --env
# 只部署跟单、不装采集器： bash scripts/deploy.sh --no-collector
```

日常分别更新：

```bash
bash scripts/update.sh env        # 或 .env —— 上传本地 .env 并重启
bash scripts/update.sh baseline   # 或 基线 —— 上传 data/source-baseline.json
bash scripts/update.sh app        # 或 带单 —— 同步跟单代码并重启 copycat
```

会在远程安装并启用 `copycat.service` 与 `copycat-collector.service`（默认目录 `/opt/copycat`）。
CentOS 7 等旧系统会自动用 Docker 跑采集器。控制台默认 `HOST=0.0.0.0`，浏览器打开：

```text
http://服务器IP:8010
```

日志上限（部署时写入）：
- journald：约 300MB（`scripts/journald-copycat.conf`）
- Docker 采集器：单文件 20MB × 5（unit 里 `--log-opt`）
- `logs/*.log`：logrotate 单文件约 20MB（`scripts/logrotate.copycat`）

### AlphaFox 延迟探针

只读检查公开策略摘要以及登录后可见的订单、仓位和信号源仓位。首次匿名验证：

```bash
.venv/bin/python scripts/alphafox_probe.py --once
```

私有接口返回 401 时，将自己已登录 AlphaFox 会话的完整 `Cookie` 请求头放入本机 `.env` 的
`ALPHAFOX_COOKIE`（不要发给他人或提交版本库），再持续采样：

```bash
.venv/bin/python scripts/alphafox_probe.py --interval 5
```

结果保存在 `data/alphafox-probe.db`。`probe_runs` 保存每次请求耗时，`snapshots` 只在接口内容变化时
保存首次发现时间和原始 JSON，供后续与币安成交时间比较。探针不会调用 CopyCat 交易引擎。

安装了 Node.js、Playwright 和相应浏览器时，可运行 `node tests/frontend_check.cjs` 验证前端超时恢复、操作互斥、暂停和历史分页。使用本机 Chrome 可设置 `PLAYWRIGHT_CHANNEL=chrome`。该测试拦截全部请求并返回本地模拟数据，不启动交易服务。

官方接口参考：[新订单与平仓参数](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order)、[请求异常与不确定执行状态](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)。
