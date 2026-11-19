# kicad-agent — AI Agent 电路板设计

> 自然语言 → BOM → 引脚级连接 → 真实 KiCad 原理图
>
> **当前阶段：W1「脚手架跑通」** —— 核心技术路线已验证，尚未接入 LLM。

---

## 1. 这是什么

课程大作业的第一段流水线：把一句自然语言需求，变成 KiCad 能打开的原理图。

本仓库把作业基础任务的**五步闭环**都实现了：

```
自然语言 ──(02 提示词)──> BOM ──(03 提示词 + 真实引脚表)──> 连接关系 JSON
                                                              │
                                                     (04) validate + 生成原理图
                                                              │
                                                     (05) 追问 → 改 JSON → 重生成
```

每一步都有程序化校验挡着：结构校验（引脚是否存在 / 短路 / 漏接）、
网表连通性分区比对、ERC。模型输出不合规就把错误回灌给它重试。

---

## 2. 环境要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.10+ | 开发时用 3.13.7 |
| KiCad | **8.0+**（含 `kicad-cli`） | 开发时用 10.0.6；安装器默认会把 `kicad-cli` 加进 PATH |
| 操作系统 | Windows / macOS / Linux | 路径自动探测，见下 |

**不需要**手动改代码里的 KiCad 路径。`kicad_env.py` 的解析优先级：

1. 环境变量 `KICAD_ROOT`（显式指定，最高优先级）
2. PATH 上的 `kicad-cli`
3. 各平台常见安装目录（`Program Files` / `/Applications` / `/usr`）

装到非常规位置时才需要设置：

```powershell
# PowerShell
$env:KICAD_ROOT = 'D:\apps\KiCad\10.0'
```
```bash
# bash
export KICAD_ROOT=/opt/kicad
```

---

## 3. 快速开始（约 5 分钟）

```bash
git clone <本仓库地址>
cd kicad-agent
python -m venv .venv

# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
```

### 3.1 先自检环境

```powershell
.venv\Scripts\python.exe kicad_env.py
```

正常输出类似：

```
kicad-cli : D:\apps new\KiCad10.0\10.0\bin\kicad-cli.exe
版本      : 10.0.6
符号库    : D:\apps new\KiCad10.0\10.0\share\kicad\symbols
内置Python: D:\apps new\KiCad10.0\10.0\bin\python.exe
```

找不到 KiCad 时会直接告诉你该设哪个环境变量，不会抛一堆 traceback。

### 3.2 生成原理图并自动校验

```powershell
.venv\Scripts\python.exe spike_gen_sch.py
```

预期输出（末尾是关键）：

```
网表已生成: blinky.net（6390 字节，3 个网络）
  Net-(BT1-+)        BT1.1, R1.1
  Net-(BT1--)        BT1.2, D1.1
  Net-(D1-A)         D1.2, R1.2

连通性校验: PASS —— 3 个网络与预期完全一致
```

脚本不只是「生成个文件就完事」：它会调 `kicad-cli` 导出网表，把网表解析回来，
跟硬编码的期望连接逐条比对，**连错就返回非 0**。这样接进 CI 或 Agent 反馈闭环
都能直接当判据用。

产物在 `out/`：`blinky.kicad_sch`（KiCad 可直接打开）、`blinky.net`。

### 3.3 从设计 JSON 生成整块板子的原理图

```powershell
.venv\Scripts\python.exe generate.py designs/light_alarm.json
```

它会依次完成：读设计 JSON → 放置 48 个器件（自动选纸）→ 每引脚引 stub 并打网络标签
→ 加 `PWR_FLAG` 压掉电源告警 → 写出 `.kicad_sch` → 调 `kicad-cli` 导出网表
→ **把网表的分区和设计的分区逐组比对**。

预期输出：

```
原理图    : out\light_alarm.kicad_sch  (126664 字节)
图纸      : A3
器件      : 51
内嵌符号  : 14
导线 stub : 131
网络标签  : 131
no_connect: 25

设计网络数(≥2节点): 28
网表网络数(≥2节点): 28

连通性比对: PASS —— 网表分区与设计完全一致
```

这里的比对**只看连通性分区，不看网络名**：把设计里的每个网络和网表里的每个网络都表示成
「引脚集合的集合」，两者必须完全相等。这样能抓住「图画出来了但引脚没接上」这类错误 ——
KiCad 本身不会报错，只是那个脚悬空。

### 3.4 其他校验命令

```bash
$CLI sch erc        --format json -o out/blinky-erc.json out/blinky.kicad_sch
$CLI sch export bom               -o out/blinky-bom.csv  out/blinky.kicad_sch
```

### 3.5 走完五步闭环（agent.py）

```powershell
# 步骤 02 + 03 + 04：一句话需求 -> BOM -> 连接关系 -> 原理图
.venv\Scripts\python.exe agent.py design "帮我设计一块光闹钟：到预设时间后 LED 灯组亮起，亮度线性增长，第 5 分钟达到最亮并同时播放音乐闹铃"

# 步骤 05：追问修改
.venv\Scripts\python.exe agent.py revise designs/light_alarm.json "把 LED 限流电阻从 82Ω 换成 100Ω"

# 只做结构校验
.venv\Scripts\python.exe agent.py check designs/light_alarm.json
```

`--provider` 三个取值：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | 有 `LLM_API_KEY` 就真调 API，没有自动退回离线 |
| `openai` | 强制调 API（任意 OpenAI 兼容端点） |
| `fixture` | 强制离线，用 `fixtures/*.json` 代替模型回答 |

**离线模式是刻意设计的**：没有 key 也能把整条流水线（解析 → schema 校验 →
生成 → 网表比对）跑通和回归；提示词或下游代码改动后，用它确认没被改坏。
接真 API 只换一个 `--provider`，代码不动。

配真 API：

```bash
cp .env.example .env    # 然后填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
```

---

## 4. 已验证的链路

`spike_gen_sch.py` 从零生成「电池 → 电阻 → LED」串联电路，全程不依赖任何手工画的工程文件：

```
BT1 (Battery_Cell, 3V) ── R1 (330Ω) ── D1 (LED)
```

| 检查项 | 结果 | 怎么验的 |
| --- | --- | --- |
| 生成 `.kicad_sch` | ✅ 10085 字节 | 文件可直接被 KiCad 打开 |
| `kicad-cli sch export netlist` | ✅ rc=0，3 个网络 | 脚本内置 |
| 网络连接正确性 | ✅ PASS | 脚本自动比对 3 组引脚 |
| `kicad-cli sch export bom` | ✅ rc=0 | BT1 / R1 / D1 均带 Value 与 Footprint |
| `kicad-cli sch erc` | ✅ 0 error / 13 warning | 详见下表 |

ERC 告警构成（实测）：

| 类型 | 条数 | 含义 |
| --- | --- | --- |
| `endpoint_off_grid` | 7 | 引脚坐标未对齐 1.27mm 网格 |
| `footprint_link_issues` | 3 | 符号未关联封装库 |
| `lib_symbol_issues` | 3 | 内嵌符号定义与库不完全一致 |

三者都不影响连通性，但属于后续该清掉的债。

---

## 5. 代码结构

```
kicad-agent/
├── agent.py            # 五步闭环 CLI（design / revise / check）
├── providers.py        # LLM 抽象：fixture 离线 / OpenAI 兼容
├── pipeline.py         # 提示词渲染 + 引脚表注入 + JSON 解析
├── schema.py           # pydantic 数据契约（BomStage / ConnectionStage / Design）
├── catalog.py          # 允许使用的符号白名单（防模型编造符号）
├── prompts/
│   ├── bom.md          # 步骤 02 提示词
│   ├── connections.md  # 步骤 03 提示词
│   └── revise.md       # 步骤 05 提示词
├── fixtures/           # 离线模式下代替模型回答
├── kicad_env.py        # KiCad 安装探测（不写死路径）
├── symbol_lib.py       # 符号库加载：UTF-8 读取 + 派生符号展平
├── sexpr.py            # 最小 S-表达式解析器（读网表用）
├── sch_writer.py       # 设计 JSON -> Schematic（旋转 / 网络标签 / 自动选纸）
├── generate.py         # 生成原理图 + 导出网表 + 连通性分区比对
├── validate_design.py  # 设计 JSON 校验器（引脚存在性 / 短路 / 漏接）
├── spike_gen_sch.py    # 最小闭环示例（电池-R-LED），用来快速验证环境
├── bisect_test.py      # 诊断脚本：逐层定位「KiCad 拒绝加载」的原因，非主流程
├── designs/
│   ├── light_alarm.json      # 光闹钟最终设计
│   └── light_alarm_spec.md   # 设计说明：架构、功率预算、亮度曲线
├── requirements.txt
├── .env.example        # LLM 配置模板
└── out/                # 生成物（已 gitignore）
```

---

## 6. 核心结论：为什么不用「KiCad 的 Python 脚本接口」

作业宣讲写的是「用 KiCad 的 Python 脚本接口把 JSON 变成真实原理图」。
**这句话不成立** —— KiCad 的官方 Python API 只覆盖 PCB 侧（`pcbnew`），
从来没有提供原理图（eeschema）的编辑接口：

```
> python -c "import eeschema"
ModuleNotFoundError: No module named 'eeschema'
```

所以技术路线改为：

| 环节 | 方案 |
| --- | --- |
| 读写 `.kicad_sch` | `kiutils`（直接操作 S-表达式文本） |
| 校验 / 导出 | `kicad-cli`：`sch export netlist` / `sch export bom` / `sch erc` |
| 符号几何 | 读 `share/kicad/symbols/*.kicad_sym`，取 `units[].pins` |
| PCB / DRC（进阶挑战） | `pcbnew` + `kicad-cli pcb drc`（这部分官方接口齐全） |

注意两个 Python 的分工：项目 `.venv` 负责生成文本、调 LLM；
KiCad 自带的 `bin/python.exe` 才**能** `import pcbnew`，PCB 相关必须用它。

---

## 7. 开发笔记（报告「失败案例分析」素材）

下面几条是踩过的坑，但**证据强度不一** —— 已按实际复现情况标注，
写进报告前请勿直接把「待复核」当结论用。

| # | 结论 | 证据强度 |
| --- | --- | --- |
| 1 | kiutils 输出的是 KiCad 6 语法（`hide`、`(id N)`），我们固定写 `version 20211014` | 已验证（现状） |
| 2 | ~~`version` 必须与语法一致，否则 KiCad 拒绝加载~~ | **已证伪**：实测 `20211014` / `20231120` / `20250114` 三个版本号，`kicad-cli` 都能正常加载导出。早期「版本号导致加载失败」的判断属于误判，真实原因未定位 |
| 3 | 元件实例的 `instances` 要用 `SymbolProjectInstance`（带 project 包裹 + 根 uuid 路径） | **待复核**：`bisect_test.py` 现在 5 种组合（有无 `id` / 有无 project 包裹 / 有无 sheet 实例）**全部 rc=0**，复现不出「塞 `SymbolInstance` 会被拒」的现象 |
| 4 | 引脚定义藏在 `Symbol.units[*].pins`，顶层 `Symbol.pins` 是空的 | 已验证 |
| 5 | 符号库 Y 轴向上为正、原理图画布 Y 轴向下为正，引脚坐标要取负（`schematic_y = inst_y - pin_y`） | 已验证（坐标算错整条电路就接反） |

写报告时的教训：**别拿「改完之后跑通了」当因果证明**。第 2、3 条就是典型 ——
当时同时改了好几处，跑通了就归因给最后一个改动，实际可能根本不是它。

---

## 8. 关于 KiCad 版本（重要）

- 本仓库固定输出 **KiCad 6 语法 + `version 20211014`**，KiCad 8 / 9 / 10 都能读，
  这是为了兼容「助教可能用 KiCad 8 验收」而做的保守选择。
- ⚠️ **一旦用 KiCad 10 的图形界面打开再保存**，文件会被自动升级成 10 的格式
  （`version 20250114` 等），届时**用 KiCad 8 就打不开了**。
- 所以：自动化流程产出的文件可以直接交；如果为了拍演示视频用 GUI 打开过，
  交之前请重新跑一遍脚本生成，别交 GUI 另存过的版本。
- 更稳的做法是问清助教的验收版本，或本机并行装一个 KiCad 8。

---

## 9. 进度与下一步

- [x] 01 环境搭建：跑通「生成 `.kicad_sch` → 导出网表 → 自动校验连通性」
- [x] 01 环境搭建：去掉硬编码路径，换机器可跑（`kicad_env.py`）
- [x] 02 自然语言 → BOM：提示词 `prompts/bom.md` + 符号白名单约束
- [x] 03 连接关系生成：提示词 `prompts/connections.md` + 注入真实引脚表
- [x] 04 JSON → 原理图：`sch_writer.py` + `generate.py`，连通性分区比对
- [x] 05 交互修正：提示词 `prompts/revise.md`，改值不动连接
- [x] 反馈闭环：输出不合规时把错误回灌重试（最多 2 次）
- [x] 离线回归：`--provider fixture`，无 key 也能验证全链路
- [ ] **接真 API 实测提示词**（需要 `LLM_API_KEY`）—— 提示词质量只能真跑才知道
- [ ] 多轮对话式修正（当前每次 revise 独立，不带上下文）
- [ ] ERC 闭环：读报告 → 自动改设计 → 重跑直到通过
- [ ] 原理图排版优化（当前按功能块排布，标签密集处偏挤）

---

## 10. AI 使用声明

按课程学术诚信要求，这里如实记录 AI 参与情况。

| 内容 | 来源 |
| --- | --- |
| 技术路线选型（kiutils + kicad-cli）与踩坑排查 | 人机协作，由 AI 编码助手协助诊断 |
| `kicad_env.py`、`sexpr.py` | AI 生成，人工审阅 |
| `spike_gen_sch.py` 的原理图生成主体逻辑 | 早期人工调试 + AI 协助 |
| 本 README | AI 起草，人工核对事实 |

关键设计决策（为什么用 kiutils 而非官方 API、为什么坐标要取负、
为什么固定输出 v6 语法）均可在答辩时口头解释。
