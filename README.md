# heatbalance-agent

Agent-ready thermal power plant heat balance models (TESPy) with automatic
conservation and plausibility checks.

面向 AI agent 的电厂热平衡模型（TESPy），带自动守恒与合理性校核。

[English](#english) | [中文](#中文)

---

## English

### Goal

Let an AI agent build, modify and solve power plant heat balance models
reliably. The agent does not write equations from scratch: it derives new
models from a **validated reference plant**, and an **automatic check layer**
reports conservation and plausibility problems back to it.

### Current stage: 1 of N - the reference plant

This stage only lays the foundation:

- `plants/sub300/` - design-point (THA) model of a typical 300 MW
  subcritical, single-reheat unit with eight extractions (3 HP heaters,
  deaerator, 4 LP heaters) and a turbine-driven feed pump, plus a
  validation case of the same model code against published design data
- `checks/balance.py` - mass and energy residuals of the system and of
  every component
- `checks/ranges.py` - engineering plausibility ranges that return
  structured issues (problem, location, value, expected range, hint)

Not in this stage: off-design operation, an MCP server, further plants, a UI.

### Run

```bash
pip install -e ".[dev]"
```

```bash
python -m plants.sub300.model
```

```bash
pytest
```

```python
from plants.sub300 import model
from checks.balance import check_balance
from checks.ranges import check_ranges

params = model.load_params()            # plain dict from design_data.yaml
network = model.solve(model.build_model(params))
summary = model.summarize(network)      # JSON-serialisable results

check_balance(network)["issues"]        # [] when mass and energy are conserved
check_ranges(summary)                   # list of Issue records
```

Requires Python 3.11+. TESPy is pinned to 0.12.0.post1 and CoolProp to 8.0.0
because TESPy's API changes between minor releases.

### Design-point results

| Quantity | Value |
|---|---|
| Gross power (generator terminals) | 300.0 MW (specified) |
| Gross turbine heat rate | 7948.7 kJ/kWh |
| Main steam | 16.7 MPa / 538 degC, 908.7 t/h |
| Reheat steam | 3.249 MPa / 538 degC, 747.5 t/h |
| Final feedwater temperature | 274.8 degC |
| Cylinder efficiencies HP / IP / LP | 88.6 % / 91.4 % / 88.2 % |
| Valve stem / HP-IP gland leakage | 3.8 t/h to H3 / 15.8 t/h to the IP inlet |
| Condenser | 4.9 kPa, 355.8 MW |
| Feed pump turbine | 34.2 t/h steam, 6.72 MW |

Calibration: power and heat rate agree with the reference to better than
1e-8 (limit 0.5 %). **The reference is an independent classical heat
balance on the same typical parameters** (`plants/sub300/handcalc.py`,
CoolProp only), not published design data. The agreement shows that the
TESPy network implements the intended topology; it does not show agreement
with a real unit.

The range check reports no errors and one explained warning: section HP2
has an isentropic efficiency of 0.970 because the first extraction point of
the data set splits the HP expansion unevenly (HP1 0.835); the HP cylinder
as a whole is plausible.

### Validation against published design data

The same model code is run on the published 100 % THA design data of a
different 300 MW subcritical unit with seven heaters (Yang et al., Energies
2023, 16(9), 3727; `plants/sub300/validation_en16093727.yaml`,
`plants/sub300/test_validation.py`).

| Quantity | Published | Model | Deviation |
|---|---|---|---|
| Gross power at 260.38 kg/s main steam | 300.234 MW | 300.23 MW | 0.00 % (calibrated) |
| Heat rate (implied by Table 1 of the paper) | 8192.6 kJ/kWh | 8190.8 kJ/kWh | -0.02 % |
| Reheat steam flow | 217.66 kg/s | 217.35 kg/s | -0.14 % |
| Extraction RH1 / RH2 | 17.08 / 20.01 kg/s | 17.25 / 20.16 kg/s | +1.0 % / +0.7 % |
| Extraction RH3 | 8.44 kg/s | 8.76 kg/s | +3.8 % |
| Extraction DEA / RH5 / RH6 | 7.48 / 11.01 / 10.51 kg/s | 7.46 / 11.03 / 10.47 kg/s | -0.3 % / +0.1 % / -0.4 % |
| Extraction RH7 | 11.64 kg/s | 11.64 kg/s | 0 % (calibrated) |

How to read this:

- The paper does not give condenser pressure, exhaust state or heat rate.
  Two parameters were fitted to one published value each: condenser
  pressure (14.55 kPa, to the RH7 flow) and last-section efficiency (0.865,
  to power). The heat rate reference is derived from the published main
  steam, reheat and feedwater data.
- Two leakage flows follow from the published mass balances without
  fitting: 5.63 kg/s bypass the reheater, of which 1.09 kg/s enter the HP
  drain cascade. Their routing (RH3 shell and IP inlet, at main steam
  enthalpy) is an assumption.
- With power fitted and the reheat flow fixed by the leakage total, heat
  rate and reheat flow agree almost by construction. They are not
  independent evidence.
- The inferred back pressure and the seven-heater layout suggest a direct
  air-cooled unit with motor-driven feed pump. This is an inference; the
  paper does not say so.
- The genuine predictions are six extraction flows: five agree within 1 %,
  RH3 within 4 %.
- Without leakages (and the last-section efficiency refitted) the heat rate
  is 0.38 % high and RH3 is missed by 17 %; a test keeps this comparison.

### Data sources and disclaimer

**sub300 is a typical reference unit. It does not represent any real power
plant and must not be used as design or guarantee data.**

- Only public sources are used; nothing comes from plant-internal or
  confidential documents.
- No complete, verifiable public THA heat balance was found. The extraction
  pressures and enthalpies come from a secondhand source (a course-design
  document quoting an N300-16.7/538/538 table) that could only be read
  through a search-engine excerpt; they are treated as typical values.
- Every number in `plants/sub300/design_data.yaml` points to a public source
  (`references`, S1-S6) or to an explicit engineering assumption
  (`assumptions`, A1-A16). A test enforces this, also for the validation
  case.
- Several assumptions are backed by the published unit of the validation
  case: reheat pressure loss 10 %, HP extraction line loss 3 %, terminal
  differences 0 / 2.8 K and 5.6 K, and the leakage fractions.

Main assumptions (full list with rationale in `design_data.yaml`):

| | Assumption | Effect on heat rate |
|---|---|---|
| A3 | last LP section efficiency 0.75 including exhaust loss | 0.11 % per 0.01 |
| A13 | condenser pressure 4.9 kPa (the source table's own value is unknown) | +0.45 % at 5.39 kPa |
| A10 | mechanical x generator efficiency 0.990 x 0.987 | 1 : 1 |
| A5 | heater terminal differences -1.7 / 0 / 0 K (HP), 2.8 K (LP) | about 0.1 % for 2 K on all LP heaters |
| A6 | drain cooler approach 5.6 K | 0.06 % at 10 K |
| A4 | extraction line pressure loss 3 % (HP), 5 % (others) | 0.12 % at 5 % / 8 % |
| A8 | one turbine-driven feed pump, 20.5 MPa, no booster pump | 0.08 % per MPa |
| A17 | valve stem leakage 0.42 % to H3, HP-IP gland leakage 1.74 % to the IP inlet (fractions from the validation case, routing assumed) | +0.51 % against no leakage |
| A11 | gland steam condenser, LP gland sealing steam, blowdown, make-up, spray neglected | not quantified |

Known limitations:

- With the design data the expansion line is fixed by the extraction
  enthalpies, so section efficiencies are results. For what-if studies use
  `model.with_fixed_efficiencies(params, summary)` first.
- Heaters are lumped (one energy balance with upper and lower terminal
  difference); desuperheating, condensing and drain cooling zones are not
  resolved.
- `feed_pump.drive` is `turbine` or `motor`; a booster pump is not
  modelled.
- Leakages are lumped into two flows at main steam enthalpy; real units
  have more leak-off points at lower enthalpy, and the routing used here is
  an assumption.

### License

MIT

---

## 中文

### 项目目标

让 AI agent 能可靠地搭建、修改、求解电厂热平衡模型。agent 不从零写方程，
而是从**已验证的参考机组**派生，由**自动校核层**把守恒和合理性问题反馈
给 agent。

### 当前阶段：第 1 阶段——参考机组

本阶段只做地基：

- `plants/sub300/`：典型亚临界 300 MW、一次再热、八级回热（3 高加 +
  除氧器 + 4 低加）、汽动给水泵机组的设计工况（THA）模型，外加一个用
  公开设计数据检验同一套模型代码的验证算例
- `checks/balance.py`：全系统及每个部件的质量、能量守恒残差
- `checks/ranges.py`：工程合理范围检查，返回结构化问题清单（问题、位置、
  实际值、期望范围、排查方向）

本阶段不做：变工况、MCP 服务器、多台机组、界面。

### 如何运行

```bash
pip install -e ".[dev]"
```

```bash
python -m plants.sub300.model
```

```bash
pytest
```

Python 接口见上方英文部分的示例：`load_params` → `build_model` → `solve`
→ `summarize`，再交给 `check_balance` 和 `check_ranges`。

需要 Python 3.11+。TESPy 锁定 0.12.0.post1，CoolProp 锁定 8.0.0（TESPy
各小版本之间接口变化较大）。

### 设计工况结果

| 项目 | 数值 |
|---|---|
| 发电机端功率 | 300.0 MW（给定） |
| 汽轮机热耗率 | 7948.7 kJ/kWh |
| 主蒸汽 | 16.7 MPa / 538 ℃，908.7 t/h |
| 再热蒸汽 | 3.249 MPa / 538 ℃，747.5 t/h |
| 给水温度 | 274.8 ℃ |
| 高 / 中 / 低压缸效率 | 88.6 % / 91.4 % / 88.2 % |
| 门杆漏汽 / 高中压缸间轴封漏汽 | 3.8 t/h 去 H3 / 15.8 t/h 去中压缸进口 |
| 凝汽器 | 4.9 kPa，355.8 MW |
| 给水泵汽轮机 | 耗汽 34.2 t/h，6.72 MW |

校准：功率、热耗率与参考值偏差小于 1e-8（限值 0.5 %）。**参考值是对同
一组典型参数做的独立常规热平衡手算**（`plants/sub300/handcalc.py`，只用
CoolProp），不是公开发表的设计数据。两者吻合说明 TESPy 模型的拓扑和设定
实现正确，不说明它与某台真实机组吻合。

合理范围检查无 error，有一条已解释的 warning：HP2 段等熵效率 0.970，原
因是数据中一段抽汽点把高压缸膨胀线分得不均（HP1 段 0.835），高压缸整体
效率正常。

### 用公开设计数据验证

同一套模型代码在另一台 300 MW 亚临界机组（7 台加热器）的公开 100 % THA
设计数据上运行（Yang 等，Energies 2023, 16(9), 3727；
`plants/sub300/validation_en16093727.yaml`、
`plants/sub300/test_validation.py`）。对比结果见上方英文表格，要点：

- 功率偏差 0.00 %（标定量），热耗率 8190.8 对 8192.6 kJ/kWh，偏差
  −0.02 %。
- 论文没有给出背压、排汽参数和热耗率。有两个参数各用一个公开数值标定：
  背压（14.55 kPa，对 RH7 抽汽量）和末级段效率（0.865，对功率）。热耗
  率参考值由论文的主汽、再热、给水数据算出。
- 两股漏汽量由论文的质量平衡直接得出，没有拟合：5.63 kg/s 不经过再热
  器，其中 1.09 kg/s 进入高加疏水系统。去向（RH3 汽侧和中压缸进口，按
  主汽焓）是假设。
- 功率是标定的，再热流量由漏汽总量确定，所以热耗率和再热流量的吻合几
  乎是必然结果，不算独立证据。
- 反推的背压和 7 级回热布置指向直接空冷、电动给水泵机组。这是推断，论
  文没有明说。
- 真正的预测值是 6 段抽汽量：5 段偏差在 1 % 以内，RH3 在 4 % 以内。
- 不建漏汽（并重新标定末级段效率）时，热耗率偏高 0.38 %，RH3 抽汽量偏
  差 17 %；有一个测试保留了这组对比。

### 数据来源与免责声明

**sub300 是典型参考机组，不对应任何一台真实电厂，不能当作设计值或保证值
使用。**

- 只用公开来源，不含任何电厂内部资料或保密数据。
- 没有找到完整、可核实的公开 THA 热平衡。抽汽压力和焓来自二手来源（引
  用 N300-16.7/538/538 参数表的课程设计文档），且只读到了搜索引擎摘要，
  因此按典型值处理。
- `plants/sub300/design_data.yaml` 里每个数值都指向公开来源（`references`，
  S1–S6）或明确的工程假设（`assumptions`，A1–A16），有测试强制检查，验
  证算例的数据文件同样检查。
- 验证算例的公开机组印证了几条假设：再热压损 10 %、高加抽汽管压损 3 %、
  端差 0 / 2.8 K 和 5.6 K，漏汽份额也取自它。

主要假设及其对热耗率的影响见上方英文表格，完整清单和理由在
`design_data.yaml`。

已知局限：

- 设计数据用抽汽焓固定了膨胀线，各段效率是计算结果。做参数变化分析前先
  调用 `model.with_fixed_efficiencies(params, summary)` 把效率固定下来。
- 加热器是集总模型（一个能量平衡加上、下端差），不分过热段、凝结段、疏
  水冷却段。
- `feed_pump.drive` 可选 `turbine`（汽动）或 `motor`（电动）；前置泵没
  有建。
- 漏汽合并成两股、按主汽焓处理（假设 A17：0.42 % 去 H3，1.74 % 去中压
  缸进口，使热耗率比不计漏汽高 0.51 %）。真实机组的漏汽点更多、焓更低，
  这里的去向是假设。

### 许可

MIT
