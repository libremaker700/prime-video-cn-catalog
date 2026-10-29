# 翻译服务配置（自动翻译到底怎么运行）

仅当某季/某作品**没有中文简介、但有同季外语简介原文**时才触发翻译；
没有原文时标 `missing`，未配置翻译服务时标 `translation_pending`，绝不用全剧简介顶替分季简介。

在 GitHub 仓库 Settings → Secrets and variables → Actions 中配置以下任一组即可
（本地运行则写进环境变量或 `.env`，`.env` 已被 .gitignore 排除，绝不提交）：

方案 A：DeepL（推荐，影视简介质量稳定）
- `TRANSLATE_PROVIDER=deepl`
- `DEEPL_API_KEY=<DeepL API 的 Auth Key>`（免费版 Key 以 `:fx` 结尾，程序自动切换免费端点）

方案 B：OpenAI 兼容接口（OpenAI / 自建 / 其他兼容网关）
- `TRANSLATE_PROVIDER=openai`
- `TRANSLATE_API_KEY=<API Key>`
- `TRANSLATE_MODEL=gpt-4o-mini`（可换）
- `TRANSLATE_BASE_URL=https://api.openai.com/v1`（可换成兼容网关地址）

可选补充来源（与翻译无关）：
- `TMDB_API_KEY=<TMDB API Key>`：提供 zh-CN 简介与整剧 16:9 backdrop。
  注意：TMDB 分季图片接口官方只返回分季海报（竖版），程序不会用它充当横屏图；
  TMDB 整剧 backdrop 只标为整剧通用图（scope=series），不标成任何一季的专属图；
  所有 TMDB 来源在记录里标 `source_type: tmdb`，与“经 Prime Video 官方资料核验”分开记录。

翻译结果在记录中标 `synopsis_source_type: machine_translation`，
并保留原文来源链接与所用 provider/model，便于人工抽查。
