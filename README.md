# NAS Music Kit Agent API

把 NAS 音乐播放器的搜索、下载、补歌与媒体库清洗能力封装成稳定的 HTTP 服务，方便 Trae Skill、榜单程序或其它自动化程序调用。

- `GET /api/search`：按关键词搜索歌曲
- `POST /api/download`：按歌曲信息触发下载（由上游完成落盘与标签写入）
- `POST /api/library/backfill`：批量补齐缺失歌曲，自动下载、清洗并触发 Navidrome 扫描
- `POST /api/playlist/import`：给歌单/榜单链接，自动抓取、匹配并创建 Navidrome 播放列表
- `POST /api/charts/refresh`：立即刷新榜单，支持 `dry_run` 预览
- `GET /api/charts/status`：查看最近一次榜单运行状态和日志
- `GET /dashboard`：监控大盘，查看上游状态、限流、任务队列和榜单日志
- `GET /api/charts/report`：下载最新未匹配清单
- `POST /api/library/organize`：按 `Artist/Album/Artist - Title` 清洗媒体库
- 接口地址、鉴权信息都通过环境变量配置，镜像可直接复用

默认镜像只包含 agent API；`Dockerfile.bundle` 还会把原版 `juneix/nas-music-kit` 打进同一容器，适合单容器部署。

> 上游 `/api/download` 只返回下载结果元数据（`filename`、`status`、`tags`），音频由上游客端写入 NAS 音库；本服务忠实透传该响应。
>
> 注意：公共的 `music-api.gdstudio.xyz` 只提供旧的 `api.php` 接口，没有 `/api/search`、`/api/download`。本服务需要把 `MUSIC_API_BASE_URL` 指向自建音乐服务（例如 NAS 上的部署地址）。

## 接口

### 搜索

```http
GET /api/search?source=netease&name=小方桌&pages=1
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `source` | 是 | 音源，如 `netease` |
| `name` | 是 | 搜索关键词，支持中文 |
| `pages` | 否 | 页码，从 1 开始，默认 1 |

返回与上游一致的 JSON 数组，字段包括 `id`、`url_id`、`lyric_id`、`pic_id`、`name`、`artist`、`album`、`source`。

### 下载

```http
POST /api/download
Content-Type: application/json
```

```json
{
  "source": "netease",
  "id": "3412527335",
  "name": "掉漆小方桌",
  "artist": "小春",
  "album": "",
  "pic_id": "109951173651495877",
  "br": "999",
  "lyric": true,
  "vip": false
}
```

| 字段 | 说明 |
| --- | --- |
| `source` / `id` | 取搜索结果里的 `source` 与 `id` |
| `name` / `artist` / `album` / `pic_id` | 用于文件名和 ID3 标签；`artist` 是字符串，多歌手自行拼接 |
| `br` | `999` 表示最高可用音质，也可传具体码率如 `320` |
| `lyric` | 是否写入歌词 |
| `vip` | 是否使用 VIP 凭据 |

成功响应：

```json
{"bitrate": "999", "filename": "小春 - 掉漆小方桌.mp3", "status": "success", "tags": "ok"}
```

响应中的 `bitrate` 是请求值回显，不代表实际码率。

### 自动补歌

```http
POST /api/library/backfill
Content-Type: application/json
```

```json
{
  "tracks": [
    {"title": "小方桌", "artist": "小春", "source": "netease"}
  ],
  "organize": true,
  "scan": true
}
```

返回 `202`，任务在后台串行执行：

```json
{"job_id": "a1b2c3d4", "status": "queued", "total": 1}
```

查询进度：

```http
GET /api/jobs/{job_id}
```

任务流程为 `搜索 → 匹配 → 下载 → 清洗 → Navidrome 扫描`，重试同样计入上游限流。

### 导入歌单/榜单链接

```http
POST /api/playlist/import
Content-Type: application/json
```

```json
{
  "urls": ["https://music.163.com/playlist?id=3778678"],
  "backfill": true,
  "scan": true
}
```

支持网易云、QQ音乐、Spotify、汽水音乐，多个链接可一次传入。返回示例：

```json
{
  "library_songs": 1200,
  "playlists": [
    {"platform": "netease", "name": "测试歌单", "total": 100, "matched": 80, "missing": 20, "playlist_id": "..."}
  ],
  "missing_tracks": [{"title": "温柔", "artist": "五月天", "source": "netease"}],
  "backfill_job_id": "...",
  "backfill_total": 20
}
```

可选字段：

- `dry_run=true`：只抓取和匹配，不建列表、不补歌
- `loose=true`：宽松匹配
- `proxy=true`：Spotify/汽水走 `IMPORT_PROXY_URL`
- `backfill=false`：只导入列表，不自动补歌
- `name`：自定义列表名，仅单个链接可用

有 `backfill_job_id` 时用 `GET /api/jobs/{job_id}` 查询补歌进度；补歌完成后再调用一次导入接口，列表会重建并包含新歌。

### 清洗媒体库

```http
POST /api/library/organize?dry_run=true
```

整理为：

```text
/library/Artist/Album/Artist - Title.mp3
/library/Artist/Album/Artist - Title.lrc
```

`LIBRARY_DIR` 默认 `/music`，推荐在 compose 里设为 `/library`。

### Navidrome 扫描

```http
POST /api/navidrome/scan?wait=true
```

### 健康检查

```http
GET /healthz
```

### 监控大盘

浏览器打开：

```text
http://NAS:8080/dashboard
```

大盘每 5 秒自动刷新，展示：

- 上游音乐服务是否可达、HTTP 状态码与延迟
- 本地滑动窗口限流用量、剩余额度
- 补歌队列：执行中 / 排队 / 已完成 / 失败数量，worker 状态
- 当前与最近任务：阶段（搜索/下载、清洗、扫描）、进度、下载/未找到/失败数、当前曲目
- 最近失败与未匹配歌曲明细
- 榜单任务状态、补歌任务 id、未匹配清单下载、最近日志

数据接口为 `GET /api/dashboard/status`，返回 JSON，便于接入其它监控。启用 Basic 认证后，浏览器打开大盘会先弹出登录框。

页面还提供与 [SKILL.md](./skill/nas-music-kit/SKILL.md) 对应的快捷操作：

- 搜索并下载单曲（先选音源、关键词，再在结果里点下载）
- 导入歌单/榜单链接（支持仅预览、宽松匹配、走代理、自动补歌、补歌后扫描）
- 批量补歌（每行 `歌名 - 歌手`，后台串行执行）
- 媒体库清洗（预览/执行）、触发 Navidrome 扫描
- 立即刷新榜单（可勾选仅预览）

这些操作和 HTTP API 权限一致，服务本身没有鉴权；只在内网使用，不要把 `8080` 直接暴露到公网。

## API 工作流程

### 1. 搜索与单曲下载

```text
GET /api/search
  → 返回候选列表
POST /api/download
  → juneix 下载并写入 /music/<歌手 - 歌名>.mp3
  → 此时还没进媒体库
POST /api/library/organize
  → 从 /music 移动到 LIBRARY_DIR/Artist/Album/
```

下载接口只返回元数据，真正的文件在 `/music`；需要整理时再调用 organize，补歌任务会自动执行这一步。

### 2. 批量补歌（异步）

```text
POST /api/library/backfill
  → 返回 202 + job_id
  → 后台单 worker 串行执行：
      searching：搜索 → 匹配 → 下载到 /music
      organizing：扫描 /music，移动到 LIBRARY_DIR
      scanning：调用 Navidrome startScan
GET /api/jobs/{job_id}
  → 查询 status / stage / processed / downloaded / not_found / failed
```

- 任务串行执行，重试也计入上游限流
- `not_found` 表示上游没有可信匹配，不会自动换关键词重试
- 只有 `status=completed` 才算结束

### 3. 导入外部歌单

```text
POST /api/playlist/import
  → 解析链接，抓取曲目
  → 读取 Navidrome 全库并匹配
  → 已匹配的歌曲立即创建/重建播放列表
  → 未匹配歌曲放进 missing_tracks，并提交 backfill 任务
  → 返回 backfill_job_id
GET /api/jobs/{backfill_job_id}
  → 补歌完成后自动清洗并扫描
再次调用 POST /api/playlist/import
  → 重建播放列表，包含刚补进来的歌
```

### 4. 清洗与扫描

```text
POST /api/library/organize?dry_run=true
  → 只统计 planned，不移动
POST /api/library/organize
  → 移动文件、修复乱码标签、歌词转 UTF-8
POST /api/navidrome/scan
  → 让 Navidrome 重新扫描 LIBRARY_DIR
```

### 5. 刷新榜单（异步）

```text
POST /api/charts/refresh {"dry_run": false}
  → 202 + status=running
  → API 后台调度内置榜单引擎（抓取、匹配、建列表、提交补歌），无需进容器执行脚本
GET /api/charts/status
  → status / job_ids / report_file / log_tail / error
GET /api/charts/report
  → 下载最新未匹配清单 CSV
```

- `dry_run=true` 只抓取、匹配、出报表，不建列表、不补歌
- `wait=true` 会等待执行结束再返回，适合调试
- 已有任务运行时返回 `409`
- 每天 6 点 cron 不受影响；手动刷新和 cron 不要同时执行

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MUSIC_API_BASE_URL` | 必填 | 上游音乐 API 根地址，可在构建或运行时指定 |
| `MUSIC_API_TIMEOUT` | `30` | 单次请求超时（秒） |
| `MUSIC_API_RETRIES` | `2` | 5xx / 网络错误重试次数 |
| `MUSIC_API_COOKIE` | 空 | 上游需要登录态时传 Cookie |
| `MUSIC_API_BEARER_TOKEN` | 空 | 上游使用 Bearer Token 时传值 |
| `PORT` | 单服务镜像 `8080`；bundle 内音乐服务 `8848` | 监听端口 |
| `AGENT_PORT` | `8080` | 仅 bundle：agent API 监听端口 |
| `MUSIC_API_RATE_LIMIT` | `40` | 限流窗口内允许的上游请求数，`0` 为关闭 |
| `MUSIC_API_RATE_WINDOW` | `300` | 限流窗口（秒） |
| `MUSIC_API_DOWNLOAD_TIMEOUT` | `300` | 下载请求超时（秒） |
| `LIBRARY_DIR` | `/music` | 容器内清洗后的媒体库目录，建议设为 `/library`，Navidrome 扫描这里 |
| `DOWNLOAD_DIR` | `/music` | nas-music-kit 的下载目录，固定为 `/music`，一般不要改 |
| `MATCH_SOURCE` | `netease` | 补歌默认搜索音源 |
| `MATCH_THRESHOLD` | `0.9` | 补歌标题匹配阈值，越高越严格 |
| `NAVIDROME_URL` | 空 | Navidrome 地址，例如 `http://127.0.0.1:4533` |
| `NAVIDROME_USER` | 空 | Navidrome 管理员账号 |
| `NAVIDROME_PASSWORD` | 空 | Navidrome 管理员密码 |
| `NAVIDROME_SCAN_WAIT` | `true` | 扫描后是否等待完成 |
| `PLAYLIST_MATCH_THRESHOLD` | `0.86` | 歌单导入的模糊匹配阈值 |
| `IMPORT_PROXY_URL` | 空 | Spotify/汽水抓取代理，如 `http://127.0.0.1:7890` |
| `SPOTIFY_CLIENT_ID` | 空 | Spotify Web API 回退凭据，可选 |
| `SPOTIFY_CLIENT_SECRET` | 空 | Spotify Web API 回退凭据，可选 |
| `CHARTS_HOST_DIR` | `./music-charts` | 宿主机榜单目录，只读挂载到 `/charts` |
| `CHARTS_DIR` | `/charts` | 容器内榜单代码目录 |
| `CHARTS_CONFIG` | `/charts/config.json` | 容器内榜单配置文件 |
| `CHARTS_REPORTS_DIR` | `/tmp/charts-reports` | 报表、日志和运行状态目录 |
| `CHARTS_TIMEOUT` | `3600` | 单次榜单执行超时（秒） |
| `ENABLE_IP_ALLOWLIST` | `auto` | 大盘 IP 白名单开关：`auto` 表示配了 CIDR 才开启，也可显式 `true` / `false` |
| `ALLOWED_CIDRS` | 空 | 允许访问大盘的 IP/CIDR，逗号分隔；空表示不限制 |
| `AUTH_USER` | 空 | 大盘 Basic 认证用户名，和密码同时配置才生效 |
| `AUTH_PASSWORD` | 空 | 大盘 Basic 认证密码 |
| `TRUST_PROXY_HEADERS` | `false` | 反向代理后设为 `true`，从 `X-Forwarded-For` 取大盘访问者真实 IP |

## 目录结构

nas-music-kit 的下载目录固定是 `/music`，这个不要改。Agent 只额外定义一个清洗后的媒体库目录：

```text
/music       nas-music-kit 下载的原始文件（固定）
/library     清洗后的媒体库，结构为 Artist/Album/Artist - Title.mp3
```

- `DOWNLOAD_DIR`：固定 `/music`，nas-music-kit 写这里，agent 从这里读取并清洗
- `LIBRARY_DIR`：清洗后的媒体库，建议 `/library`，Navidrome 扫描这里
- 清洗时音频和同名 `.lrc` 会一起从 `/music` 移动到 `/library`
- 移动成功后，`/music` 里的原文件和空子目录会被清理
- 源目录为空时会删除整个源目录；但 `/music` 根目录始终保留，因为 nas-music-kit 还要继续往里下载
- 非音频附属文件（如封面 `.jpg`）不会自动删除，避免误删素材
- 想保持旧行为可以让 `LIBRARY_DIR=/music`，此时就是原地整理，不会删除目录
- `GET /healthz` 会返回当前实际使用的 `library_dir` 和 `download_dir`

> 旧部署升级：Navidrome 原来扫描 `./music` 的，要改成扫描 `./library`；第一次执行 `organize` 会把 `/music` 里的已有文件全部搬到 `/library`。

## 限流

上游 GD 音乐台/juneix 的限制约为 **5 分钟 50 次**，并且会动态调整；重试同样消耗额度。

本服务在每次真正发往上游的请求前做滑动窗口限流，默认 **40 次 / 300 秒**，给重试和上游波动留余量：

- 超限返回 `429`，带 `Retry-After` 响应头
- 上游自己返回 `429` 时，同样转成 `429` 并保留等待时间
- `GET /healthz` 不消耗额度
- `MUSIC_API_RATE_LIMIT=0` 可关闭本地限流，不建议
- 限流只作用于经过本服务的请求；直接访问 juneix 的 `8848` 网页不受此限制
- 限流状态在进程内存里，本镜像只启动一个 uvicorn 进程；如果部署多个副本，上游额度是共享的，需要自行降低每个副本的限制

## 大盘访问控制

IP 白名单和 Basic 认证只保护监控大盘（`/dashboard`、`/api/dashboard/status`）。搜索、下载、补歌、歌单导入、榜单、清洗、扫描等业务 API 不受影响，Skill 可以正常远程调用。

大盘包含下载、清洗等操作按钮，建议对外限制访问；业务 API 本身没有认证，端口不能直接暴露到公网。

```env
# 大盘 IP 白名单开关：auto = 配了 CIDR 就开启；也可显式 true / false
ENABLE_IP_ALLOWLIST=true

# 只允许局域网和 Docker 网段访问大盘；逗号分隔，支持单个 IP 和 CIDR
ALLOWED_CIDRS=192.168.1.0/24,10.0.0.0/8,172.17.0.0/16

# 大盘 HTTP Basic 认证；两项必须同时配置
AUTH_USER=admin
AUTH_PASSWORD=change-me

# 部署在 Nginx / Traefik 等反向代理后面时开启
TRUST_PROXY_HEADERS=true
```

规则：

- 只作用于 `/dashboard` 和 `/api/dashboard/status`，其它路径直接放行
- `ENABLE_IP_ALLOWLIST` 默认 `auto`：`ALLOWED_CIDRS` 非空就自动开启
- 显式设 `true` 但 `ALLOWED_CIDRS` 为空会拒绝启动，避免“以为开了其实全放行”
- 显式设 `false` 会忽略已配置的 CIDR，方便临时关闭排查
- 访问大盘时来源 IP 不在白名单返回 `403`，不会读取账号密码
- 启用 Basic 认证后，访问大盘未带凭据返回 `401` 和 `WWW-Authenticate`；浏览器会弹登录框
- 需要命令行访问大盘时可以加 `-u "admin:change-me"`，例如：
  ```bash
  curl -u "admin:change-me" http://NAS:8080/api/dashboard/status
  ```
- `AUTH_USER` / `AUTH_PASSWORD` 只配一项会拒绝启动，避免“以为开了认证其实没开”
- `ALLOWED_CIDRS` 写错格式会拒绝启动，错误信息会指出具体条目

注意：

- Basic 认证在 HTTP 下是明文传输，只适合内网；要公网访问请套 HTTPS 反向代理
- `TRUST_PROXY_HEADERS=true` 时只应在可信代理后面使用，否则客户端可以伪造 `X-Forwarded-For`
- 容器桥接部署时，Docker 通常保留原始客户端 IP；若发现来源变成网关地址，按实际网段放行或开代理头

## 自动补歌与媒体库清洗

补歌任务由后台单 worker 串行执行，天然受上游限流约束，适合一次提交、慢慢跑完。

清洗规则：

- 目录结构：`LIBRARY_DIR/Artist/Album/Artist - Title.ext`
- 原始下载固定在 `/music`，清洗时自动移动到 `LIBRARY_DIR`；两者相同时原地整理
- 标签乱码用 `ftfy` 修复；检测到乱码时回写标题、歌手、专辑
- 同名 `.lrc` 随歌曲一起移动，统一转成 UTF-8
- 没有 `.lrc` 时，把内嵌歌词导出为 UTF-8 `.lrc`
- 文件名非法字符替换，重名自动追加 `(2)`
- `dry_run=true` 只预览，不改动文件
- 清洗完成后可自动调用 Navidrome `startScan`

## 部署

三种方式按需选一种。需要固定版本时，把 `latest` 换成 `0.11.2` 之类的具体标签即可。

| 方案 | 使用镜像 | 端口 |
| --- | --- | --- |
| A 独立 agent | `nas-music-kit-agent-api` | 8080 |
| B 两个容器 + host 网络（推荐，不扩展原镜像） | `juneix/nas-music-kit` + `nas-music-kit-agent-api` | 8848 + 8080 |
| C 单镜像 bundle | `nas-music-kit-agent-api-bundle` | 8848 + 8080 |

### 方案 A：只部署 agent API

音乐 API 已经跑在 NAS 的其它容器或主机上时使用。

```yaml
services:
  music-kit-agent:
    image: ghcr.io/yaobin-kid/nas-music-kit-agent-api:latest
    container_name: nas-music-kit-agent
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./music:/music
      - ./library:/library
    environment:
      MUSIC_API_BASE_URL: http://192.168.1.10:8848   # 改成你的音乐 API 地址
      MUSIC_API_TIMEOUT: "30"
      MUSIC_API_DOWNLOAD_TIMEOUT: "300"
      MUSIC_API_RETRIES: "2"
      MUSIC_API_RATE_LIMIT: "40"
      MUSIC_API_RATE_WINDOW: "300"
      LIBRARY_DIR: /library
      DOWNLOAD_DIR: /music
      MATCH_SOURCE: netease
      MATCH_THRESHOLD: "0.9"
      PLAYLIST_MATCH_THRESHOLD: "0.86"
      IMPORT_PROXY_URL: ""
      SPOTIFY_CLIENT_ID: ""
      SPOTIFY_CLIENT_SECRET: ""
      NAVIDROME_URL: http://192.168.1.10:4533
      NAVIDROME_USER: admin
      NAVIDROME_PASSWORD: change-me
      NAVIDROME_SCAN_WAIT: "true"
```

说明：

- 音乐服务在同一个 compose 网络时，地址可以写服务名，例如 `http://nas-music-kit:8848`。
- 只有走绿联隧道等带登录态的地址，才需要额外添加 `MUSIC_API_COOKIE` 或 `MUSIC_API_BEARER_TOKEN`。
- `./music` 是 nas-music-kit 的下载目录，不要改；必须和音乐服务的下载目录一致。
- `./library` 是清洗后的媒体库，Navidrome 应扫描这里。
- 仓库文件：[docker-compose.yml](./docker-compose.yml)。

### 方案 B：两个容器 + host 网络（推荐）

不修改 juneix 镜像，两个容器共用宿主网络，agent 通过 `127.0.0.1:8848` 调音乐服务。

```yaml
services:
  nas-music-kit:
    image: ghcr.io/juneix/nas-music-kit:latest
    container_name: nas-music-kit
    network_mode: host
    restart: unless-stopped
    environment:
      PORT: 8848
    volumes:
      - ./music:/music

  music-kit-agent:
    image: ghcr.io/yaobin-kid/nas-music-kit-agent-api:latest
    container_name: nas-music-kit-agent
    network_mode: host
    restart: unless-stopped
    depends_on:
      - nas-music-kit
    volumes:
      - ./music:/music
      - ./library:/library
    environment:
      PORT: 8080
      MUSIC_API_BASE_URL: http://127.0.0.1:8848
      MUSIC_API_RATE_LIMIT: "40"
      MUSIC_API_RATE_WINDOW: "300"
      LIBRARY_DIR: /library
      DOWNLOAD_DIR: /music
      MATCH_SOURCE: netease
      MATCH_THRESHOLD: "0.9"
      PLAYLIST_MATCH_THRESHOLD: "0.86"
      IMPORT_PROXY_URL: ""
      SPOTIFY_CLIENT_ID: ""
      SPOTIFY_CLIENT_SECRET: ""
      NAVIDROME_URL: http://127.0.0.1:4533
      NAVIDROME_USER: admin
      NAVIDROME_PASSWORD: change-me
      NAVIDROME_SCAN_WAIT: "true"
```

说明：

- `network_mode: host` 下不要写 `ports`，8848 和 8080 会直接占用宿主机端口。
- 端口冲突时修改 `PORT`，并同步修改 `MUSIC_API_BASE_URL` 里的端口。
- 两个容器都挂载 `./music`：juneix 仍下载到 `/music`。
- agent 额外挂载 `./library`，清洗时把文件从 `/music` 移到 `/library`。
- Navidrome 只扫描 `./library`。
- 仓库文件：[docker-compose.host.yml](./docker-compose.host.yml)。

### 方案 C：单镜像 bundle

juneix 音乐服务和 agent API 在同一个容器里，通过容器内回环通信，不需要 Cookie。

```yaml
services:
  nas-music-kit-bundle:
    image: ghcr.io/yaobin-kid/nas-music-kit-agent-api-bundle:latest
    container_name: nas-music-kit-bundle
    restart: unless-stopped
    ports:
      - "8848:8848"
      - "8080:8080"
    environment:
      PORT: 8848
      AGENT_PORT: 8080
      MUSIC_API_BASE_URL: http://127.0.0.1:8848
      MUSIC_API_TIMEOUT: "30"
      MUSIC_API_DOWNLOAD_TIMEOUT: "300"
      MUSIC_API_RETRIES: "2"
      MUSIC_API_RATE_LIMIT: "40"
      MUSIC_API_RATE_WINDOW: "300"
      LIBRARY_DIR: /library
      DOWNLOAD_DIR: /music
      MATCH_SOURCE: netease
      MATCH_THRESHOLD: "0.9"
      PLAYLIST_MATCH_THRESHOLD: "0.86"
      IMPORT_PROXY_URL: ""
      SPOTIFY_CLIENT_ID: ""
      SPOTIFY_CLIENT_SECRET: ""
      NAVIDROME_URL: http://192.168.1.10:4533   # 桥接网络不能写 127.0.0.1
      NAVIDROME_USER: admin
      NAVIDROME_PASSWORD: change-me
      NAVIDROME_SCAN_WAIT: "true"
      TZ: Asia/Shanghai
    volumes:
      - ./music:/music
      - ./library:/library
```

说明：

- 左侧端口被占用时只改映射，例如 `18848:8848`；`PORT`、`AGENT_PORT` 保持容器内端口不变。
- bundle 是桥接网络，`NAVIDROME_URL` 不能写 `127.0.0.1`，要填 NAS 的局域网地址。
- 容器内 juneix 仍下载到 `/music`，agent 清洗后写入 `/library`；Navidrome 扫描 `./library`。
- 仓库文件：[docker-compose.bundle.yml](./docker-compose.bundle.yml)。
- bundle 跟随上游镜像版本，juneix 更新后重新构建或拉取新版本即可。

### 启动与验证

```bash
# 按所选方案启动，例如：
docker compose -f docker-compose.host.yml up -d

curl http://127.0.0.1:8080/healthz

curl -G "http://127.0.0.1:8080/api/search" \
  --data-urlencode "source=netease" \
  --data-urlencode "name=小方桌" \
  --data-urlencode "pages=1"
```

三个 compose 文件都支持用 `.env` 覆盖默认值，变量参考 [.env.example](./.env.example)：

| 变量 | 说明 |
| --- | --- |
| `MUSIC_API_BASE_URL` | 音乐 API 地址 |
| `AGENT_HOST_PORT` | agent API 映射到宿主机的端口 |
| `MUSIC_KIT_PORT` | host 方案里音乐服务的端口 |
| `AGENT_PORT` | host 方案里 agent 容器的监听端口 |
| `MUSIC_KIT_HOST_PORT` | bundle 方案里音乐服务映射到宿主机的端口 |
| `MUSIC_DIR` | 宿主机上的 nas-music-kit 下载目录，挂载为容器内 `/music` |
| `LIBRARY_HOST_DIR` | 宿主机上的清洗后媒体库目录，挂载为容器内 `/library` |
| `LIBRARY_DIR` | 容器内媒体库路径，compose 里设为 `/library` |
| `DOWNLOAD_DIR` | 容器内下载路径，固定 `/music`，一般不用改 |
| `MATCH_SOURCE` / `MATCH_THRESHOLD` | 补歌搜索音源与匹配阈值 |
| `NAVIDROME_URL` / `NAVIDROME_USER` / `NAVIDROME_PASSWORD` | Navidrome 扫描配置 |

`MUSIC_DIR` 和 `LIBRARY_HOST_DIR` 是宿主机路径，写在 `.env` 里；`LIBRARY_DIR` 和 `DOWNLOAD_DIR` 是容器内路径，写在 compose 的 `environment` 里。

### 功能自检（按顺序）

以下命令假设 agent 地址是 `http://127.0.0.1:8080`，宿主机下载目录是 `./music`，清洗目录是 `./library`。

**1. 健康检查**

```bash
curl http://127.0.0.1:8080/healthz
```

预期：`status=ok`，并且 `download_dir` 是 `/music`，`library_dir` 是 `/library`。

**2. 搜索**

```bash
curl -sG "http://127.0.0.1:8080/api/search" \
  --data-urlencode "source=netease" \
  --data-urlencode "name=小方桌" \
  --data-urlencode "pages=1"
```

预期：返回数组，至少有 `id`、`name`、`artist`、`pic_id`、`source`。返回 `429` 说明触发限流，等 `Retry-After` 秒再试。

**3. 下载单曲**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/download" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "netease",
    "id": "3412527335",
    "name": "掉漆小方桌",
    "artist": "小春",
    "album": "",
    "pic_id": "109951173651495877",
    "br": "999",
    "lyric": true,
    "vip": false
  }'
```

预期：`status=success`，并且宿主机 `./music` 里出现 `小春 - 掉漆小方桌.mp3`。

**4. 清洗预览**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/library/organize?dry_run=true"
```

预期：`download_dir=/music`、`library_dir=/library`，`planned` 大于 0，文件没有移动。

**5. 执行清洗**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/library/organize"
```

预期：`moved` 大于 0，文件出现在 `./library/歌手/专辑/歌手 - 歌名.mp3`，`./music` 里对应文件消失。第一次执行会把 `/music` 里已有文件全部整理到 `/library`。

**6. 导入歌单（预览）**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/playlist/import" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://music.163.com/playlist?id=3778678"],
    "dry_run": true,
    "backfill": false
  }'
```

预期：`playlists[0]` 里有 `total`、`matched`、`missing`，且不会创建列表、不会补歌。确认能匹配后去掉 `dry_run` 再执行一次。

**7. 补歌任务**

用一首你确定不在库里的歌测试：

```bash
curl -s -X POST "http://127.0.0.1:8080/api/library/backfill" \
  -H "Content-Type: application/json" \
  -d '{
    "tracks": [{"title": "不存在的歌名", "artist": "测试歌手", "source": "netease"}],
    "organize": true,
    "scan": false
  }'
```

预期：返回 `202` 和 `job_id`，然后：

```bash
curl -s "http://127.0.0.1:8080/api/jobs/<job_id>"
```

轮询到 `status=completed`，`results[0].status` 应为 `not_found` 或 `downloaded`。

**8. Navidrome 扫描**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/navidrome/scan?wait=true"
```

预期：返回 `started` / `waited` / `count`。返回 `503` 表示 `NAVIDROME_*` 没配置。

**9. 观察日志**

```bash
docker compose logs -f music-kit-agent
```

可以看到上游 `/api/search`、`/api/download` 调用，以及后台任务的 `searching → organizing → scanning` 阶段。

常见状态码：`200` 成功、`202` 已提交任务、`422` 参数错误、`429` 限流、`502` 上游失败、`503` Navidrome 未配置。

### 自行构建

```bash
# 独立 agent 镜像
docker build -t nas-music-kit-api:local .

# bundle 镜像（可固定上游版本）
docker build -f Dockerfile.bundle \
  --build-arg MUSIC_KIT_IMAGE=ghcr.io/juneix/nas-music-kit:latest \
  -t nas-music-kit-bundle:local .
```

构建参数只提供默认值，运行时仍可用 `-e MUSIC_API_BASE_URL=...` 覆盖。

## 对接 music-charts

`music-charts/` 是榜单/歌单匹配程序。匹配结束后，未匹配歌曲会自动提交给本服务：

1. `charts.py` 读取 `config.json` 里的 `agent_api`
2. 调用 `POST /api/library/backfill` 批量提交缺失歌曲
3. 本服务后台搜索、下载，完成后清洗并触发 Navidrome 扫描
4. 下一次榜单运行时，已下载的歌曲会正常匹配进播放列表

> 补歌任务是后台异步的：本次榜单的播放列表不会立刻包含新歌，等任务完成并扫描后，下一次运行会自动纳入。需要立即重建时，等补歌任务完成后再调用一次 `POST /api/charts/refresh`。

Apple Music 按地区拆成多个榜单，`type` 为 `itunes`，`id` 填 Apple Music storefront 代码：

```text
榜单06·AppleMusic(美国)      id: us
榜单07·AppleMusic(台湾)      id: tw
榜单08·AppleMusic(香港)      id: hk
榜单09·AppleMusic(新加坡)    id: sg
榜单10·AppleMusic(马来西亚)  id: my
榜单11·AppleMusic(日本)      id: jp
榜单12·AppleMusic(韩国)      id: kr
榜单13·YouTube
```

想增删地区只改 `platforms`：复制一段 `itunes` 配置，改 `key`、`id` 和 `name`，再把后面的榜单序号顺延即可。

配置参考 [config.example.json](./music-charts/config.example.json)，复制为 `config.json` 后填写：

```json
"agent_api": {
  "enable": true,
  "base": "http://127.0.0.1:8080",
  "organize": true,
  "scan": true,
  "wait": false
}
```

- `wait=false`：提交任务后脚本退出，适合 cron，任务在容器里继续跑
- `wait=true`：脚本等待任务完成；大批量会很慢

### 榜单功能测试

**1. 确认服务与榜单配置**

```bash
curl http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/api/charts/status
```

`/api/charts/status` 里 `configured` 必须是 `true`。如果是 `false`，把 `CHARTS_HOST_DIR` 指向 NAS 上的 `music-charts` 目录并重启容器。

**2. 预览一次**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/charts/refresh" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

返回 `202` 和 `status=running`，然后轮询：

```bash
watch -n 5 "curl -s http://127.0.0.1:8080/api/charts/status"
```

直到 `status=completed`。重点看：

- `log_tail` 里的抓取、匹配数量
- `report_file` 是否生成
- `error` 是否为空

下载未匹配清单：

```bash
curl -s -o "未匹配清单.csv" "http://127.0.0.1:8080/api/charts/report"
```

**3. 正式刷新一次**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/charts/refresh" \
  -H "Content-Type: application/json" \
  -d '{}'
```

预期 `status=completed`，`log_tail` 中出现：

```text
[平台] 已创建播放列表《...》
[agent] 已提交补歌任务 xxxx，共 N 首
```

`job_ids` 里就是补歌任务，继续查：

```bash
curl -s "http://127.0.0.1:8080/api/jobs/<job_id>"
```

轮询到 `status=completed` 后检查 `./library` 和 Navidrome。

**4. 复跑预览**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/charts/refresh" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

未匹配数量应该比第一次少；如果没减少，说明补歌没成功或 `LIBRARY_DIR` 配错了。

**5. 定时任务**

每天 6 点的 `run.sh` 可以保持原样。也可以把 cron 改成调用：

```bash
curl -s -X POST "http://127.0.0.1:8080/api/charts/refresh" \
  -H "Content-Type: application/json" \
  -d '{}'
```

两种方式选一种，不要同时跑，避免重复建列表。

**6. 独立歌单链接测试**

```bash
curl -s -X POST "http://127.0.0.1:8080/api/playlist/import" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://music.163.com/playlist?id=3778678"],
    "dry_run": true
  }'
```

确认 `total / matched / missing` 正常后，把 `dry_run` 改成 `false` 正式导入。
- `max_tracks` 默认 1000，避免一次提交过多
- `config.json` 含 Navidrome 密码和榜单配置，已在 `.gitignore` 中排除，不要提交

外部歌单/榜单链接导入使用 `POST /api/playlist/import`：支持网易云、QQ音乐、Spotify、汽水音乐，匹配完成后同样会自动提交缺失歌曲。

## Skill

`skill/nas-music-kit/SKILL.md` 是给 Trae 使用的技能说明，流程为：

1. 读取环境变量 `MUSIC_KIT_API_URL`（指向本服务，如 `http://192.168.1.10:8080`）
2. 调用 `/api/search` 搜索候选项
3. 让用户确认要下载的歌曲
4. 调用 `/api/download`，返回文件名与写入状态

批量补齐缺失歌曲时，改用 `/api/library/backfill` 提交后台任务，再轮询 `/api/jobs/{job_id}`；不要逐首循环调用下载接口。

安装时把 `skill/nas-music-kit` 目录复制到 Trae 的 Skills 目录（全局或项目级均可），并确保 `MUSIC_KIT_API_URL` 可访问。

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload --port 8080
```

## 发布与自动构建

以下事件会触发 `.github/workflows/docker-publish.yml`，先跑测试再构建并推送镜像：

- 推送 `v*` 标签
- 发布 GitHub Release
- 手动触发 `workflow_dispatch`

镜像地址：

- `ghcr.io/yaobin-kid/nas-music-kit-agent-api`（独立服务）
- `ghcr.io/yaobin-kid/nas-music-kit-agent-api-bundle`（含 juneix 音乐服务）

标签规则：`v1.2.3` 会生成 `1.2.3`、`1.2`、`1`、`latest` 和 commit sha 标签，平台为 `linux/amd64` 与 `linux/arm64`。

发布新版本：

```bash
git tag v0.11.2
git push origin v0.11.2
```

首次推送的 GHCR 包默认是私有的，需要在仓库的 Packages 设置里改为 public，或先 `docker login ghcr.io` 再拉取。

## 错误处理

- 上游 4xx/5xx 或网络失败统一返回 `502`，响应体包含 `error`、`upstream_status`、`upstream_body`，便于排查鉴权与地址问题。
- 上游 5xx 会自动重试 `MUSIC_API_RETRIES` 次。
