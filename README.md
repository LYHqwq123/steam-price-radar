# 游戏价签 · Steam 游戏比价雷达

中文静态比价网站：搜索 Steam 游戏目录，分别比较 **Steam 国区直售价** 与 **杉果、SteamPY、SteamCici 的 CDK 报价**，查看标准版、豪华版、捆绑包及 DLC，并显示来自小黑盒的本体标准版价格走势。支持白天／黑夜主题，主题和关注清单保存在当前浏览器。

在线网站：[游戏价签](https://lyhqwq123.github.io/steam-price-radar/)。源码仓库：[LYHqwq123/steam-price-radar](https://github.com/LYHqwq123/steam-price-radar)。首次部署、自动采价和目录同步已于 2026-10-17 在 GitHub Actions 实际运行成功；后续状态可在仓库 Actions 查看。

网页读取已发布的目录和价格 JSON；自动同步由 GitHub Actions 执行，无需常驻服务器。**目录可搜索，不代表每款游戏已有价格；定时快照也不是全库实时行情。**

## 本机预览

双击根目录的 **`打开预览.html`**，或运行 `启动预览.cmd`，即可查看独立快照，无需安装依赖。更新数据后需重新生成预览文件。

需要预览最新 `dist/` 时，在项目目录运行：

```powershell
python scripts/serve.py --open
```

默认地址为 `http://127.0.0.1:8765/`，按 `Ctrl+C` 停止；端口占用可加 `--port 8766`。不要直接双击 `dist/index.html`，浏览器会限制其读取本地 JSON。预览服务只绑定本机地址，不提供即时查价代理。

## 目录与更新节奏

| 数据 | 同步方式与范围 |
| --- | --- |
| 搜索目录 | 每天同步一批 Steam 国区公开搜索可见的游戏本体，保存分页游标，下次接续；不含受限条目、DLC、软件和捆绑包 |
| 完整度 | `catalog-index.json` 用 `status`、`complete`、`scope` 和说明标注实际覆盖范围；`partial` 表示尚在补全，不等于 Steam 全库 |
| 核心游戏现价 | `data/catalog.json` 当前配置 13 款核心游戏，计划每 15 分钟刷新 |
| 目录游戏现价 | 每轮额外选取 3 款目录游戏采价并保留旧结果，逐步回填；不同游戏更新时间不同，不保证每款都在 15 分钟内更新 |
| 版本与 DLC | 按精确购买包或 AppID 采集独立价格；每款游戏每轮最多补取 30 项待核验 DLC 资料，其余继续排队 |
| Steam 历史 | 小黑盒国区人民币记录缓存 24 小时，避免每轮重复请求；不与 CDK 历史混用 |

匿名公开目录同步无需密钥。配置 **`STEAM_WEB_API_KEY`** 后，目录脚本改用官方 `IStoreService/GetAppList`：首次分页全量同步，完成后按修改时间增量更新。这里的全量指官方商店接口返回的游戏本体，仍不代表所有已下架、受限或非游戏应用。密钥只在采集进程中使用，不写入网页、目录 JSON 或日志。

未采价的目录游戏显示“尚未采集价格”，并提供 Steam 商店入口；不会用估算价格或模拟曲线填充。API 或某页失败时保留已有数据及检查时间，不跳过失败页，也不将失败标成同步完成。

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

## 手动同步与检查

Python 采集脚本使用标准库，无需 `pip install`；检查页面另需 Node.js，无需安装 npm 包。在项目目录依次运行：

```powershell
# 扩充下一批目录；首次可用 --pages 50，默认每页最多 100 项
python scripts/sync_catalog.py --pages 30

# 核心游戏 + 3 款目录游戏逐步采价
python scripts/refresh_prices.py --batch-size 3

# 按需强制刷新历史或版本资料
python scripts/refresh_prices.py --batch-size 3 --refresh-history --refresh-content

# 验证并生成可双击预览
python -m unittest discover -s tests -v
node scripts/check_site.mjs
python scripts/package_preview.py
```

目录脚本支持 `--output`、`--catalog`、`--pages`、`--page-size`、`--api-page-size` 和 `--delay`。本机可通过环境变量配置 `STEAM_WEB_API_KEY`，不要把密钥写进源文件或提交到仓库。

`refresh_prices.py` 保留未轮到的游戏及其原报价时间；运行前不要删除 `prices.json` 或 `catalog-index.json`。如只需按人工目录采集核心游戏，也可运行 `python scripts/collect_prices.py`。重新载入网页只读取已发布快照，不会即时查询商家接口。

## GitHub Pages 与自动任务

仓库需要在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。随后可在 Actions 手动运行以下工作流：

| 工作流文件 / 名称 | 作用 |
| --- | --- |
| `pages.yml` / Deploy GitHub Pages | 页面代码变动后的检查与发布 |
| `refresh-prices.yml` / Refresh prices and deploy | 每小时第 7、22、37、52 分钟执行 `refresh_prices.py --batch-size 3`，提交更新并直接部署 |
| `sync-catalog.yml` / Sync game catalog and deploy | 每天 UTC 19:19，即北京时间次日 03:19，执行下一批目录同步，再提交并部署；支持手动运行 |

可选官方目录密钥在 **Settings → Secrets and variables → Actions → New repository secret** 中添加，名称为 **`STEAM_WEB_API_KEY`**。不配置时走匿名公开搜索；其他常规匿名采集不依赖这项密钥。

自动提交需要 `contents: write`，Pages 发布需要相应 Pages 权限，工作流已声明。三份工作流共用并发锁。采集工作流直接完成部署，不依赖 `GITHUB_TOKEN` 提交再次触发 `push` 工作流。

定时任务只在默认分支执行，可能因 GitHub 排队、商家限流或网络限制而延迟。公开仓库长期无活动时，GitHub 可能停用计划任务；可在 Actions 重新启用。页面显示每条报价的实际检查时间；在线报价超过有效期时不再参与当前最低价，本地独立预览则明确保留为采集时快照。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `dist/` | 发布到 Pages 的静态网页、明暗样式、脚本与 JSON |
| `dist/data/catalog-index.json` | 自动同步的独立搜索目录、覆盖状态与分页游标 |
| `dist/data/prices.json` | 已采价游戏、独立版本 / DLC 报价、历史及轮转进度 |
| `打开预览.html` / `启动预览.cmd` | 可双击的独立快照及启动入口 |
| `data/catalog.json` | 核心游戏中文名称、别名和已核验的商品映射 |
| `scripts/sync_catalog.py` | 公开搜索分页、可选官方目录全量 / 增量同步 |
| `scripts/refresh_prices.py` | 核心游戏刷新与目录游戏轮转回填 |
| `scripts/collect_prices.py` / `providers.py` | 商家采集、标准版匹配、报价归一化与异常处理 |
| `scripts/editions.py` | 精确购买包、捆绑包、DLC 资料与独立报价 |
| `scripts/price_history.py` | 小黑盒国区历史、日期归一化与缓存 |
| `scripts/package_preview.py` / `serve.py` | 生成独立快照、本机 HTTP 预览 |
| `tests/` / `scripts/check_site.mjs` | 采集逻辑、目录覆盖、数据与静态资源检查 |
| `.github/workflows/` | 页面部署、现价刷新、目录同步三份工作流 |

核心目录可继续手动补充中文别名和已验证的商家映射；未知映射留空，避免模糊标题把其他版本凑进比价。

## 参考

[Steam 官方目录接口](https://partner.steamgames.com/doc/webapi/IStoreService)、[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)、[GitHub Actions 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[GITHUB_TOKEN 的触发限制](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow)。

商店与小黑盒的数据接口由各自平台运营，不是本项目保证长期可用的开放 API。接口结构或访问限制改变时，需要更新对应模块。
