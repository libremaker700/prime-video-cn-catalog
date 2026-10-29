# Prime Video 中文影视资料目录

为电视 App 提供作品名称、别名、简介、详情与至少 960px 的 16:9 横屏图片。播放线路和可播放集数仍由 App 原有片源决定，本仓库只发布公开影视资料。

## 已发布的数据

- [版本清单](https://libremaker700.github.io/prime-video-cn-catalog/version.json)
- [兼容目录](https://libremaker700.github.io/prime-video-cn-catalog/catalog.json)
- [Oracle 资料镜像版本清单](https://objectstorage.us-phoenix-1.oraclecloud.com/n/ax9hsd5wg0wx/b/prime-video-catalog/o/catalog/version.json)
- [Oracle 资料镜像目录](https://objectstorage.us-phoenix-1.oraclecloud.com/n/ax9hsd5wg0wx/b/prime-video-catalog/o/catalog/catalog.json)
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

## 中文名称自动查询与复用

`Update verified Chinese titles` 在 GitHub 每天北京时间约 11:47 检查，按
`data/title-cache.json` 独立的七天间隔执行名称查询，不依赖官网全量采集成功。
手动 Run workflow 可提前执行，但不能绕过已核验缓存、预算和待核验冷却期。

- `verified*` 或用户确认的中文名直接复用；作品 ID、原名、类型或首播年份变化后，旧缓存不跨作品套用。只增加季度不重查整剧名称。
- 未核验作品先尝试中文维基；动画另查 Bangumi 的原名、TV 类型和首播日期。修复旧查询同时拼接“电视剧 动画”导致漏查的问题。
- 普通资料源未通过时，使用仓库 Secret `TAVILY_API_KEY` 做一次 basic 搜索，关闭自动参数、AI 总结和自动重试。搜索结果仅用于定位来源，不直接当成身份核验。
- 目前自动核验支持中文维基定义段和 Bangumi 结构化动画条目。严格核对原名、类型、首播年份；国内其他站点结果进入待核验记录，不凭摘要猜片名。不具备可靠原名或首播年份时保留旧名。
- 已有中文名与新来源名不同时，只有旧名也出现在定义段中才保留并标为核验通过；否则留待确认。用户指定名称始终优先。
- 默认每轮最多尝试 12 部，最多 20 部；本流程每月默认预留最多 100 次搜索，配置硬上限 200。免费账户其他程序的用量不在本流程统计内。Variables 为 `TITLE_LOOKUP_LIMIT` 和 `TITLE_LOOKUP_MONTHLY_LIMIT`。
- 每次请求前保存预算预留；网络失败也不自动退还本地预算。已核验记录长期复用，未核验记录至少 30 天后重查，先轮转尚未尝试的作品。缓存和预算在工作流后续失败时仍单独提交，避免重复消费。
- 仅修改名称、别名及来源，原有作品身份、季度、简介、海报及播放映射均保留。生成新目录版本但不推进官网全量采集的 `last_success_at`。发布继续校验哈希、全部图片，Oracle 清单最后切换。

`logs/title-lookup.json` 记录本轮请求数、直连核验数、剩余待核验数和实际名称变更。
Tavily 是网页搜索服务；本步骤没有接入外语简介的 AI 翻译，也不把搜索摘要作为分季简介。

## Gemini 接入验证

Actions 中的 `Test Gemini Chinese title search` 是手动触发的一部作品诊断。
它通过仓库 Secret `GEMINI_API_KEY` 调用 Gemini 2.5 Flash，启用 Google Search，
核验 Vinland Saga（2019 年电视动画第一季）的中文名、身份及中文简介。
每次最多一次生成请求，不自动换模型或重试；额度或权限不足时明确失败。
报告检查响应中的搜索查询、引用来源及身份字段，诊断不等于全目录采集验收。
密钥不写入仓库或报告；报告仅保留经过脱敏的选定字段。
该诊断不更新 `docs/`、成功时间、Oracle 或 App，现有每周采集暂未启用 Gemini。

2026-10-29 实测：Secret 认证和模型列表请求为 HTTP 200，但 2.5 Flash 生成请求返回
HTTP 404 / `NOT_FOUND`，服务明确说明该模型不再对新用户开放。模型出现在列表中
不代表账户可以实际生成。诊断保持失败，不自动切换到可能产生费用的新模型。

## Oracle Object Storage 分发

Oracle 镜像只保存公开 JSON 与图片，使用现有账号的对象存储免费额度，不依赖账号内的 VPN 虚拟机、SSH 密钥或虚拟机端口。免费额度有容量、请求数和流量限制，不代表无限免费；用量以账号控制台及 Oracle 当前规则为准。当前个人用目录只有约 13 MB。

工作流在数据校验与提交后调用 `scripts/publish_oci.py`。它只选取校验通过的图片、固定版本目录、兼容目录和版本清单；不上传其他仓库文件。图片与目录先上传并校验，`version.json` 最后上传，失败时不推进镜像指针。图片及固定版本文件可长期缓存，版本清单使用 `no-cache`。版本和目录哈希一致时不重复上传。

配置项：

- 仓库 Variable `OCI_MIRROR_ENABLED=true`。
- 仓库 Variable `OCI_CATALOG_PUBLIC_URL`：资料下载前缀，以上两个镜像地址去掉文件名后的 HTTPS 地址。
- 仓库 Secret `OCI_CATALOG_UPLOAD_URL`：仅允许向 `prime-video-catalog` 桶的 `catalog/` 前缀写入的 Oracle PAR 上传地址。它是凭据，不能写入源码、App 或日志。

首次配置的只写授权在 **2027-10-29 02:30 UTC（北京时间 10:30）**到期，到期前需要在 Oracle 控制台续设受限授权并更新 Secret。工作流不会自行扩大权限或创建新凭据。App 始终使用不含凭据的公开下载地址。

手动选择 Actions 的 `publish` 模式会强制重新同步现有资料并部署，便于修复镜像；不触发官网采集。成功后仍须在电视自己的网络下测试，桌面电脑的下载测试不能替代电视关闭 VPN 的验收。
