# Prime Video 中文影视资料目录

为电视 App 提供作品名称、别名、简介、详情与至少 960px 的 16:9 横屏图片。播放线路和可播放集数仍由 App 原有片源决定，本仓库只发布公开影视资料。

## 已发布的数据

- [版本清单](https://libremaker700.github.io/prime-video-cn-catalog/version.json)
- [兼容目录](https://libremaker700.github.io/prime-video-cn-catalog/catalog.json)
- 图片地址：`https://libremaker700.github.io/prime-video-cn-catalog/images/<SHA-256>.jpg`

2026-10-29 修复时保留 156 条作品、127 张横屏图（18 张整剧/电影图、109 张分季图）、109 条非空分季简介。121 条作品保留既有官方 ID 映射，35 条没有官方 ID，统一标为 `pending_review`。这些数量描述已有覆盖，不代表本次全部从官网重新获取。

初始资料来自 App 导出的 154 条公开资料快照，另有 2 条资料种子。快照在 `data/bootstrap-catalog.json` 和 `data/fallback_catalog.json`，图片来源和快照状态保留在记录中。修复操作补回了旧合并逻辑丢失的分季官方 ID、官方标题、日期及来源，不推进采集成功时间。

## 每周更新如何工作

GitHub Actions 每天北京时间约 11:17 检查一次。距离 `last_success_at` 满 604800 秒（7 天）才采集；GitHub 的实际启动时间可能延后。手动选择 `sync` 强制采集，选择 `publish` 只验证并部署现有资料。提交代码时会自动运行测试、校验并部署，不会立即批量采集外部站点。

刷新工作清单来自完整目录，七条 `config/seeds.yaml` 只是补充种子。已映射作品及每个已映射季度尝试读取 Prime Video 公开详情页；有 TMDB Key 时对完整目录按原名、类型和首播年份匹配 TMDB，并逐季请求资料。未核验中文名再查询中文维基。缺少官方身份或可靠检索条件的条目保持待核验，并在日志单独统计，不假报为已刷新。

限流、网络错误及解析失败会有限重试，同一来源持续不可用后开启熔断，避免重复请求。只要本轮有采集错误，便不替换已发布目录、不推进 `last_success_at`，第二天继续尝试。失败明细保存在 Actions 的 `catalog-failed-attempt` 附件中。日志区分 `processed`、`checked`、`retained_without_refresh` 与错误数量；CI 绿色、文件提交、保留旧数据均不能代替真实采集成功。

官网入口发现会收集公开作品链接，并请求对应详情页验证。身份冲突、疑似已有作品的新季度进入待核验队列；未通过身份验证的搜索结果不直接加入目录。此流程只能覆盖当前公开页面可见作品，不能保证取得 Prime Video 的全部地区目录。

**可用性边界：** Prime Video 可能返回 403/503 或改变页面结构；TMDB 与翻译需要配置相应服务。程序不绕过访问限制。若官网不可用，本轮会失败并保留旧版。给服务器换位置或部署到 GitHub 并不能保证数据源始终开放。

## 名称、简介与图片

- `data/title-overrides.json` 保存用户确认的国内显示名，例如 Fallout→“辐射”、Reacher→“侠探杰克”；其他地区名称作为别名。
- 中文资料检索核对原名、作品类型和可用的首播年份；同名重拍、歧义结果不自动合并。`seed_year` 是 App 当前选中季度的年份，不用于冒充整剧首播年份。
- 分季简介、官方 ID 和图片按季保存。没有中文、但有该季外语原文时实际调用翻译；未配置则保留原文、来源并标 `translation_pending`。没有原文就留空。
- 优先保留官方图片。TMDB backdrop 仅作为整剧图片，真实标记 `source_type: tmdb`，不当作分季官方海报。分季图片只从对应官方季度取得，竖版图不拉伸补位。
- 每次发布校验图片解码、尺寸、SHA-256、所属季度、目录身份唯一性、清单数量和目录哈希。已有正确资料不因新结果为空而被抹去。

## App 接入约定

1. 取得 `version.json`。
2. 按其中相对路径 `catalog_path` 下载固定版本目录（`releases/<完整SHA-256>/catalog.json`）。
3. 对收到的原始 UTF-8 字节校验 `catalog_sha256`，校验后再解析和更新本地资料。图片路径相对于站点根目录，不相对于 releases 子目录。
4. 按官方 ID 和季度对应 App 作品，缺少映射时不要仅凭标题覆盖；原有播放地址、线路和可播放集数不由此目录修改。网络失败继续显示上一份通过校验的数据。

根目录 `catalog.json` 保留兼容读取；新接入应使用固定版本路径，避免部署切换时新清单与旧目录混用。发布目录不含 APK、签名、片源凭据或播放会话。

## 运行和测试

需要 Python 3.12 或以上。

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/sync.py --repair       # 从保留快照恢复丢失字段，不算一次新采集
python scripts/sync.py --due          # 7 天间隔；刷新完整目录及入口发现
python scripts/sync.py --force        # 忽略间隔，强制刷新
python scripts/validate.py
```

`--no-discovery` 可只刷新现有目录。`scripts/import_bootstrap.py` 为历史首版导入工具，不用于日常更新；修复现有发布请用 `--repair`。

GitHub Actions Secrets 可配置 `TMDB_API_KEY`，以及 `TRANSLATE_PROVIDER` + `DEEPL_API_KEY` 或 `TRANSLATE_API_KEY` / `TRANSLATE_MODEL` / `TRANSLATE_BASE_URL`。实际翻译配置见 [config/translation.md](config/translation.md)。Key 不写入源码、日志或公开目录。仓库 Variables 可设置 `UPDATE_INTERVAL_SECONDS`（默认 604800）与 `DISCOVERY_LIMIT`（默认 20）。

中文维基条目保留原始来源链接；其内容适用来源站许可。TMDB 补充资料需遵守 TMDB 的使用与署名要求。本项目没有与 Amazon 或 TMDB 的官方隶属关系。
