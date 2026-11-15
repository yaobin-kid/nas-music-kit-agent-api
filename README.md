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

## 部署

### docker run

```bash
docker run -d --name nas-music-kit-api \
  -p 8080:8080 \
  -e MUSIC_API_BASE_URL=http://192.168.1.10:8080 \
  -e MUSIC_API_COOKIE='your-cookie' \
  ghcr.io/yaobin-kid/nas-music-kit-agent-api:latest
```

### docker compose

```bash
cp .env.example .env
# 修改 .env 里的 MUSIC_API_BASE_URL
docker compose up -d
```

### 自行构建（构建时指定接口地址）

```bash
docker build \
  --build-arg MUSIC_API_BASE_URL=http://192.168.1.10:8080 \
  -t nas-music-kit-api:local .
```

构建参数只提供默认值，运行时仍可用 `-e MUSIC_API_BASE_URL=...` 覆盖。

### 单镜像扩展（bundle）

`Dockerfile.bundle` 基于 `ghcr.io/juneix/nas-music-kit`，在同一个容器里同时启动：

- 原版音乐服务：`0.0.0.0:8848`
- 本 agent API：`0.0.0.0:8080`

此时上游地址走容器内回环即可，不需要 Cookie，也不经过绿联隧道：

```bash
docker run -d --name nas-music-kit-bundle \
  -p 8848:8848 -p 8080:8080 \
  -v ./music:/music \
  ghcr.io/yaobin-kid/nas-music-kit-agent-api-bundle:latest
```

或者使用仓库里的 compose：

```bash
docker compose -f docker-compose.bundle.yml up -d
```

bundle 镜像内 `PORT` 默认 `8848`、`AGENT_PORT` 默认 `8080`。如果改了 `PORT`，`MUSIC_API_BASE_URL` 会按 `http://127.0.0.1:<PORT>` 自动推导，也可以显式覆盖。

自行构建：

```bash
docker build -f Dockerfile.bundle -t nas-music-kit-bundle:local .
```

可以用 `--build-arg MUSIC_KIT_IMAGE=ghcr.io/juneix/nas-music-kit:<tag>` 固定上游镜像版本。

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
git tag v0.2.0
git push origin v0.2.0
```

首次推送的 GHCR 包默认是私有的，需要在仓库的 Packages 设置里改为 public，或先 `docker login ghcr.io` 再拉取。

## 错误处理

- 上游 4xx/5xx 或网络失败统一返回 `502`，响应体包含 `error`、`upstream_status`、`upstream_body`，便于排查鉴权与地址问题。
- 上游 5xx 会自动重试 `MUSIC_API_RETRIES` 次。
