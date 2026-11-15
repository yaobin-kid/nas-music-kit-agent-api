---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 96438347111ad2f55abbce6c2dd14d81_3a82745de09e11f1913b525400393706
    ReservedCode1: A5FirkU3hGD/0svshNRTBXKRBXWbWFINCSl+Lrg0OfVyDmV4j82BVVK4nHfL69BT7oh9/76VvWRMajfbwxKiCven2Si8seSeEMUhxFItgnjRJ4Mfuq13X8N7tIKkfEfML7tMNi+G7xP9meuzh5sWopr74sS7CUswVdxpWqwSrb0f6AzidpsC1s7NNN0=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 96438347111ad2f55abbce6c2dd14d81_3a82745de09e11f1913b525400393706
    ReservedCode2: A5FirkU3hGD/0svshNRTBXKRBXWbWFINCSl+Lrg0OfVyDmV4j82BVVK4nHfL69BT7oh9/76VvWRMajfbwxKiCven2Si8seSeEMUhxFItgnjRJ4Mfuq13X8N7tIKkfEfML7tMNi+G7xP9meuzh5sWopr74sS7CUswVdxpWqwSrb0f6AzidpsC1s7NNN0=
---

# 外部歌单导入工具

把 QQ音乐 / 网易云 / 汽水音乐 / Spotify 的歌单或榜单链接，解析曲目后匹配本地曲库（/volume1/共享音乐），在 Navidrome 里创建播放列表。

## HTTP API（推荐）

服务端已经内置同样的导入能力，不需要进入容器执行脚本：

```bash
curl -s -X POST "http://127.0.0.1:8080/api/playlist/import" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://music.163.com/playlist?id=3778678"],
    "backfill": true,
    "scan": true
  }'
```

返回 `playlists`（每个链接的抓取/匹配结果）、`missing_tracks` 和 `backfill_job_id`。补歌进度用：

```bash
curl -s "http://127.0.0.1:8080/api/jobs/<backfill_job_id>"
```

可选字段：

- `dry_run=true`：只抓取和匹配，不建列表、不补歌
- `loose=true`：宽松匹配
- `proxy=true`：Spotify/汽水走服务端 `IMPORT_PROXY_URL`
- `name`：自定义列表名，仅单个链接可用
- `backfill=false`：只导入列表，不自动补歌

## 命令行用法（NAS 本机）

以下命令适用于直接在 NAS 上调试；日常和 Skill 调用请优先使用上面的 HTTP API。

```bash
cd /volume1/docker/marvis/music-charts

# 单个链接
python3 playlist_import.py "https://music.163.com/playlist?id=3778678"

# 多个链接一次导入
python3 playlist_import.py "链接A" "链接B" "链接C"

# 自定义列表名
python3 playlist_import.py "链接" --name "我的歌单"

# 只看匹配结果，不写 Navidrome
python3 playlist_import.py "链接" --dry-run

# 走代理（Spotify / 汽水短链需要时）
python3 playlist_import.py "链接" --proxy

# 宽松匹配（默认严格匹配，歌手对不上就不收）
python3 playlist_import.py "链接" --loose
```

## 自动补歌

HTTP API 默认会自动补歌；命令行模式仍可读取 `config.json` 的 `agent_api`，把未匹配歌曲提交给 `nas-music-kit-agent-api`：

- `POST /api/library/backfill` 批量提交
- 后台串行下载，受上游限流约束
- 完成后自动按 `Artist/Album/Artist - Title` 清洗，并触发 Navidrome 扫描
- 下次运行会匹配到新下载的歌曲

`wait=false` 时脚本提交后直接退出，适合 cron；配置示例见 `config.example.json`。

## 支持的链接

| 平台 | 链接形式 |
|---|---|
| QQ音乐 | `https://y.qq.com/n/ryqq/playlist/8xxxxxxxx`、`https://c6.y.qq.com/base/fcgi-bin/u?__=xxxx` 短链 |
| 网易云 | `https://music.163.com/playlist?id=123456`、`https://music.163.com/#/playlist?id=...`、`163cn.tv` 短链 |
| 汽水音乐 | `https://music.douyin.com/qishui/chart/7036274230471712007`（榜单）、`.../share/playlist?id=...`、`https://qishui.douyin.com/s/xxxx` 短链 |
| Spotify | `https://open.spotify.com/playlist/37i9dQ...`（含 intl-xx 前缀）、`spotify:playlist:...` |

## 数据来源（全部免登录 / 无付费）

- **网易云**：`music.163.com/api/v6/playlist/detail` + `/api/playlist/track/all` 补全
- **QQ**：`u.y.qq.com/cgi-bin/musicu.fcg` 的 `music.srfDissInfo.aiDissInfo`，按 offset 分批
- **汽水音乐**：`api.qishui.com/luna/pc/charts/{chart_id}`（榜单）、`/luna/pc/playlist/detail?playlist_id=`（歌单）
- **Spotify**：`open.spotify.com/embed/playlist/{id}` 页面的 `__NEXT_DATA__` SSR 数据（需要代理，国内直连 TLS 会被重置）。
  Web API（`api.spotify.com`）自 2024 年起要求 app 所有者账号持有 Premium，本项目不再使用该通道，
  `config.json` 的 `spotify_auth` 仅作历史记录保留。

常用榜单 id：

| 榜单 | id |
|---|---|
| 汽水热歌榜（weekly，50 首） | 7036274230471712007 |
| Spotify Top 50 Global | 37i9dQZEVXbMDoHDwVN2tF |
| Spotify Today's Top Hits | 37i9dQZF1DXcBWIGoYBM5M |

## 行为说明

- 列表名默认取平台上的歌单原始名称；同名旧列表会先删除再重建（内容随之更新）。
- **默认严格匹配**：歌名精确且歌手有交集才收录；有同名歌但歌手不符时宁可不收（避免「春天的故事」这类误配）。
  `--loose` 回退到旧逻辑（歌名唯一即采纳 + 模糊匹配）。
- 只创建播放列表条目，不会复制或移动任何音频文件。
- 每次运行输出 SUMMARY_JSON，包含抓取数、匹配数、未匹配明细。
- 未匹配清单落盘为 `reports/歌单未匹配-{歌单名}-{日期}.csv`，并镜像到 `/volume1/共享音乐/_榜单报表/`（这是补曲库的采购/下载清单）。
