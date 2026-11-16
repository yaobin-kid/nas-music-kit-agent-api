---
name: nas-music-kit
description: 通过自建的 NAS 音乐 API 搜索、下载歌曲，导入外部歌单链接并匹配/补齐 Navidrome 歌曲，按 Artist/Album 清洗媒体库并触发 Navidrome 扫描。当用户要搜歌、点歌下载、导入歌单、补齐缺失、整理音乐目录或同步 Navidrome 时使用。
---

# NAS 音乐库

所有操作都通过已部署的 `nas-music-kit-agent-api` 服务完成，不要直接访问上游音乐 API。

服务职责：

- 搜索歌曲
- 单曲下载
- 批量补齐缺失歌曲（后台队列 + 上游限流）
- 按 `Artist/Album/Artist - Title` 清洗媒体库
- 修复标签/歌词乱码，歌词统一转 UTF-8
- 触发 Navidrome 扫描

## 前置配置

- 从环境变量 `MUSIC_KIT_API_URL` 读取服务地址，例如 `http://192.168.1.10:8080`。
- 未设置时先向用户询问，不要臆造地址。
- 调用前可检查 `GET $MUSIC_KIT_API_URL/healthz`，返回 `"status":"ok"` 即正常。
- 需要人工查看状态或临时干预时，让用户浏览器打开 `$MUSIC_KIT_API_URL/dashboard`，不要要求用户进入容器。
- 服务端可能对 `/dashboard` 配了 IP 白名单和 Basic 认证；业务 API 不受影响，Skill 直接调用即可，不需要账号密码。
- 上游地址、Cookie、Navidrome 账号都由服务端环境变量配置，Skill 不接触。

## 快速选择

| 用户需求 | 使用接口 |
| --- | --- |
| 给歌单/榜单链接，自动匹配并建列表 | `POST /api/playlist/import` |
| 立即刷新榜单 | `POST /api/charts/refresh` |
| 查看榜单运行状态 | `GET /api/charts/status` |
| 下载最新未匹配清单 | `GET /api/charts/report` |
| 找歌、看候选 | `GET /api/search` |
| 下载某一首 | `POST /api/download` |
| 补齐一批缺失歌曲 | `POST /api/library/backfill` |
| 查看补歌进度 | `GET /api/jobs/{job_id}` |
| 整理媒体库 | `POST /api/library/organize` |
| 让 Navidrome 重新扫描 | `POST /api/navidrome/scan` |

## 导入外部歌单/榜单链接

用户直接给出歌单或榜单链接时，调用 HTTP 接口，不要进入容器执行脚本，也不要逐首搜索。

支持平台：网易云、QQ音乐、Spotify、汽水音乐（含短链）。

```bash
curl -s -X POST "$MUSIC_KIT_API_URL/api/playlist/import" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["<歌单链接>"],
    "backfill": true,
    "scan": true
  }'
```

请求字段：

- `urls`：1-20 个链接，可一次导入多个。
- `name`：自定义 Navidrome 列表名；多个链接时不能指定，否则返回 `422`。
- `dry_run`：只抓取和匹配，不创建列表、不补歌。
- `loose`：宽松匹配；默认严格匹配，歌名精确且歌手有交集才收录。
- `proxy`：Spotify / 汽水需要代理时设为 `true`，代理地址由服务端 `IMPORT_PROXY_URL` 配置。
- `backfill`：是否自动补歌，默认 `true`。
- `scan`：补歌后是否触发 Navidrome 扫描，默认 `true`。

返回示例：

```json
{
  "library_songs": 1200,
  "playlists": [
    {
      "url": "...",
      "platform": "netease",
      "name": "测试歌单",
      "total": 100,
      "matched": 80,
      "missing": 20,
      "playlist_id": "...",
      "created": true
    }
  ],
  "missing_tracks": [{"title": "温柔", "artist": "五月天", "source": "netease"}],
  "missing_truncated": false,
  "backfill_job_id": "...",
  "backfill_total": 20
}
```

处理规则：

1. 接口立即匹配本地曲库，创建或重建 Navidrome 播放列表。
2. 未匹配歌曲会返回在 `missing_tracks` 中。
3. 有缺失且 `backfill=true` 时，返回 `backfill_job_id`；用 `GET /api/jobs/{backfill_job_id}` 查询补歌进度，不要再调用 `/api/library/backfill` 重复提交。
4. 补歌完成后 agent 会自动清洗并触发扫描。
5. 本次播放列表只包含当时已匹配的歌；补歌完成后再调用一次同一个接口，列表会重建并包含新下载的歌。

注意：

- `503` 表示服务端未配置 Navidrome，提示用户配置，不要重试。
- 单个歌单抓取失败会放在对应 `playlists[]` 的 `error` 字段，其它链接照常处理。
- `dry_run=true` 不会创建列表，也不会提交补歌任务。
- 不要向用户暴露 Navidrome 密码或平台密钥。

## 调用配额（重要）

上游音乐 API 有频率限制（约 **5 分钟 50 次**，会动态调整），重试也消耗额度。本服务默认 **40 次 / 300 秒**，超限返回 `429` 并带 `Retry-After`。

必须遵守：

- 交互式搜索一个任务最多 2 次：先只搜歌名；没有结果再换关键词。
- 默认只取第 1 页；确需更多才翻到第 2 页，禁止连续翻页。
- 单曲下载只提交用户确认的那一首。
- 批量补歌只能通过一次 `/api/library/backfill` 提交，由后台限流执行；禁止循环调用 `/api/download`。
- 不要为了对比版本反复搜索同一首歌，一次性展示候选。
- 遇到 `429` 立即停止，把 `Retry-After` 告诉用户，不要自动重试。
- 不要重复提交同一个补歌任务；任务会在后台继续跑。

## 第一步：搜索

```bash
curl -sG "$MUSIC_KIT_API_URL/api/search" \
  --data-urlencode "source=netease" \
  --data-urlencode "name=<关键词>" \
  --data-urlencode "pages=1"
```

规则：

- `source` 默认 `netease`，除非用户指定其他音源。
- `pages` 从 1 开始。
- 返回是数组，字段包括：`id`、`url_id`、`lyric_id`、`pic_id`、`name`、`artist`（数组）、`album`、`source`。
- 搜索是模糊匹配，可能包含不相关的歌，展示时说明这一点。

## 第二步：让用户确认

把候选整理成：

```text
1. 歌名 - 歌手（专辑）
2. ...
```

- 不要替用户自动挑选，除非用户已经明确指定歌名和歌手。
- 结果为空时，建议换关键词或换音源，不要连续轰炸。

## 第三步：下载单曲

```bash
curl -s -X POST "$MUSIC_KIT_API_URL/api/download" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "netease",
    "id": "<搜索结果的 id>",
    "name": "<歌名>",
    "artist": "<歌手>",
    "album": "<专辑，可为空>",
    "pic_id": "<搜索结果的 pic_id>",
    "br": "999",
    "lyric": true,
    "vip": false
  }'
```

字段规则：

- `id` 用搜索结果里的 `id`，不要用 `pic_id` 或 `lyric_id`。
- `artist` 是字符串；多个歌手用 `" / "` 拼接。
- `album` 没有就传空串。
- `br` 固定 `"999"`，表示最高可用音质。
- `lyric` 为 `true` 时写入歌词；`vip` 默认 `false`。

成功响应：

```json
{"bitrate":"999","filename":"小春 - 掉漆小方桌.mp3","status":"success","tags":"ok"}
```

- `filename` 是保存的文件名；`tags` 为 `ok` 表示元数据写入成功。
- `bitrate` 是请求值回显，不代表实际码率。
- `status` 不是 `success` 时，原样告诉用户，不要谎报成功。

## 立即刷新榜单

用户想马上刷新榜单、不等每天 6 点任务时，调用 HTTP 接口，不要执行 `charts.py`。

```bash
curl -s -X POST "$MUSIC_KIT_API_URL/api/charts/refresh" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

- 默认异步：返回 `202` 和 `status=running`，用状态接口轮询。
- `dry_run=true`：只抓取、匹配、出报表，不建列表、不补歌；建议先用它预览。
- `wait=true`：等待执行结束再返回，可能很久，只在用户明确要求时使用。
- `409`：已有榜单任务在执行，不要重复提交。
- `503`：容器没有挂载 `music-charts` 目录或缺少 `config.json`。

查看状态：

```bash
curl -s "$MUSIC_KIT_API_URL/api/charts/status"
```

重点字段：

- `status`：`idle` / `running` / `completed` / `failed`
- `job_ids`：本次提交的补歌任务 id，可再用 `/api/jobs/{id}` 查详情
- `report_file`：未匹配清单路径
- `log_tail`：最近日志
- `error`：失败原因

下载最新未匹配清单：

```bash
curl -s -o "未匹配清单.csv" "$MUSIC_KIT_API_URL/api/charts/report"
```

每日 6 点 cron 不受影响；但手动刷新与 cron 不要同时执行，避免重复建列表。

## 批量补齐缺失歌曲

用户提供一批缺失歌曲，或 music-charts 之外的批量需求时使用。

提交任务：

```bash
curl -s -X POST "$MUSIC_KIT_API_URL/api/library/backfill" \
  -H "Content-Type: application/json" \
  -d '{
    "tracks": [
      {"title": "小方桌", "artist": "小春", "source": "netease"},
      {"title": "温柔", "artist": "五月天", "source": "netease"}
    ],
    "organize": true,
    "scan": true
  }'
```

返回 `202`：

```json
{"job_id":"a1b2c3d4","status":"queued","total":2}
```

任务流程：`搜索 → 匹配 → 下载 → 清洗 → Navidrome 扫描`。

### 查询进度

```bash
curl -s "$MUSIC_KIT_API_URL/api/jobs/<job_id>"
```

建议 30-60 秒查询一次，不要高频轮询。

关键字段：

- `status`：`queued` / `running` / `completed` / `failed`
- `stage`：`searching` / `organizing` / `scanning` / `done`
- `processed` / `total`
- `downloaded` / `not_found` / `failed`
- `results[]`：逐首结果，`status` 可能是 `downloaded`、`not_found`、`download_failed`、`failed`、`invalid`
- `organize_result`：清洗统计
- `scan_result`：Navidrome 扫描结果

汇报规则：

- 大批量任务不要同步等待；告诉用户任务在后台继续跑，并给出 `job_id`。
- 只有 `status=completed` 才能说任务完成。
- `not_found` 是正常结果，说明上游没有匹配到可信版本，不要自动换关键词重试。
- `failed` 要说明失败原因，不要吞掉。
- `scan_result.skipped` 表示服务端没配置 Navidrome，需要提示用户配置环境变量。

限制：

- 每次最多 1000 首；超过时拆成每批 500 首提交。
- 批量任务本身受上游限流，耗时长属于正常现象。
- 不要重复提交同一批歌曲，避免重复下载。

## 媒体库清洗

整理目录：

```bash
# 先预览
curl -s -X POST "$MUSIC_KIT_API_URL/api/library/organize?dry_run=true"
# 确认后执行
curl -s -X POST "$MUSIC_KIT_API_URL/api/library/organize"
```

目标结构（实际根目录由服务端 `LIBRARY_DIR` 决定，推荐 `/library`）：

```text
/library/Artist/Album/Artist - Title.mp3
/library/Artist/Album/Artist - Title.lrc
```

nas-music-kit 固定下载到 `/music`，清洗时会自动移动到 `LIBRARY_DIR`；Skill 不需要关心宿主机路径，可通过 `/healthz` 查看当前目录。

清洗规则：

- 标签乱码用 `ftfy` 修复，检测到乱码时回写标题、歌手、专辑。
- 同名 `.lrc` 随歌曲移动，统一转 UTF-8。
- 没有 `.lrc` 时，把内嵌歌词导出为 UTF-8 `.lrc`。
- 非法字符替换，重名自动追加 `(2)`。
- 移动成功后，原下载目录里的文件、空子目录和空源目录会被清理；`/music` 根目录保留。
- `dry_run=true` 只预览，不移动文件。

返回字段：

- `library_dir`：清洗后的媒体库目录
- `download_dir`：原始下载目录
- `scanned`：扫描到的音频数
- `moved` / `planned`：已移动 / 预计划移动
- `tags_fixed`：修复并回写的标签数
- `lyrics_written`：写出的歌词数
- `skipped`：本来就在目标位置的文件数
- `source_dirs_removed`：清理掉的源目录空子目录数
- `source_dir_removed`：源根目录是否被删除（`/music` 固定目录除外）
- `errors[]`：失败文件和原因

汇报时至少说明移动数量、修复数量、歌词数量；有 `errors` 要列出来。

## Navidrome 扫描

```bash
curl -s -X POST "$MUSIC_KIT_API_URL/api/navidrome/scan?wait=true"
```

- 需要服务端配置 `NAVIDROME_URL`、`NAVIDROME_USER`、`NAVIDROME_PASSWORD`，账号需要是 Navidrome 管理员。
- 未配置时返回 `503`，不要把这条命令反复重试，应提示用户补配置。
- `wait=true` 会等待扫描结束；大库可能较慢，可以改用 `wait=false` 只触发。
- 返回可能包含 `started`、`waited`、`timed_out`、`count`。

## 错误处理

- `429`：触发上游或本地限流。立即停止，告诉用户等待 `Retry-After` 秒，不要自动重试。
- `502`：上游音乐 API 失败。响应里通常有 `upstream_status`、`upstream_body`。把关键信息告诉用户，并提示检查 `MUSIC_API_BASE_URL`、Cookie/Token 和上游是否可访问。
- `422`：参数不合法。检查 `source`、`name`、`tracks` 或下载请求体。
- `404`（查询任务）：`job_id` 不存在或服务已重启，任务状态可能已丢失。
- `400`（清洗）：`LIBRARY_DIR` 不存在或不可访问。
- `503`（扫描/歌单导入）：Navidrome 未配置。
- 任何下载返回 `status` 不是 `success`，原样告知用户。

## 禁止事项

- 不要直接调用 juneix 的 `8848` 上游接口，绕过限流。
- 不要循环调用 `/api/download` 批量下载。
- 用户给的是歌单/榜单链接时，直接调用 `POST /api/playlist/import`，不要逐首调用 `/api/search`，也不要进容器执行脚本。
- 不要对 `429`、`503` 自动重试。
- 不要重复提交同一个补歌任务。
- 不要在任务未完成时宣称已下载、已清洗或已同步。
