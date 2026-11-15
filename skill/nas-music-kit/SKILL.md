---
name: nas-music-kit
description: 通过自建的 NAS 音乐 API 搜索歌曲并下载到 NAS。当用户要求搜索歌曲、按歌名/歌手找歌、点歌下载、把音乐下载到 NAS 音库时使用。
---

# NAS 音乐库

所有操作都通过已部署的 `nas-music-kit-agent-api` 服务完成，该服务再转发到 `MUSIC_API_BASE_URL` 指向的上游音乐 API。

## 前置配置

- 从环境变量 `MUSIC_KIT_API_URL` 读取服务地址，例如 `http://192.168.1.10:8080`。
- 如果未设置，先向用户询问地址；不要臆造，也不要直接访问上游 API。
- 调用前可先检查 `GET $MUSIC_KIT_API_URL/healthz`，返回 `"status":"ok"` 即服务正常。

## 第一步：搜索

```bash
curl -sG "$MUSIC_KIT_API_URL/api/search" \
  --data-urlencode "source=netease" \
  --data-urlencode "name=<关键词>" \
  --data-urlencode "pages=1"
```

- `source` 默认用 `netease`，除非用户指定其他音源。
- `pages` 从 1 开始；需要更多结果时再翻页。
- 返回是数组，每项字段：`id`、`url_id`、`lyric_id`、`pic_id`、`name`、`artist`（数组）、`album`、`source`。
- 搜索是模糊匹配，结果可能包含不相关的歌。

## 第二步：让用户确认

把候选整理成“序号. 歌名 - 歌手（专辑）”的列表展示给用户，由用户选择要下载的歌曲。

- 不要替用户自动挑选，除非用户已经明确指定歌名和歌手。
- 如果结果为空，建议用户换关键词或换音源。

## 第三步：下载

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

- `id` 用搜索结果里的 `id`（不要用 `pic_id`/`lyric_id`）。
- `artist` 是字符串；多个歌手用 `" / "` 拼接。
- `album` 没有就传空串。
- `br` 固定用 `"999"`，表示请求最高可用音质。
- `lyric` 为 `true` 时把歌词写入标签；`vip` 默认 `false`。

成功响应示例：

```json
{"bitrate":"999","filename":"小春 - 掉漆小方桌.mp3","status":"success","tags":"ok"}
```

- `filename` 是实际保存的文件名；`tags` 为 `ok` 表示元数据写入成功。
- `bitrate` 是回显的请求值，不代表实际码率。

## 错误处理

- HTTP 502：上游音乐 API 失败，响应里会有 `upstream_status` 和 `upstream_body`。把关键信息告诉用户，并提示检查容器配置（`MUSIC_API_BASE_URL`、`MUSIC_API_COOKIE`、`MUSIC_API_BEARER_TOKEN`）以及上游是否可访问。
- HTTP 422：缺少必填参数，检查 `source`、`name` 或下载请求体。
- 下载返回 `status` 不是 `success` 时，原样把响应告诉用户，不要谎报成功。
