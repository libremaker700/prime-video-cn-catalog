# Prime Video 中文影视资料同步

为个人电视 App 提供经身份匹配的中文名、英文/原文名、逐季简介、日期、分类和16:9海报。仓库发布公开资料，不包含播放地址、片源会话、收藏、播放进度或上传凭据。

## 发布与 App

GitHub Pages：`https://libremaker700.github.io/prime-video-cn-catalog/`。
Oracle 镜像：`https://objectstorage.us-phoenix-1.oraclecloud.com/n/ax9hsd5wg0wx/b/prime-video-catalog/o/catalog/`。
App自动下载使用GitHub；Oracle保留独立手动网络测试。

App先读version.json，再读releases/<SHA256>/catalog.json，核对目录和全部海报后原子切换，失败继续使用有效旧版。图片以内容哈希命名，逐张解码，宽至少960、16:9。App版本和资料版本独立，不根据未经片源核验的新发现条目自动增加可播放作品。

## 资料身份和 App 映射

保留156条初始公开资料，基础快照为data/fallback_catalog.json；快照不等于一次新官网采集，来源和时间按真实情况保留。

data/app-metadata.json只保存App已有官方编号、英文原名和官方分季链接，不包含片源vodId或媒体URL。App项目用tools/prime-official/export-cloud-metadata.py导出。云端scripts/import_app_metadata.py按官方编号和作品类型补缺失原名、分季ID，统一电影日期/分类字段，不编造身份或推进采集成功时间。

逐季抓取支持ASIN和官方compact ID，精确核对季度号；旧ASIN不可用时尝试同季已保存的官方链接。只有一个链接不等于该季内容已更新。App 1.3.36起接收云端英文名、旧版嵌套电影日期/分类，以及通过原有compact链接核验的季度资料。界面、线路、选集和个人数据仍由App管理。

官网规范地址的跳转最多跟随4次，只接受同站HTTPS（Prime的带www和不带www域名视作同站）；跨站、降级和登录外站跳转拒绝。单个作品404或正常302不计为整个网站不可用，仍核对最终页面的作品及季度身份。

## 每周采集与失败保留

Update Prime Video CN Catalog每天北京时间约11:17检查，默认完整周期604800秒。

- 最近完整采集未满7天且没有到期部分失败任务时跳过。
- 成功条目7天复用；失败或部分成功条目6小时后可重试，日检会补它们；缺身份条目按7天轮转。
- 正常条目可更新，失败条目保留旧资料；仍发布包含全部旧作品的完整目录，标refresh_status=partial。
- 部分更新只推进last_partial_at/last_attempt_at，不推进last_success_at。App区别显示完整与部分更新。
- 全部来源失败、图片/目录校验失败或作品数量异常时不发布，原版本指针保留；失败诊断由Actions artifact保存。
- 每轮默认20分钟采集预算，给验证和发布留出时间；到限保留未完成作品，已核验结果按部分更新发布。下轮优先处理尚未尝试及最早尝试的作品，发布顺序保持不变。可用SYNC_BUDGET_SECONDS设置60～1800秒；请求及重试等待受剩余时间约束。

```bash
python -m unittest discover -s tests -v
python scripts/import_app_metadata.py
python scripts/sync.py --due
python scripts/sync.py --force --no-discovery
python scripts/validate.py
python scripts/prune_publication.py
```

手动mode=sync强制采集；mode=publish只对齐、验证并发布现有资料。TMDB和翻译可按需配置TMDB_API_KEY、TRANSLATE_PROVIDER、DEEPL_API_KEY或TRANSLATE_API_KEY/TRANSLATE_MODEL/TRANSLATE_BASE_URL，见config/translation.md。

## 中文名查询与双 Tavily 密钥

Update verified Chinese titles每天北京时间约11:47检查，名称有独立7天周期。已核验名称、用户偏好和缓存直接复用；未核验条目先查中文维基/Bangumi，可靠来源仍不足才调用Tavily。核对原名、年份、类型，不用摘要猜片名或代替分季简介。

Actions Secrets：TAVILY_API_KEY_1为主密钥，TAVILY_API_KEY_2为备用。两者调用同一个Tavily接口，是两份凭据。相同密钥去重；旧单密钥环境仍兼容TAVILY_API_KEY。

- 主密钥遇到401/403认证或432/433额度错误，可切备用；每个密钥最多一次尝试，后续沿用可用备用。
- 429限速、超时和服务端错误停止本轮付费请求，不循环密钥绕过限速，不升级搜索档位或开启付费。
- 每次实际HTTP请求发送前保存预算，包括失败主请求和备用请求；两个密钥共用月预算，并分别记录槽位用量。
- 默认每轮尝试12部、上限20；每月预留100次请求、上限200。Variables为TITLE_LOOKUP_LIMIT和TITLE_LOOKUP_MONTHLY_LIMIT；其他程序消耗的账户额度不在本账本内。
- 缓存按作品身份长期复用，未核验记录至少30天后重查。review_saved=true、limit=0只重查已保存来源，不发新搜索。
- python scripts/title_lookup.py --check-keys只调用官方/usage验证密钥，输出槽位和HTTP状态，不输出密钥或账户响应，也不搜索。工作流会自动检查。

## 文件保留

正式保留两个工作流：资料采集/发布、名称查询/发布。旧Gemini、Google AI概览、一次性Tavily探测及旧全量导入副本已移除。当前及前一个不可变目录和它们引用的全部图片保留，其余旧目录/无引用哈希图片由prune_publication.py清理。Git历史可恢复旧文件。

名称缓存、预算、偏好规则、基础资料、App映射、镜像发布工具保留。

## Oracle 镜像

图片和不可变目录先上传核验，version.json最后切换。Variable OCI_MIRROR_ENABLED=true启用，OCI_CATALOG_PUBLIC_URL设置公共前缀，Secret OCI_CATALOG_UPLOAD_URL保存受限写入PAR地址，不使用虚拟机或SSH密钥。

现有只写PAR授权到期时间2027-10-29 02:30 UTC，届时由用户续设授权并更新Secret，代码不会扩大权限。电视可达性需在真实电视网络检查。

本项目不隶属于Amazon或TMDB；补充资料保留真实来源与许可要求。密钥不进入公开仓库、日志、目录或APK。
