# Prime Video 中文影视资料自动同步

为 Android TV App 提供 Prime Video 作品的中文资料目录：国内常用中文名、官方译名、
原名/别名、按季中文简介、详情与 16:9 横屏海报。播放线路仍由 App 原有片源提供，
本仓库只含采集程序与公开资料，不含任何凭据、签名密钥或片源会话配置。

## 发布地址（GitHub Pages 启用后）
- `https://libremaker700.github.io/prime-video-cn-catalog/catalog.json`
- `https://libremaker700.github.io/prime-video-cn-catalog/version.json`
- 海报：`https://libremaker700.github.io/prime-video-cn-catalog/images/<内容SHA-256>.jpg`

## 首版数据来源（重要）
首版不是从官网重新抓取的：它导入了用户 App 现有资料快照
（`data/bootstrap-catalog.json`，2026-10-29 导出，经 App 内官方 ID 映射），
再与中文维基核验结果合并。快照状态在每条记录里标 `snapshot_status`，
来源标 `source_type: app_snapshot_official_mapping`，不冒充实时抓取。
导入适配：`scripts/import_bootstrap.py`；导入后快照同时存为
`data/fallback_catalog.json`，作为后续抓取失败时的回退基线。

首版覆盖：作品 156（快照 154 + 维基独有 2）｜官方 ID 已映射 121｜待核验身份 33｜
季度条目 225｜分季中文简介 109｜横屏图 127 张去重文件（整剧/电影级 18、分季专属 109），
逐张解码验真（宽 ≥960、16:9 容差内、SHA-256 与文件名一致）。

## 数据规则
- 身份：官方 ID 优先，其次原名+类型；33 条无官方 ID 的保持 `pending_review`，不编造。
  `seedYear` 只是 App 当前收录季度年份（单独存并标注 scope），不冒充首播年份；
  `firstAirYear` 有官方第一季年份才填。
- 中文名：用户 App 偏好（`data/title-overrides.json`，如 Fallout→“辐射”、
  Reacher→“侠探杰克”）优先作显示名，其他地区译名进别名；自动检索的名称须
  原名+年份+类型+定义句多信号核验，未达阈值记 `pending_review`，繁简转换不代替常用名检索。
- 简介：电视剧逐季保存；`officialMetadata` 只属于其标注的那一季，不套用到其他季；
  没有中文但有同季外语原文时走翻译（配置见 `config/translation.md`），
  无原文标 `missing`、未配置翻译标 `translation_pending`。
- 海报：分季图只挂对应季度（`poster_scope: season`），整剧图标 `scope: series`；
  缺失时保留已有正确图片，不以别季/别作图片补位，不拉伸竖版图。
  TMDB 仅作可配置补充来源且如实标注 `source_type: tmdb`：其分季图片接口只返回
  竖版海报，不能充横屏图；整剧 backdrop 只作整剧通用图，不标为某季专图；
  TMDB 来源不等于经 Prime Video 官方核验，核验状态另行记录。
- 更新与发现分开：定时任务只刷新已有种子；新作品发现须记录发现来源并完成
  官方身份核验后才入目录，任意搜索结果不直接导入。
- 安全发布：新数据为空或条目骤降 >50% 时中止并保留上一版；
  `version.json` 哈希与 catalog 内容绑定。

## 运行
```bash
pip install requests Pillow PyYAML
python scripts/import_bootstrap.py <资料包解压目录>   # 导入 App 快照（首版/回退基线）
python scripts/sync.py --force   # 立即刷新已有种子一次
python scripts/sync.py --due     # 距上次成功 <72 小时自动跳过（定时任务用）
python scripts/validate.py       # 校验 JSON/图片一致性、横屏比例与 scope 纪律
```
可选仓库 Secrets：`TMDB_API_KEY`、`TRANSLATE_PROVIDER` + `DEEPL_API_KEY`
或 `TRANSLATE_API_KEY/TRANSLATE_MODEL/TRANSLATE_BASE_URL`（见 config/translation.md）。

定时：GitHub Actions 每日检查、满 3 天才真正更新，支持手动触发（见 .github/workflows/update.yml）。
