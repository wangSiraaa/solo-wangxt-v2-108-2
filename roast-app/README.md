# 烘焙批次曲线对比系统

面向烘焙负责人的**过程记录**工具：并排查看豆温、环境温度与操作事件（回温点、一爆、风门变化、出锅），
而不是用成品评分替代过程。系统**不连接真实烘焙机**，数据来自带噪声、不均采样与探针失联的合成生成器。

## 烘焙方案版本（roast plan versions）

在批次/事件/双批次对比之上新增一套**可复审、不可变**的目标曲线方案：

- **方案定义**：目标段完全相对已定义锚点定位——`charge → turning_point →
  first_crack_start → drop`，对应脱水/梅纳/发展三段；每段有目标时长 ±容差，
  可选段末温度 ±容差与段内曲线点 ±容差。定义经规范化 JSON + SHA-256 内容哈希。
- **状态机（严格线性）**：`draft 草稿 → confirmed 已确认 → retired 退役`。
  已确认/退役版本的定义**只不可变**：没有任何接口改写其内容；调整目标只能
  “基于某版本创建新版本”，确认新版本自动退役旧版本。
- **批次绑定**：只能绑定**已确认**版本；绑定是只追加行（改绑新版本插入新行、
  旧行置 superseded 并保留）。`series/export` 内嵌的是**版本快照**，因此旧批次
  与旧导出永远指向当时的判断依据。
- **偏差结论持久化（append-only）**：`plan_assessments` 保存总体判定、逐段
  pass/fail/unevaluable、锚点依据、取证口径与内容哈希。重算生成新行并将旧行
  置 `superseded`；**手工修正锚点事件**（charge/TP/FC/drop）后当前结论被标为
  `needs_review`（不覆盖），历史结论与依据仍可在历史接口/界面回看。
- **双编辑端冲突显式化**：同一旧版本被两个编辑端派生时，后到提交得到
  `409 version_conflict` 且不写入；同一草稿的并发保存用 `expected_revision`
  做原子乐观锁（条件 UPDATE + 唯一约束），后到者得到 `409 draft_revision_conflict`。
- **诚实判定**：缺少边界锚点 → 该段 `unevaluable`（含 `missing_anchor` 原因）；
  目标段跨越超过桥接上限的长缺测 → 该段 `unevaluable`（`crosses_wide_unfilled_gap`）；
  温度检查时刻 ±8s 内只有插值点（非实测）→ 该检查 `unevaluable`
  （`only_interpolated_in_window`）。**相邻插值永不计为通过依据，缺测永不补造
  达标结论。** 所有结论附声明：过程偏差不构成因果或成品质量结论。

页面“烘焙方案版本”面板可建草稿/确认/退役/绑定，并内置两个独立编辑端演示
验收④；“方案偏差审阅”面板按锚点对齐逐段展示偏差与容差，图表叠加锚点对齐
目标曲线、容差带与按判定着色的段背景；导出面板用导出内快照经 `/api/recompute`
逐段复现同一判断。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Svelte 4 + Vite + ECharts 5 |
| API | FastAPI（Pydantic 校验） |
| 计算 | NumPy：温升率、插值、阶段指标，全部为纯函数 |
| 存储 | PostgreSQL（原始采样、下豆点、人工标记），SQLAlchemy ORM |
| 测试 | pytest，同一套用例在 SQLite 与 PostgreSQL 上运行 |

## 数据与口径（重要）

### 温升率 RoR —— 窗口必须说明
采样间隔不均（1–5 s 抖动），因此不用相邻点差分。在每个**实测**时刻 t，取居中时间窗
`[t−W/2, t+W/2]`（默认 W=30 s）内的实测豆温点做普通最小二乘直线拟合，取斜率换算 °C/min。
- 至少 4 个实测点、时间跨度 ≥10 s 才给出 RoR，否则为 null（不编造）；
- **插值点不参与拟合**；探针失联的宽缺口处 RoR 直接断档；
- 序列边缘窗口被截断，返回值带 `ror_edge=true` 标记；
- 前端另有一个“显示平滑”参数（居中均值），只作用于展示曲线，窗口本身随接口参数和图表标题一起返回。

### 缺测与插值 —— 插值段不冒充实测
- `samples` 表**只存实测**：探针失联时豆温为 NULL，绝不回写；
- 查询时对 ≤`max_gap_fill_s`（默认 45 s）的内缺口做**相邻实测点线性插值**，
  逐点带 `is_interpolated=true`，图上为**虚线+空心菱形**，图例单列“插值段（非实测）”；
- 超过桥接上限的缺口与端点缺测**不填充**，曲线断档；缺测段在“缺测与插值审计”表逐条列出（通道、时长、处理方式）。

### 事件 —— 人工修正并保留来源
- 事件为只追加（append-only）。人工提交同类型事件时，旧行置 `superseded=true` 并记录
  `superseded_by_id`，不删除；自动建议记 `source=auto`，人工记 `source=manual`+`created_by`；
- 风门变化允许多条并存（离散操作点），金色虚线标出。

### 发展时间比 —— 明确区间
| 指标 | 区间 |
|---|---|
| 脱水期 drying | 下豆 charge → 回温点 turning_point |
| 梅纳期 maillard | 回温点 → 一爆开始 first_crack_start |
| 发展期 development | 一爆开始 → 出锅 drop |
| 一爆持续 | 一爆开始 → 一爆结束 |
| 总时长 total | 下豆 → 出锅 |
| **发展时间比 DTR** | development / total |

边界事件缺失时指标为 `null`（不猜测），并返回每个锚点的来源以便审计。

### 双批次对比 —— 不宣称因果
两批次按开火/下豆时刻对齐叠加；风门变化前后的形态变化仅供观察，接口和界面都附带声明：
无对照、无重复、无统计检验，**不构成因果结论**。

## 快速开始

### 方式一：本地

    # 终端 1 —— API（需要先有 PostgreSQL，或用 SQLite 做本地演示）
    cd backend
    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    # 默认连接 postgresql+psycopg2://roast:roast@localhost:5432/roast
    # 仅本地无 PG 时：export DATABASE_URL="sqlite:///./dev.db"
    uvicorn app.main:app --reload --port 8000

    # 终端 2 —— 前端
    cd frontend
    npm install
    npm run dev        # http://localhost:5173 （/api 已代理到 8000）

打开页面后点 **① 生成两个合成批次**：A 批在 300 s 有关一次风门（70%→40%），B 批无风门变化，
两批均含测量噪声、不均采样、一次短失联（5 s，插值桥接）和一次长失联（56 s，断档不桥接）。

再点 **③ 生成并绑定演示烘焙方案（已确认版本）**：自动创建一个已确认方案
`DEMO-PLAN-2026-09` 并把两个演示批次绑定到它、生成初始偏差结论。梅纳段
（约 54–480 s）刻意跨越 ~424–480 s 的长缺测，因此该段显示**未评估**而非通过。

### 手工验证五项验收

1. **绑定即逐段偏差 + 导出复现**：③ 后，曲线按锚点叠加白色虚线目标与容差带，
   “方案偏差审阅”表逐段给出实际/目标/偏差/容差/状态；导出面板逐段复现同一判定。
2. **新版本只向前生效**：在“两个编辑端”卡片（或时间线）基于已确认版本改目标
   → 得到新草稿 → 确认；把批次 B 绑到新版本，批次 A 的图表、结论与已下载的
   旧导出仍是 v1（`GET /api/batches/{a}/series` 的 `plan.version_snapshot`）。
3. **一爆缺失/长断档未评估**：删除/不录入 `first_crack_start` 后重算，梅纳/发展
   两段显示未评估及原因；梅纳跨长缺测段同样未评估，相邻插值不作为通过依据。
4. **双端版本冲突**：编辑端 A 与编辑端 B 都基于同一旧版本提交不同改动，B 得到
   显式 409 且其内容不落库；点“同步到最新基础版本”后才能合并重提。
5. **锚点修正 → 需要重新审阅**：在“人工修正事件”中改回温点/一爆/drop 时间，
   当前方案判断被标 `needs_review`（页面顶部横幅），历史判断与依据可在
   “查看历史判断/绑定”中回看；点重算后旧行置 superseded、新行为 current。

### 方式二：docker compose

    docker compose up --build
    # web: http://localhost:5173  api: http://localhost:8000/docs

## 验证（对应需求中的验收项）

    pytest                       # SQLite
    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest

界面“缺测与插值审计 · 导出可复现”面板一键完成：
1. 导出 JSON（原始采样 + 全量事件含已取代行 + 参数 + 阶段指标
   **+ 已绑定方案的不可变版本快照与全部偏差结论**）；
2. 调 `/api/recompute` 从原始数据独立重算，逐指标比对（脱水/梅纳/发展/一爆/总时长/DTR），
   并在携带 `plan_definition` 时**逐段复现同一方案判定（含未评估原因）**；
3. 再用翻倍窗口、不同平滑重取曲线，逐点比对原始豆温/环温**完全不变**。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/batches` | 批次列表 |
| POST | `/api/seed` | 生成两个合成批次 |
| GET | `/api/batches/{id}/series?...` | 曲线+RoR+指标（绑定后含 `plan` 快照+结论） |
| GET/POST | `/api/batches/{id}/events[?include_history=true]` | 事件列表/人工修正（只追加；锚点修正返回 `plan_review` 标记） |
| GET | `/api/compare?a=&b=` | 双批次叠加（含非因果声明） |
| GET | `/api/batches/{id}/export` | 自包含导出（v2，含 `plan_export` 快照与全部结论） |
| POST | `/api/recompute` | 从导出载荷独立重算指标（+可选方案逐段判定） |
| POST/GET | `/api/plans` | 建方案(含 v1 草稿) / 方案与版本链列表 |
| GET | `/api/plans/{id}` `/api/plans/{id}/versions/{vid}` | 方案 / 单个版本（只读） |
| POST | `/api/plans/{id}/versions` | 基于已确认/退役版本派生**新草稿**（409 版本冲突） |
| PATCH | `/api/plans/{id}/versions/{vid}` | 编辑开放草稿（`expected_revision` 乐观锁，409 草稿冲突） |
| POST | `/api/plans/{id}/versions/{vid}/confirm` `/retire` | 草稿→已确认（旧确认版自动退役）/ 已确认→退役 |
| PUT/GET | `/api/batches/{id}/plan-binding` | 绑定（仅已确认版本，改绑保留旧行）/ 当前绑定 |
| GET | `/api/batches/{id}/plan-binding/history` | 绑定历史（含已取代行） |
| POST/GET | `/api/batches/{id}/plan-assessment[s]` | 重算并持久化 / 当前结论（current 或 needs_review） |
| GET | `/api/batches/{id}/plan-assessment/history` | 全部历史结论（含 superseded，依据可回看） |
| POST | `/api/seed-demo-plan` | 演示：创建并确认 DEMO 方案、绑定两个演示批次 |

## 目录

    backend/app/  config.py models.py analysis.py plans.py plan_api.py
                  synth.py schemas.py main.py
    frontend/src/ App.svelte lib/RoastChart.svelte lib/PlanVersionPanel.svelte
                  lib/PlanDeviationPanel.svelte lib/api.js
    tests/        test_analysis.py test_api.py test_plan_versions.py
                  （双后端同一套用例）

纯计算与判定口径在 `app/plans.py`（方案规范化/哈希/锚点对齐评估）与
`app/analysis.py`（RoR/插值/阶段指标），均为无副作用纯函数；状态机与
持久化在 `app/plan_api.py`。
