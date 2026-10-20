# 游戏价签 · Steam 游戏比价雷达

中文静态比价网站：搜索 Steam 国区游戏目录，比较 **Steam 国区现价** 与 **杉果、SteamPY、SteamCici 的 CDK 报价**；已采集详情的游戏还能查看版本、捆绑包、DLC 和小黑盒历史走势。支持白天／黑夜主题，主题和关注清单保存在当前浏览器。

在线网站：[游戏价签](https://lyhqwq123.github.io/steam-price-radar/)。源码仓库：[LYHqwq123/steam-price-radar](https://github.com/LYhqwq123/steam-price-radar)。

网页只读取已发布的数据快照；采集由 GitHub Actions 定时执行，无需常驻服务器。**全库价格是分批快照，不是实时行情；目录已收录也不等于本轮一定取得价格。**

## 全库数据怎么组织

Steam 国区公开目录约 18 万款游戏。为了不把几十万个对象塞进一个页面，数据分为四层：

| 数据 | 内容与节奏 |
| --- | --- |
| 搜索目录 `catalog-index.json` | 紧凑数组 `[appid, name, nameEn?, aliases?]`，按 AppID 排序；每周日 18:23 UTC（北京时间周一 02:23）接续游标继续遍历，单次可走完整公开搜索结果 |
| 全库现价 `price-shards/price-NN.json` | 64 个分片，按 `appid % 64` 划分；每个分片保存整数分币值、原价、折扣、状态和独立检查时间 |
| 分片清单 `price-manifest.json` | 汇总覆盖数、有效现价数、缺失分片、状态计数和最新／最旧检查时间；页面先读它，再按 6 个并发懒加载分片 |
| 游戏详情 `prices.json` | 已采集的版本、DLC、CDK 与历史；每小时第 7、22、37、52 分钟轮换约 20 款，新增内容不会覆盖旧报价 |

现价工作流每 2 小时的第 17 分钟运行一次（`17 */2 * * *`）。GitHub Actions 启动 8 个采集 worker，每个 worker 负责连续 8 个分片；合并后重建清单并直接部署。每轮从同一工作流、默认分支上最近成功的合并快照恢复数据，逐款保留较新的检查结果；本轮合并快照保存为保留 14 天的 Actions artifact，下轮继续补采。每周保存一次 Git 基线，避免仓库每两小时膨胀。代码更新部署也恢复最新快照，不会把现价倒退到每周基线。

目录尚未完整时，现价只覆盖当前已同步部分；页面顶部会显示「可搜索 / 全库约」和分片进度。目录同步结束后会重建覆盖清单并排队触发现价采集与部署；首次部署也可在 Actions 手动运行一次 `Refresh all Steam prices and deploy`，并保留 `save_baseline`，建立已采集数据的基线。免费、未发售或国区不可售条目可能没有可用价格。

### 前端约束与过期规则

- 搜索最多返回 2,000 条并按相关度排序，超出时明确提示补充关键词或输入 AppID；每次翻页 30 条。
- 默认列表只展示已采集详情的游戏和折扣最高的 120 款目录游戏，避免首屏渲染 18 万行。
- 目录游戏的价格来自分片；没有有效现价时只嵌入 Steam 官方价格组件，不猜测价格，也不复用其他版本的资料。
- 全库现价超过 **8 小时**未更新即视为过期，不参与现价卡、最低价与排序；已采集详情最多保留 **48 小时**。双击预览按采集时快照展示并标注，不冒充当前价。
- 未返回 `price_overview` 不代表游戏免费；接口失败时保留旧金额与原始检查时间，并标记过期或错误。

## 本机预览

双击根目录的 **`打开预览.html`**，或运行 `启动预览.cmd`，无需安装依赖。该文件内嵌目录、清单、全部分片和详情快照，更新数据后需重新生成。

预览最新 `dist/`：

```powershell
python scripts/serve.py --open
```

默认地址为 `http://127.0.0.1:8765/`，按 `Ctrl+C` 停止；端口占用可加 `--port 8766`。预览服务只绑定本机地址，不提供即时查价代理。

## 手动采集与检查

Python 采集脚本只使用标准库，无需 `pip install`；网页检查需要 Node.js，无需 npm 包。

```powershell
# 继续同步目录；--pages 2500 足够单次遍历约 18.4 万款
python scripts/sync_catalog.py --pages 2500 --delay 0.6

# 单个 worker 试跑现价（每批 100 个 AppID，可用 --max-batches 限制时长）
python scripts/direct_prices.py --worker-index 0 --worker-count 8 --shard-count 64 --max-batches 20

# 本机完整刷新可去掉 worker 参数；完成后重建浏览器清单
python scripts/build_data.py

# 详情轮换；指定 AppID 可优先补齐
python scripts/refresh_prices.py --batch-size 20
python scripts/refresh_prices.py --appids "386940,620"

# 校验、测试与打包
node scripts/check_site.mjs
python -m unittest discover -s tests -v
python scripts/package_preview.py
```

目录脚本支持 `--output`、`--catalog`、`--pages`、`--page-size`、`--api-page-size` 和 `--delay`。配置 **`STEAM_WEB_API_KEY`** 后改用官方 `IStoreService/GetAppList` 并按修改时间增量同步；不配置时匿名遍历公开搜索页。密钥只在采集进程中使用，不写入网页、数据文件或日志。

`direct_prices.py` 支持 `--worker-index/--worker-count` 八路分片采集，也支持 `--max-seconds`、`--max-batches`、`--delay`；`--write-manifest` 会在写分片后重建清单。`refresh_prices.py` 保留未轮到的游戏及原报价时间，运行前不要删除 `prices.json`、目录或分片。

## GitHub Pages 与自动任务

仓库需要在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。四份工作流共用并发锁并允许最多 100 个待执行任务排队，默认分支上依次执行，新增详情任务不会取消已有的待发布任务：

| 工作流 | 作用 |
| --- | --- |
| `pages.yml` / Deploy GitHub Pages | 页面代码、脚本或核心目录变化后的检查与发布 |
| `refresh-prices.yml` / Refresh all Steam prices and deploy | 每 2 小时采集全库分片、重建清单并部署；手动运行可立即保存完整基线 |
| `refresh-details.yml` / Refresh detailed game data | 每 15 分钟采集约 20 款游戏的版本、DLC、CDK 与历史；详情提交后由下一次现价部署带上线 |
| `sync-catalog.yml` / Sync full Steam catalog | 每周接续遍历公开目录、重建覆盖清单并提交游标，再排队触发现价采集与部署 |

可选官方目录密钥在 **Settings → Secrets and variables → Actions → New repository secret** 中添加，名称为 **`STEAM_WEB_API_KEY`**。自动提交需要 `contents: write`，恢复历史构建快照需要 `actions: read`，目录任务排队触发采价需要 `actions: write`，Pages 发布需要相应 Pages 权限，工作流已声明。

定时任务只在默认分支执行，可能因 GitHub 排队、商家限流或网络限制而延迟。公开仓库长期无活动时，GitHub 可能停用计划任务；可在 Actions 重新启用。页面显示每条报价的实际检查时间；本地独立预览明确保留为采集时快照。

## 价格与版本怎么比较

| 项目 | 含义 |
| --- | --- |
| Steam 直售价 | Steam 中国大陆商店人民币报价；免费游戏按 0 元显示，无国区售价和接口失败另行标注 |
| 各版本 / DLC | 标准版、豪华版等购买包按 SubID 区分；捆绑包按 BundleID、DLC 按其自身 AppID 展示，不复用标准版的 CDK 价格 |
| CDK 报价 | 只比较匹配同一内容的激活码参考或可购报价，不计礼物代购、余额购及其他版本 |
| 可购 / 参考 | 库存与商品状态已核实才标可购；库存或激活地区未确认时仅作参考，不能承诺可立即成交 |
| 异常 / 过期 | 异常金额置空并排除比价；过期或失败后保留的旧报价标明原检查时间，不参与当前最低价 |
| 本体价格走势 | 小黑盒返回的 Steam 国区标准版记录，支持 30 / 90 / 180 天、全部记录、放大及逐点查看；其他版本与 DLC 不冒用该曲线 |

**SteamPY 当前不能提供已核验的实时卖单最低价。** 官网实际卖单与详情接口匿名访问返回 401；本项目使用公开插件接口的参考报价，标明检查时间、匹配版本和未知库存。未经核实的 **≤1 元**微额报价隐藏金额；缺失、非数字、版本不匹配和异常报价也不进入最低参考价。正常大幅折扣不会仅因折扣比例低就被剔除。

SteamCici 使用公开列表的 `lastLowSellPrice` 平台低价参考，库存和可成交状态无法确认；商品入口打开平台首页，需要搜索游戏名。杉果商品采用已核验 SKU，库存未知时同样标为参考。

Steam 官方接口失败时，可以使用小黑盒公开国区参考价，并明确标注来源。不同地区、币种、版本和 DLC 的金额独立处理，不用外区换算价代替国区报价。捆绑包显示整套公开报价，已有部分内容时个人补齐价可能不同；DLC 一般需要游戏本体。

历史图的“全部”和“收录区间最低”只针对已取得的真实记录，不保证覆盖发售以来全部历史。请求失败时保留原历史并标记缓存状态，不补造走势。SteamDB 仅提供外部核对入口。本站自行积累的可购 CDK 日记录单独展示，不混入 Steam 曲线。

报价不叠加个人优惠、新客券、会员券或支付活动。购买前请核对商品版本、所含内容、激活地区、库存及最终结算金额。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `dist/` | 发布到 Pages 的静态网页、样式、脚本与数据 |
| `dist/data/catalog-index.json` | 紧凑 v2 搜索目录、覆盖状态与分页游标 |
| `dist/data/price-manifest.json` | 64 个现价分片的清单、覆盖统计与检查时间 |
| `dist/data/price-shards/price-NN.json` | 按 AppID 取模划分的全库现价分片 |
| `dist/data/prices.json` | 已采集的版本、DLC、CDK 报价与历史 |
| `打开预览.html` / `启动预览.cmd` | 内嵌全库目录、分片与详情的可双击快照 |
| `data/catalog.json` | 核心游戏中文名称、别名和已核验的商品映射 |
| `scripts/sync_catalog.py` | 公开搜索分页、可选官方目录全量 / 增量同步 |
| `scripts/direct_prices.py` | 全库现价分片采集、worker 划分、失败保留与断点接续 |
| `scripts/price_format.py` | 分片紧凑格式、整数分币值与清单构建 |
| `scripts/catalog_format.py` | 目录 v2 数组格式读写与 AppID 提取 |
| `scripts/build_data.py` | 校验分片并重建 `price-manifest.json` |
| `scripts/refresh_prices.py` | 详情轮换、版本 / DLC / CDK / 历史补全 |
| `scripts/collect_prices.py` / `providers.py` | 商家采集、标准版匹配、报价归一化与异常处理 |
| `scripts/editions.py` | 精确购买包、捆绑包、DLC 资料与独立报价 |
| `scripts/price_history.py` | 小黑盒国区历史、日期归一化与缓存 |
| `scripts/package_preview.py` / `serve.py` | 生成独立快照、本机 HTTP 预览 |
| `scripts/check_site.mjs` / `scripts/browser_check.mjs` | 数据契约与静态资源检查、无依赖 Chromium 交互 QA |
| `tests/` | 目录、分片、采集、轮换与数据校验的 Python 测试 |
| `.github/workflows/` | 页面部署、全库现价、详情轮换、目录同步四份工作流 |

核心目录可继续手动补充中文别名和已验证的商家映射；未知映射留空，避免模糊标题把其他版本凑进比价。

## 参考

[Steam 官方目录接口](https://partner.steamgames.com/doc/webapi/IStoreService)、[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)、[GitHub Actions 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

商店与小黑盒的数据接口由各自平台运营，不是本项目保证长期可用的开放 API。接口结构或访问限制改变时，需要更新对应模块。
