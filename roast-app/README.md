# 烘焙批次曲线对比系统

面向烘焙负责人的**过程记录**工具：并排查看豆温、环境温度与操作事件（回温点、一爆、风门变化、出锅），
而不是用成品评分替代过程。系统**不连接真实烘焙机**，数据来自带噪声、不均采样与探针失联的合成生成器。

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

### 烘焙方案版本 —— 不可变版本、绑定快照、只追加的偏差结论
实际曲线不得与“会被后来修改的模板”混在一起，因此方案与批次之间有三层持久记录：

| 表 | 含义 |
|---|---|
| `roast_plans` / `roast_plan_versions` | 方案与其**不可变**版本；内容为相对锚点（charge、turning_point、first_crack_start、first_crack_end、drop）定位的目标段（控制点按段内比例 `p∈[0,1]` + 目标温度 + ±容差，可附目标时长） |
| `plan_bindings` | 批次绑定到**一个已确认版本**（按版本行 id 引用）；重新绑定只追加新行并让旧行 `superseded` |
| `plan_evaluations` | 绑定时/重审时生成的偏差结论，**只追加**：`current → needs_review → obsolete`，结果、依据、锚点指纹一并保存 |

- 版本状态：`draft → confirmed → retired`。只有 HEAD 草稿可以确认，确认新版本会自动退役旧确认版本；
  **只有已确认版本可绑定**；退役只改状态，内容永不改写（旧批次/旧导出继续引用旧版本）。
- 乐观并发：提交新版本必须带 `base_version_no`；两个编辑端从同一旧版本提交时，后到者收到 **409 版本冲突**，
  先到版本不会被覆盖，后到者需基于最新 HEAD 重新提交。
- 偏差判定口径（纯函数 `app/plan_eval.py`，导出后可用 `/api/recompute` 独立复算）：
  - 段按当前锚点事件的**实现时刻**对齐，逐时间格比较实测豆温与目标线/容差带；
  - **只有实测豆温点可作达标依据**；线性插值覆盖的时间格标 `interpolated`、未桥接长缺测覆盖的格标 `long_gap`，
    均不计为通过；
  - 缺少边界锚点、段内跨越超过 `long_gap_threshold_s`（默认 45 s）的探针断档、实测覆盖不足或插值占比过高时，
    该段为 `unevaluated` 并给出原因码（`missing_anchor` / `crosses_long_gap` /
    `insufficient_measured_coverage` / `too_much_interpolation`），**绝不补造达标结论**；
  - 每条结论固定附带声明：仅描述观察性偏差，**不构成因果或成品质量结论**。
- 人工修正锚点事件后，当前绑定的存储结论被标 `needs_review`（旧行保留全部依据可回看）；
  `POST /api/batches/{id}/plan-review` 追加一条新结论并把旧结论置 `obsolete`。
  非锚点事件（如风门变化）不影响方案判断。
- 导出 JSON 在批次已绑定时携带 `plan_snapshot`（版本号、状态、完整 content、sha256）与 `plan_evaluation`，
  `/api/recompute` 收到快照后从原始采样+事件重算同一判断；方案日后出了新版本，旧导出仍复现旧版本结论。


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
再点 **③ 载入内置目标曲线模板（草稿）**，在方案面板确认 v1 后把批次绑定到该版本：
按锚点对齐的容差带叠加到曲线上，梅纳期因跨越 56 s 长断档显示**未评估**（不把两侧插值当通过依据）。

### 方式二：docker compose

    docker compose up --build
    # web: http://localhost:5173  api: http://localhost:8000/docs

## 验证（对应需求中的验收项）

    pytest                       # SQLite
    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest

界面“缺测与插值审计 · 导出可复现”面板一键完成：
1. 导出 JSON（原始采样 + 全量事件含已取代行 + 参数 + 阶段指标；已绑定方案时另含**方案快照与偏差结论**）；
2. 调 `/api/recompute` 从原始数据独立重算，逐指标比对（脱水/梅纳/发展/一爆/总时长/DTR，
   以及方案每段偏差/未评估原因）；
3. 再用翻倍窗口、不同平滑重取曲线，逐点比对原始豆温/环温**完全不变**。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/batches` | 批次列表 |
| POST | `/api/batches` | 手工录入批次（原始采样，NULL 保持 NULL + 带来源事件） |
| POST | `/api/seed` | 生成两个合成批次 |
| GET | `/api/batches/{id}/series?window_s&display_smooth_s&max_gap_fill_s` | 曲线+RoR+指标+当前方案绑定/结论 |
| GET/POST | `/api/batches/{id}/events[?include_history=true]` | 事件列表/人工修正（只追加；锚点修正会标 needs_review） |
| GET | `/api/compare?a=&b=` | 双批次叠加（含非因果声明） |
| GET | `/api/batches/{id}/export` | 自包含导出（绑定后含方案快照与偏差结论） |
| POST | `/api/recompute` | 从导出载荷独立重算全部派生指标与方案判断 |
| GET/POST | `/api/plans` | 方案列表 / 建方案（含 v1 草稿） |
| POST | `/api/plans/seed-default` | 载入内置模板（草稿，幂等） |
| POST | `/api/plans/{id}/versions` | 提交新版本（必须带 `base_version_no`，冲突返回 409） |
| POST | `/api/plans/{id}/versions/{n}/confirm` · `/retire` | 草稿确认（自动退役旧版）/ 已确认版退役 |
| POST | `/api/batches/{id}/bind-plan` | 绑定已确认版本并生成偏差结论（重绑只追加） |
| GET | `/api/batches/{id}/bindings` | 绑定与历史判断（含已作废行及其依据） |
| POST | `/api/batches/{id}/plan-review` | 修正后重新审阅（旧结论置 obsolete，追加新结论） |

## 目录

    backend/app/  config.py models.py analysis.py plan_eval.py default_plan.py
                  synth.py schemas.py main.py
    frontend/src/ App.svelte lib/RoastChart.svelte lib/PlanPanel.svelte
                  lib/PlanReview.svelte lib/api.js
    tests/        test_analysis.py test_api.py test_plans.py（双后端同一套用例）
