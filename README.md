# QuarkNas

把自己的夸克网盘文件手动挑选后，下载到飞牛 fnOS（或任意 Linux NAS）的指定目录。

- 单用户、自托管，Docker Compose 一键部署
- 扫码登录：网页出示二维码，手机夸克 App 扫码确认后自动获取 Cookie，无需手动复制
- 网页端浏览夸克目录 → 勾选文件/文件夹 → 自动展开目录并排队下载；支持全盘搜索与容量显示
- 分段并发下载 + 断点续传（`.part` + 断点元数据）+ 原子落盘（`os.replace`）
- 小窗口请求：每条连接只取 64 MB 的 Range 窗口，写完立刻换连接续下一段（实测 512 MB 以上跨度会被 CDN 压到 90 KB/s）
- 低速自动扩容：单连接被源站限速时自动把分段一分为二，最多到 16 段；速度正常时不加连接
- 风控自适应：180 秒内连续 2 次 403 自动降并发（减半，最少 1）、分段压到 ≤2 并冷却 120 秒起（翻倍、封顶 600 秒）；冷却期暂停扩容，任务成功即清零，页面顶部显示冷却倒计时
- 同名文件策略可选：跳过（默认）/ 覆盖 / 保留二者
- 任务控制：暂停 / 恢复 / 取消 / 重试 / 置顶；支持批量操作与状态筛选；文件夹任务按批次分组，可折叠查看子文件进度
- 拖拽排序：拖动排队任务的左侧手柄即可调整下载顺序，顺序落库、刷新与重启后保持
- 队列顺序：按时间 / 大文件优先 / 小文件优先，手动置顶的任务始终最前
- 任务详情：每段进度、速度、重试次数与失败原因，随实时事件刷新
- 下载统计：今日/累计流量、成功率、最近 7 天柱状图与失败原因 Top 5，支持导出任务 CSV
- 观测：SSE 实时进度、单任务速度与 ETA、最近 10 分钟总速度曲线、日志页与脱敏诊断导出
- 限流与时段：全任务共享总限速；指定下载时段（支持跨午夜）
- 失败自动重试：指数退避（可配置次数与起始间隔），磁盘不足等不可重试错误直接失败
- 体验：设置页即时生效并持久化、设置一键导入/导出（JSON）、移动端适配、刷新后保持目录与选中状态
- 磁盘水位保护：剩余空间不足时拒绝新任务
- 自动清理：按保留天数定时清除已完成/失败任务记录（只删记录，不删文件）

## 快速开始

```bash
cd QuarkNas
# 修改 docker-compose.yml 里的落盘路径（左侧 /vol1/1000/Downloads）
docker compose up -d --build
```

也可以直接用 Docker Hub 上的预构建镜像，省去本地构建（`<用户名>` 换成实际发布者）：

```bash
QUARKNAS_IMAGE=<用户名>/quarknas:latest docker compose pull
QUARKNAS_IMAGE=<用户名>/quarknas:latest docker compose up -d
```

浏览器打开 `http://<NAS-IP>:18080`，点击右上角「扫码登录」：

网页自动生成二维码，用手机夸克 App 扫码并在手机上确认，服务端自动取回 Cookie 落库，无需手动复制。

> 夸克没有公开下载 API，本程序复用网页会话。直链有效期很短且绑定 UA，所以下载必须由本服务代理完成，无法交给 aria2/qBittorrent 直接下载。

## 配置项

`docker-compose.yml` 的 `environment`：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOWNLOAD_ROOT` | `/downloads` | 容器内落盘根目录，与宿主机卷映射对应 |
| `CONFIG_DIR` | `/config` | SQLite 与配置目录 |
| `AUTH_PASSWORD` | 空 | 非空时网页启用 HTTP Basic 认证，建议内网也设置 |
| `SEGMENTS` | `4` | 单文件分段数初始值，`1` 为顺序下载；非会员建议 2~4 |
| `CONCURRENCY` | `2` | 同时下载的文件数初始值，夸克风控敏感，建议 1~2 |
| `LOW_WATERMARK_GB` | `2` | 剩余空间低于该值时拒绝新任务的初始值 |
| `TMDB_LANGUAGE` | `zh-CN` | 影视整理校正后的片名语言：`zh-CN` / `zh-TW` / `en-US` |
| `RETRIES` | `3` | 单个分段的重试次数 |
| `REQUEST_TIMEOUT` | `30` | 单次请求超时（秒） |

环境变量只提供**首次启动的默认值**；之后以数据库中的设置为准，可在网页「设置」里修改，保存后即时生效（无需重启）：

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| 单文件分段数 | `4` | 1–16，1 表示顺序下载；慢源站会自动扩容到 16 段 |
| 并发下载数 | `2` | 1–8，同时进行的文件数 |
| 队列顺序 | 按时间 | 按时间 / 大文件优先 / 小文件优先；手动「置顶」的任务始终排在最前 |
| 同名文件 | 跳过 | 跳过 / 覆盖 / 保留二者 |
| 磁盘水位（GB） | `2` | 剩余空间低于该值拒绝新任务 |
| 总限速（MB/s） | `0` | 0 为不限速，所有任务共享 |
| 下载时段 | 关闭 | 支持跨午夜，如 23:00–07:00 |
| 失败自动重试 | `0` 次 | 0 为关闭；按 2 倍指数退避，单次最长 10 分钟 |
| 自动清理任务记录 | `0` 天 | 0 为不清理；超过天数的已完成/失败记录会被删除，只删记录不删文件 |

`user: "${PUID:-1000}:${PGID:-1000}"` 控制落盘文件属主，按飞牛上的用户改 `PUID/PGID`。

## 自动构建镜像

[.github/workflows/docker.yml](.github/workflows/docker.yml) 在推送到 `main`、打 `v*` 标签或手动触发时，用 buildx 构建 `linux/amd64` + `linux/arm64` 双架构镜像并推送到 Docker Hub。

在仓库 Settings → Secrets and variables → Actions 里配置两个 Secret：

| Secret | 说明 |
| --- | --- |
| `DOCKERHUB_USERNAME` | Docker Hub 用户名，同时作为镜像名前缀 |
| `DOCKERHUB_TOKEN` | Docker Hub Access Token（Account Settings → Security 新建，权限选 Read & Write 即可） |

镜像标签：`latest`（默认分支）、分支名、`v1.2.3` 对应的 `1.2.3` 与 `1.2`、提交短 SHA。

## 本地开发

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux: source .venv/bin/activate
pip install -r requirements.txt

$env:DOWNLOAD_ROOT=".devdata/downloads"; $env:CONFIG_DIR=".devdata/config"
uvicorn app.main:app --reload --port 8080
```

接口文档：`http://127.0.0.1:8080/docs`

## M0 能力探测

在真实下载前，先用独立脚本确认 Cookie、直链、Range 与多连接速度：

```bash
pip install httpx
python tools/probe.py --cookie-file cookie.txt --speed-mb 32 --threads 8 --resume-test
```

它会输出：账号是否有效、直链主机、是否支持 `206` 分段、单连接 vs 多连接速度、断点续传是否生效。建议把结果作为调整 `SEGMENTS` / `CONCURRENCY` 的依据。

## 目录结构

```
app/
  main.py            FastAPI 入口、Basic Auth、启动/关闭钩子
  settings.py        环境变量配置（仅作默认值）
  config_store.py    运行期设置：校验、持久化、即时生效
  db.py              SQLite（WAL）封装与表结构
  paths.py           文件名清洗与目标路径防逃逸
  events.py          SSE 事件总线
  runtime.py         应用上下文（配置 / 数据库 / 队列）
  logbuf.py          内存日志环形缓冲与脱敏
  quark/client.py    夸克接口客户端（列目录 / 搜索 / 直链 / 账号 / 容量）
  quark/auth.py      Cookie 存取、校验与失效处理
  quark/qrlogin.py   二维码登录：出码、轮询扫码、换 Cookie
  download/engine.py 分段下载、续传、原子落盘
  download/queue.py  任务状态机、并发 worker、限速、时段、进度上报
  api/               REST 接口（files / tasks / account / settings / logs / events）
web/                 前端页面（原生 HTML/CSS/JS，无构建步骤）
tools/probe.py       M0 探测脚本
```

## 设计要点

- **状态机**：`pending → resolving → downloading → verifying → done`，异常分支为 `paused / failed / canceled / skipped`；进程重启后自动把中断任务恢复为 `pending` 并续传。Cookie 失效时自动转 `paused`，重新登录原账号后可恢复。
- **原子落盘**：始终先写 `<目标>.part`（与目标同目录、同文件系统），校验字节数后再 `os.replace`，避免半截文件出现在媒体库目录。
- **风控意识**：直链短时有效、并发过高会被限速或 403。因此默认 `CONCURRENCY=2`、`SEGMENTS=4`，遇到 403/404 直接失败而不是无限重试。
- **注入防护**：夸克文件名会清洗非法字符并做截断，目标路径强制落在 `DOWNLOAD_ROOT` 内。

## 尚未包含

- 定时/自动同步、分享链接转存、上传、重命名规则、媒体库刮削
- 多账号
- 应用内自更新（容器场景请重新拉取镜像）
- fnOS `.fpk` 打包（当前用 Docker Compose）

## 排障

- 大文件下载慢（约 0.8 MB/s）：「单次 Range 跨度」越大越慢——同一偏移下 8–256 MB 跨度约 2 MB/s，512 MB 以上只剩 90 KB/s。引擎已改为 64 MB 小窗口续传，无需手动调参；若仍慢，先确认是整体降速（见下一条）。
- 所有任务一起变慢、新建连接也只有 0.1 MB/s：连续大流量后会出现账号级、可恢复的通道降速，暂停下载几分钟即恢复，加并发无效。
- 下载速度上限约 5 MB/s：这是夸克网页通道的账号级限速，加并发无法突破，详见 [参考项目与借鉴清单](docs/参考项目与借鉴清单.md) 第六节。
- 任务因「Cookie 失效」暂停：重新扫码登录后点「恢复」即可续传。
- 页面「日志」可筛选级别与关键词；「导出诊断」会生成脱敏 JSON（不含 Cookie / token / 文件内容）。
