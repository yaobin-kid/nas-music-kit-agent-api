# NAS Music Kit Agent API

把 NAS 音乐播放器的两个接口（`/api/search`、`/api/download`）封装成稳定的 HTTP 服务，方便 Trae Skill 或其它自动化程序调用。

- `GET /api/search`：按关键词搜索歌曲
- `POST /api/download`：按歌曲信息触发下载（由上游完成落盘与标签写入）
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

### 健康检查

```http
GET /healthz
```

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

## 限流

上游 GD 音乐台/juneix 的限制约为 **5 分钟 50 次**，并且会动态调整；重试同样消耗额度。

本服务在每次真正发往上游的请求前做滑动窗口限流，默认 **40 次 / 300 秒**，给重试和上游波动留余量：

- 超限返回 `429`，带 `Retry-After` 响应头
- 上游自己返回 `429` 时，同样转成 `429` 并保留等待时间
- `GET /healthz` 不消耗额度
- `MUSIC_API_RATE_LIMIT=0` 可关闭本地限流，不建议
- 限流只作用于经过本服务的请求；直接访问 juneix 的 `8848` 网页不受此限制
- 限流状态在进程内存里，本镜像只启动一个 uvicorn 进程；如果部署多个副本，上游额度是共享的，需要自行降低每个副本的限制

## 部署

三种方式按需选一种。需要固定版本时，把 `latest` 换成 `0.3.0` 之类的具体标签即可。

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
    environment:
      MUSIC_API_BASE_URL: http://192.168.1.10:8848   # 改成你的音乐 API 地址
      MUSIC_API_TIMEOUT: "30"
      MUSIC_API_RETRIES: "2"
      MUSIC_API_RATE_LIMIT: "40"
      MUSIC_API_RATE_WINDOW: "300"
```

说明：

- 音乐服务在同一个 compose 网络时，地址可以写服务名，例如 `http://nas-music-kit:8848`。
- 只有走绿联隧道等带登录态的地址，才需要额外添加 `MUSIC_API_COOKIE` 或 `MUSIC_API_BEARER_TOKEN`。
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
    environment:
      PORT: 8080
      MUSIC_API_BASE_URL: http://127.0.0.1:8848
      MUSIC_API_RATE_LIMIT: "40"
      MUSIC_API_RATE_WINDOW: "300"
```

说明：

- `network_mode: host` 下不要写 `ports`，8848 和 8080 会直接占用宿主机端口。
- 端口冲突时修改 `PORT`，并同步修改 `MUSIC_API_BASE_URL` 里的端口。
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
      MUSIC_API_RETRIES: "2"
      MUSIC_API_RATE_LIMIT: "40"
      MUSIC_API_RATE_WINDOW: "300"
      TZ: Asia/Shanghai
    volumes:
      - ./music:/music
```

说明：

- 左侧端口被占用时只改映射，例如 `18848:8848`；`PORT`、`AGENT_PORT` 保持容器内端口不变。
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
| `MUSIC_DIR` | 音乐下载目录 |

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

## Skill

`skill/nas-music-kit/SKILL.md` 是给 Trae 使用的技能说明，流程为：

1. 读取环境变量 `MUSIC_KIT_API_URL`（指向本服务，如 `http://192.168.1.10:8080`）
2. 调用 `/api/search` 搜索候选项
3. 让用户确认要下载的歌曲
4. 调用 `/api/download`，返回文件名与写入状态

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
git tag v0.3.0
git push origin v0.3.0
```

首次推送的 GHCR 包默认是私有的，需要在仓库的 Packages 设置里改为 public，或先 `docker login ghcr.io` 再拉取。

## 错误处理

- 上游 4xx/5xx 或网络失败统一返回 `502`，响应体包含 `error`、`upstream_status`、`upstream_body`，便于排查鉴权与地址问题。
- 上游 5xx 会自动重试 `MUSIC_API_RETRIES` 次。
