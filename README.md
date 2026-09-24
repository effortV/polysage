# HDU×恒诺 - 包装膜（AI4S）—— 包装膜配方 AI 降本平台（PolySage）

一个能自己跑完 **查资料 → 搞懂现配方 → 找替代料 → 出降本配方 → 给实验意见 → 数据回灌建模 → 扫描全部可行配方 → 推荐下一轮** 的智能体流水线。
LLM 用硅基流动（SiliconFlow）上的 `deepseek-ai/DeepSeek-V4-Pro`，每个结论都带出处（DOI / URL / 文件），价格分“网查参考价 / 甲方实价”两档。

仓库：https://github.com/effortV/polysage

## 0 部署

### 0.1 本机作为临时服务器（当前方式）

Streamlit Community Cloud 的磁盘不持久（重新部署 / 休眠唤醒后 `data/`、`knowledge/` 里新写的内容会丢），
所以正式数据放在本机：本机常驻运行，同一局域网的人用 `http://<本机IP>:8511` 访问。

```powershell
cd "D:\桌面\membrane for Packaging\claude"
powershell -ExecutionPolicy Bypass -File .\install_service.ps1     # 注册计划任务并立即启动
```

- `serve.ps1`：服务器模式（绑定 0.0.0.0:8511、关闭热重载、崩溃 5 s 自动拉起），日志在 `data/server.log`。
- `install_service.ps1`：注册三个计划任务——`PolySage-Server`（登录后自动常驻）、`PolySage-Tunnel`（外网隧道）、`PolySage-Backup`（每天 02:30 备份）；
  `-Remove` 卸载。以当前用户运行，不需要管理员。
- `backup.ps1`：把数据库（一致快照）、`data/`、`knowledge/`、`.env`、`.streamlit/secrets.toml` 打成 `backups/polysage-日期.zip`，保留 14 份。
- 让局域网能访问，需要在**管理员** PowerShell 放行端口，并关掉睡眠：

```powershell
netsh advfirewall firewall add rule name="PolySage 8511" dir=in action=allow protocol=TCP localport=8511
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

- **外网访问（隧道）**：`tunnel.ps1` 用 Cloudflare Tunnel 把本机 8511 暴露到公网，计划任务 `PolySage-Tunnel` 登录后自动跑。
  - 必须先在 `.env` 里设 `APP_PASSWORD=你的口令`（打开网页要先输口令），没设它会拒绝开隧道。
  - 不配 token 时是临时隧道：随机网址 `https://xxx.trycloudflare.com`，每次重启会变，当前网址写在 `data/tunnel_url.txt`，侧栏「后台运行」也显示。
  - **固定网址 https://hn.hduai4s.cn（已配好）**：域名 `hduai4s.cn` 托管在 Cloudflare（NS = ada/cartman.ns.cloudflare.com），命名隧道 `hdu-film`，
    配置在 `%USERPROFILE%\.cloudflared\hdu-film.yml`（tunnel id + 凭据 json，勿入库）；`tunnel.ps1` 检测到该文件就走固定模式。
    换机器迁移时：`cloudflared tunnel login` → 把 `.cloudflared` 目录下的 cert.pem、`f1a95f08-…json`、`hdu-film.yml` 拷过去即可。
  - cloudflared 安装：`winget install Cloudflare.cloudflared`。国内访问 Cloudflare 慢的话可换 cpolar / 花生壳，把 `tunnel.ps1` 里的命令换掉即可。
- 停止 / 重启：任务计划程序里结束 `PolySage-Server`，或 `Stop-ScheduledTask -TaskName PolySage-Server` 后 `Start-ScheduledTask -TaskName PolySage-Server`。
- 改了代码后需要重启服务才生效（服务器模式不热重载）。

**迁移到正式服务器**：在新机器上克隆仓库、建 `.venv` 装依赖，把最新一份 `backups/polysage-*.zip` 解压到项目根目录
（覆盖 `data/`、`knowledge/`、`.env`、`.streamlit/`），再按上面方式启动即可；数据库是单文件 SQLite，没有别的状态。

### 0.1b Ubuntu 服务器（2026-09-21 起的正式部署）

正式服务在实验室服务器 `112.15.87.51`（SSH 端口 9010）上：项目目录 `/home/adminisator/data/zzh-new/hn-hdu`，以用户 `adminisator` 运行。

- `deploy/polysage-server.service`：Streamlit 常驻 0.0.0.0:8511（systemd，崩溃 5 s 自动拉起，开机自启）
- `deploy/polysage-tunnel.service`：cloudflared 命名隧道 `hdu-film` → https://hn.hduai4s.cn（配置 `~/.cloudflared/hdu-film.yml`，凭据不入库）
- `deploy/polysage-backup.timer`：每天 02:30 跑 `deploy/backup.sh`（数据库快照 + knowledge + .env + 隧道配置 → `backups/`，留 14 份）
- 安装/更新单元：`sudo bash deploy/install.sh`

常用命令（root）：

```bash
systemctl status polysage-server polysage-tunnel        # 状态
journalctl -u polysage-server -n 100 --no-pager          # 服务日志
systemctl restart polysage-server                        # 改完代码重启
sudo bash /home/adminisator/data/zzh-new/hn-hdu/deploy/update.sh    # 更新代码（只覆盖代码文件，不碰 data/knowledge）并重启
```

笔记本上的计划任务已移除；本地开发仍可 `.
un.ps1`（端口 8511，只连本地库）。

### 0.2 Streamlit Community Cloud（仅演示）

1. 用 GitHub 账号登录 https://share.streamlit.io ，授权 Streamlit 读取仓库。
2. New app → Repository `effortV/polysage`，Branch `master`，Main file `streamlit_app.py`；Advanced settings 里 Python 选 3.12 或 3.13。
3. Advanced settings → Secrets：粘贴本地 `.streamlit/secrets.toml` 的内容（由 `.env` 生成，键名一致；该文件不入库）。
4. Deploy。首次构建约 3～5 分钟。云端磁盘不持久：`data/`、`knowledge/` 随重新部署重置，只适合演示。

## 1 安装与启动

```powershell
cd "D:\桌面\membrane for Packaging\claude"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env      # 已有 .env 则跳过；填 SILICONFLOW_API_KEY
.\run.ps1                        # 浏览器打开 http://localhost:8511
```

也可以直接用 Anaconda 自带的 Python 跑（已验证 Streamlit 1.45 与 1.63 都能用）：

```powershell
pip install -r requirements.txt
streamlit run streamlit_app.py
```

命令行方式（不开界面）：

```powershell
$env:PYTHONUTF8="1"
.\.venv\Scripts\python.exe -m polysage.pipeline run            # 发现阶段 ①～⑥
.\.venv\Scripts\python.exe -m polysage.pipeline status
.\.venv\Scripts\python.exe -m polysage.pipeline learn           # ⑦ 回灌建模（数据放 knowledge/06_实验数据/data.csv）
.\.venv\Scripts\python.exe -m polysage.pipeline scan            # ⑧ 扫描推荐
.\.venv\Scripts\python.exe -m polysage.pipeline review --round 1
```

测试（离线，假 LLM + 假检索源）：`.\.venv\Scripts\python.exe -m pytest -q`

## 2 推荐智能体（V3 主入口）

### 对话出方案（推荐用法）

“智能体”页第一个标签，或“对话”页新建“★ 膜方智能体”会话。直接用自然语言：

- 报材料与价格：`供应商 A 报了国产 7042，密度 0.918，熔点 122，MFR 2.0，到厂价 8300，2026-09-16 报价` → 自动 `register_material`（同类料复用已有代码，如 7042→LLC）→ 价格进价格卡并联动重算。
- 要方案：`请出降本方案` → `recommend_schemes` → 表格：排名 / 配方 / 成本(口径) / 降本 / 五项方向 / 过关把握 / 风险 + 首轮建议。
- 改价格：`7042 涨到 9200 了，重新出一下` → `add_price` 联动 → 重新出方案并对比涨价前后。
- 追问：`第 1 名的透明度为什么是 ≈？依据？` → `explain_scheme` + 知识库检索；`价格变了哪些方案受影响？` → `price_report`。
- 筛替代品：`有什么料可以替代现用 LLDPE？` → `screen_materials`（密度 / 熔点 / MFR 窗口 + 价格）。

DeepSeek 的函数调用参数用 `additionalProperties` 明确类型（否则 SiliconFlow 会返回空对象）；同一轮里同一工具有调用上限，到上限强制作答。

### base（现用膜实测）与两条数据通道

- base 只有 4 个数：拉伸强度、撕裂强度、穿刺力、热封强度（不分 MD/TD；透明度按目视判，不进 base）。“实验与模型 → base 现用膜”表单录入，或对话里直接报数（`set_base`）。
  存 `00_项目/base.yaml`，自动派生过关线（≥ base×0.95 且 ≥ base−1SD；无 SD 时只用 0.95）。有 base 后智能体、模型判定、报告都按它判过关。
  数据表主指标列：`tensile / tear / puncture / seal`（若只填了 MD/TD 明细列会自动取平均折算）。
- 约束页（原料与配方 → 约束）是可视化编辑：现配方比例、分组规则区间图（横条 = 允许区间，圆点 = 现配方值）与可编辑表、单组分范围、成本上限（自动跟随价格或手动）。
- 数据回灌两条通道（同一格式，多一列 `source`）：**推荐方案试验**（选方案编号上传，自动关联，用于预测 vs 实测复盘）与 **自主实验**；
  “方案试验包”按方案生成称料单 + 预填配方的数据表模板。样品 ≥ 15 组可一键建模。

### 对话模型切换

“设置”页可在 SiliconFlow（DeepSeek-V4-Pro）与 ZJU（`https://api.zjumembrane.cn/v1`，`zju-qwen`）之间切换（`.env` 的 `LLM_PROFILE`）；向量/重排固定用 SiliconFlow bge-m3。

### 表单 / 命令行出方案

界面“智能体 → 表单出方案”页 / 命令行 `python -m polysage.recommend`：

- 输入：目标膜（任务书）+ 可用材料清单（物性 + 价格，可勾选/新增/改价或上传 JSON）+ 约束。
- 过程：替代品窗口筛选（以现用 LLDPE 的密度 / 熔点 / MFR 为基准）→ 约束内生成候选（含配方库）→ 按价格卡算成本 →
  LLM 用材料卡/文献卡做五项定性评估 →（有实验模型时）预测 P(过关) → 按“降本额 × 过关把握”排序 → 首轮试验建议。
- 输出：`05_配方库/智能体方案_<时间>.xlsx`（方案 / 替代品筛选 / 价格口径 / 首轮试验建议）+ JSON + `04_价格卡/询价清单.xlsx`。
- 价格联动：任何价格登记（界面、`recommend price`、回填、每周自动网查）→ 全部方案成本重算 → 重排 → `04_价格卡/价格变动报告.md`（成本变动 ≥ 100 元/吨或排名变动 ≥ 3 位会标记）。
- 价格基准：现用 LLDPE 12,000、再生料 8,000（甲方口述）；国产 C4 LLDPE（LLC）8,400 等为假设参考价；成本上限 = 现配方按当前价格卡的成本（自动跟随）。

```powershell
.\.venv\Scripts\python.exe -m polysage.recommend example > input.json   # 示例输入
.\.venv\Scripts\python.exe -m polysage.recommend run input.json         # 出方案
.\.venv\Scripts\python.exe -m polysage.recommend screen --ref LL        # 替代品筛选
.\.venv\Scripts\python.exe -m polysage.recommend price LLC 8200 --type actual --source "供应商A 2026-09-16"
```

## 3 流水线

| 阶段 | 做什么 | LLM 负责 | 产出（knowledge/） |
|---|---|---|---|
| ① 采集 | 8 组主题 × 多源检索（OpenAlex / Crossref / Scopus / Google Patents / 网页），抓 OA 全文与专利全文，入库建索引 | 生成检索式、逐条判相关、抽成文献卡/专利卡 | 02_文献卡、03_专利卡、00_项目/来源清单.xlsx |
| ② 机理 | 回答“为什么是 LLDPE 60 / LDPE+HDPE+再生 40”，缺依据自动补检索 | 带 [S#] 的机理分析与报告 | 00_项目/现配方机理报告.md |
| ③ 寻料 | 按功能找替代料，定向检索 TDS 与文献，网查参考价（带 URL/日期） | 材料卡、价格抽取、新材料建议 | 01_材料卡、04_价格卡、00_项目/候选材料全清单.xlsx |
| ④ 配方 | 约束内生成候选 + 首版 Top 20 → 成本/密度/面积指数 → 排序 | 五项性能定性评估、风险、过关把握 | 05_配方库/Top20候选配方_初步预计.xlsx、04_价格卡/询价清单.xlsx |
| ⑤ 报告 | 汇总为内部版 / 甲方版 Word | 首轮试验配方挑选与建议 | 00_项目/发现阶段报告_*.docx |
| ⑥ DOE | 首轮 24～30 个配方（优先配方 + D-最优补点）+ 数据表模板 | — | 06_实验数据/首轮试验方案.xlsx、数据表模板.csv |
| ⑦ 学习 | 质检 → 每项性能一个模型（Scheffé / GBR / GP）→ LOOCV 验收 | — | 07_模型/*.joblib、模型验证报告.md |
| ⑧ 扫描 | 候选空间（配方库 + 约束内随机 2000）逐个预测 → P(过关) → min 成本 → 利用型 + 探索型 | 化学合理性与货源复核 | 06_实验数据/推荐配方.xlsx / .md |
| ⑨ 复盘 | 实测 vs 预测 → 复盘纪要；收敛判断（连续两轮改善 < 50 元/吨） | 复盘纪要 | 08_复盘/*.md |

未配置 LLM 密钥时：①（检索入库）、④（生成与成本）、⑥⑦ 可运行；判断类步骤跳过或退化，并在日志中提示。

## 4 硬规则

- 数值只能来自 TDS / 文献 / 实价；LLM 只给方向与幅度等级（↑↑/↑/≈/↓/↓↓）。
- 每条结论带 [S#]，出处在报告末尾；可信度：实验 > TDS > 期刊 > 专利 > 行业网站 > 论坛。
- base、约束（`knowledge/00_项目/constraints.yaml`，对应内部技术路线表 4-2）、任务书（`task.yaml`）只从文件读取，不在对话里口头改。
- 网查参考价与现有估计价偏差 > 40% 时不自动采用，记为待核对（价格卡里 `estimate_candidate`）。
- 模型验收看 LOOCV 的 RMSE（≤ base 8%；雾度 ≤ 1.0），不看训练集 R²；不达标先补数据。

## 4.2b 对话回答下面直接下载

助手每次给完方案，回答下面会出现一排下载按钮：
- **回答里提到的文件**（方案表 xlsx、采购清单 xlsx、询价清单 xlsx…）——自动从正文里识别路径并校验文件存在（`polysage/answer_doc.py`）；
- **导出本条回答 Word**——把这条回答（标题、表格、列表）转成 .docx，存在 `data/exports/`，点一下即可下载。

## 4.3 配方库的三个入口

配方库（数据 → 原料与价格 → 配方库）不再预置种子，配方只来自：研发流水线 ④/⑧、配方推荐页与对话推荐（自动入库）、手工录入；
相同组分不重复入库。每条配方带两栏采购信息，报价一更新就自动刷新：

- **原料采购（怎么来的）**：每种料的当前来源与价格，如 `LLC 9670（日报·贸易商B）；R1 6290（网查·东莞金满林）；AD 12500（估计价·待询价）`
- **全部可采购**：所有组分都有实价/日报/网查来源时为“是”

**配方只用买得到的料，买不到的配方不入库**：
- 可采购 = 有实价 / 贸易商日报 / 网查报价，或者是现配方正在用的料、标了“必配”的助剂（工厂本来就在买，只是还没登记报价）。
- `recommend_schemes` 默认 `only_buyable=True`：其余材料（只有内部估计价的新料，如 POE、VL、mLL 等）不参与出方案，notes 写明本次排除了哪些。
- `library.save_formulation` 默认校验，买不到就抛 `NotPurchasable` 不入库；流水线 ④/⑧、对话、手工录入都遵守；
  `library.purge_unbuyable()` 可以把来源失效的旧配方清出去。


配方库（数据 → 原料与价格 → 配方库）不再预置“内部技术路线 V2.0 表 4-3 首版”种子（已清理），配方只来自：
1. 研发流水线 ④ 候选配方 / ⑧ 扫描推荐（编号 P..，来源“流水线 ④ / ⑧”）；
2. 配方推荐页与对话推荐（`recommend_schemes` 出的方案自动入库，编号 A..，来源“对话推荐 日期”/“配方推荐页 日期”）；
   对话里用户自己报的配方 → `save_formulation`（编号 G..，来源“对话手工”）；
3. 配方库标签里手工录入（`LL 50 / R1 45 / AD 5`，来源“手工录入”）。
相同组分不会重复入库（按组分查重，复用已有编号）。

## 4.4 贸易商日报（数据 → 供应商与报价 → 贸易商日报）

把贸易商每天发来的报价（微信文字直接粘贴，或截图上传、离线 OCR）解析成一行一条：树脂类别 / 厂家 / 牌号 / 仓库地 / 货物状态 / 含税价（H）。
到厂价 = 含税价 + 运费表（`knowledge/04_价格卡/运费表.yaml`，界面可改；没写仓库地按“默认”）。
选用规则：LLDPE / LDPE / HDPE 各自牌号视为等价，**每类取当日最低到厂价写进价格卡实价**（LL 与 LLC 同价），推荐方案自动重排；
页面给每类前 3 名、与上次涨跌、价格趋势。对话里可直接粘贴报价：工具 `import_trader_quotes` / `daily_picks`。

## 4.4b 辅料与再生料寻源 + 采购方案

- **辅料/再生料网查**（供应商与报价 → 网查对照 → 辅料与再生料）：R1/RL/R2/PCR、AD（PPA+抗氧母粒）、FL/TFL 填充母料、POE/EVA/VL 等，
  按材料代码检索（每种料有专用检索词），抽“供应商 / 类型 / 地区 / 规格 / 报价 / 起订量 / 联系方式 / 日期”，折到厂价入库（channel=网查，近一个月）；
  可一键把最低到厂价写成**估计价**（不是实价）。对话工具 `find_material_suppliers`。
- **采购方案**（`polysage/procurement.py`）：配方 → 每种料买谁家、到厂价、联系方式、用量与小计、合计元/吨。
  价格来源优先级：实价 > 贸易商日报 > 网查 > 估计价（标“待询价”）。
  配方推荐页「结果」标签底部可选方案与批量、导出 `采购清单.xlsx`（含备选供应商页）；推荐运行时自动为第 1 名生成一份。
  对话工具 `procurement_plan`；助手协议要求每次给方案后必须给“导出的文件”和“怎么进货”两段。

## 4.5a 价格预判（数据 → 供应商与报价 → 价格预判）

`polysage/forecast.py`：大商所 L 线性主连日 K（新浪接口，L0 + 次两个活跃合约看期限结构）+ 我们的现货报价历史 + 近一周行情评述（Bing 一周内）→
模型给方向/幅度/依据（带来源链接），与期货隐含幅度按 6:4 混合，限 ±10%/月，存 `price_forecasts`。
页面卡片给每类 1 周 / 1 月预期，并把最近一次推荐的方案按预期价格重算成本（“变化% 越小越扛涨价”）。
配方推荐助手的系统提示会带上最新预判；工具 `price_outlook`。只是参考，不是交易建议。

## 4.5 网查对照（数据 → 供应商与报价 → 网查对照）

按日报同样的口径网查：检索目标取自最近日报里的 厂家+牌号（浙石化7042、镇海6098…），Bing 限最近一月，
抓页面 → 页面日期超过两周整页跳过 → 模型抽“厂家/牌号/仓库地/状态/含税价/报价方/日期”，报价没日期用页面日期，仍没有的丢弃。
入库 channel=网查、可信度 3，只在“网查对照”里看（每类前 5、到厂价），**不进价格卡**；价格卡只认贸易商日报。旧版泛寻源结果已清理。

## 4.6 旧版供应商寻源（保留代码）

给每种原料在网上找厂商 / 贸易商 / 回收厂的报价，与价格卡比较，生成询价清单；询价核实后一键登记为实价（推荐方案联动）。

- 检索：按牌号（如 `LLDPE 7042`）或名称生成 2～6 条检索词（再生料有专用模板），Bing 优先；抓页面正文，模型抽取多条报价
  （厂商、类型、地区、牌号、报价、口径、起订量、日期、联系方式、证据）；行情站（生意社/塑料在线）计为“行情”作基准。
- 结果：与现价（实价优先，否则估计价）算差价；不含税/不含运/期货/平台标价标“口径存疑”；可勾选“加入询价”“不可信”。
- 询价清单：导出 xlsx 给采购；回来填“实际报价”→“登记为实价”→ `pricing.record_price`，价格卡与推荐自动重排。
- 厂商库：累积档案，可手动录入现有供应商与报价；对话里可用 `find_suppliers` / `supplier_quotes`。
- 网查价只用于“该问谁、大致区间”，不当实价；1688 等平台页面部分需登录，联系方式以平台为准。

**搜索后端**：必应 → 搜狗 → 360，三家都是自带解析、直连、每家 12 秒超时，一条查询最多约 40 秒就返回；
ddgs 那套在国内服务器上基本不通，只有设 `WEBSEARCH_USE_DDGS=1` 时才兜底（否则一条失败的查询要卡 6～7 分钟）。

**出网策略**（`polysage/net.py`）：这台机器开着系统代理，默认国内站点直连、只有 `PROXY_HOSTS`（Google、DuckDuckGo、Brave、Tavily…）走代理；
`.env` 可设 `PROXY_MODE=auto|system|off`、`PROXY_HOSTS=域名,域名`。

## 5 检索源现状

| 源 | 状态 | 说明 |
|---|---|---|
| OpenAlex / Crossref / Semantic Scholar | 可用 | 英文文献题录 + 摘要 + OA 链接 |
| Elsevier Scopus | 可用（两枚 key 均通过测试） | 题录 + 摘要；全文需机构授权 |
| Elsevier ScienceDirect 检索 / 全文 | 需机构 IP 或 insttoken | 当前 401，已从默认源移除 |
| Google Patents | 可用 | 题录 + 权利要求 + 说明书全文 |
| Unpaywall | 可用 | 合法 OA PDF；部分出版社对程序下载返回 403 → 手动上传 |
| DuckDuckGo / Tavily | 可用 | 供应商 TDS、价格行情页 |
| CNKI / 万方 / 智慧芽 / incoPat | 无开放接口 | 在“知识库 → 手动上传”上传 PDF 或导出 txt（自动拆题录） |

## 6 目录

```
polysage/
  llm.py            SiliconFlow 客户端（对话 / 函数调用 / 向量 / 重排）
  db.py, kb.py      SQLite 持久层；知识库文件层（卡片 Markdown、task/constraints）
  sources/          各检索源与抓取
  ingest/           解析、切片、入库、卡片生成
  rag/              BM25 + 向量混合检索、带引用回答
  agents/           六个长期角色、工具集、函数调用循环、会话管理
  formulation/      原料库（含再生料批次）、约束、成本、候选生成、替代品筛选、定性评估、导出
  recommender.py    推荐智能体核心；recommend.py 命令行；pricing.py 价格联动与每周刷新
  ml/               数据表、模型、DOE、推荐优化
  pipeline/         ①～⑨ 编排与命令行
app_pages/          Streamlit 页面（智能体 / 流水线 / 对话 / 知识库 / 原料与配方 / 实验与模型 / 设置）
knowledge/          00_项目 … 08_复盘（人类可读镜像）
data/               polysage.db、下载与上传文件、流水线状态与日志
```

## 7 实验数据表

`knowledge/06_实验数据/数据表模板.csv`（内部技术路线附录 A）：一行一次测量；`sample_id` 以 `S0` 开头的为现用膜（base）；
17 个组分列合计 100；`R1_batch` 必填；工艺列首轮固定；性能列填单次测量值。
