# Circuit Design Agent 操作规约

## 角色与边界

- Codex：解析顶层需求、选择系统架构、分解 child 与指标预算、选择/修改 topology、运行测试/dry-run、分析结果并给出真实仿真命令。
- `topologies/`：程序化生成 Spectre DUT/testbench；不要手改 rendered `.cir/.scs`。
- `main.py`：给定 topology 下的 gm/Id、BO、Spectre、解析和结果保存。
- `system_decomposition.py`：系统架构、block graph、child 指标/预算与 `system_design.json`。
- `hierarchical_flow.py`：child-parent 依赖、资格调用、frozen artifact 与嵌入。
- `design_flow_graph.py`：单个 BO/Review 结果的 Design Audit、Review gate、PVT 和导出。
- `review_optimization.py`：生成 Review context、校验 patch plan、生成并验证 candidate。


运行项目命令前：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
```

## Spectre 仿真主机访问

运行真实 Spectre/BO/PVT 仿真前，先根据当前所在机器选择入口；不要假设本机已经安装 Cadence 或挂载 PDK。

- 从本地电脑运行：先执行 `ssh ic-vm` 进入本地虚拟机，再在虚拟机内进入项目目录、激活 `Auto_Agent_Design` 环境并运行仿真。
- 从机房服务器运行：先执行 `ssh chenhaonan@10.131.254.102`，登录后再进入项目目录、激活环境并运行仿真。
- 登录密码等敏感信息只记录在仓库根目录的 `LOCAL_SIMULATION_ACCESS.md`。该文件必须保持 Git ignored；若文件不存在或凭据失效，向用户确认，不要猜测。
- 密码仅在 SSH 的交互式提示中输入；不要把密码拼进 shell 命令、脚本、日志、测试输出或提交内容，也不要使用 `sshpass`/`expect` 自动注入密码。
- 登录后先用 `hostname`、`pwd` 确认目标机器和项目目录，再运行 `python pdk_profiles.py --validate --require-gmid --require-virtuoso --check-files`；验证通过后才开始真实仿真。

## 标准流程

1. 识别设计层级，将指标转换为 SI 单位，并区分硬约束与可选软目标。
2. 系统级需求通过 `system_decomposition.py` 生成架构、block graph、child targets/接口/预算；叶子模块可直接选择 topology。
3. 根据知识库、topology registry 和 PDK 约束选择 child topology。
4. 用 `write_project()` 生成网表、testbench、`requirements.json`；层级项目同时生成 `hierarchy.json`。
5. 叶子模块运行 `main.py`；层级项目运行 `hierarchical_flow.py`。
6. 读取 `results.json`：达标则执行 Design Audit，未达标则进入 `failure_repair`；Audit blocker 进入 `audit_repair`。
7. nominal 与 Design Audit 合格后运行 PVT；parent gap 必要时回传并重分配 child targets。
8. nominal/PVT 合格后用 `export_to_virtuoso.py` 导出。

`main.py` 不自动运行 Review/PVT。`design_flow_graph.py` 负责状态编排，不替代 BO，也不自动填写 `patch_plan.json`。

## 指标策略

- 每项指标通过 `MetricGoal` 声明硬约束：`min`、`max`、`range` 或 `target`；可附加 `minimize`、`maximize` 或 `target` 软目标。
- BO 采用 feasibility-first：先满足全部硬约束，再在可行解中优化软目标；功耗默认是上限约束并同时最小化。
- 旧版 `DesignTarget` 字段会自动映射为 `MetricGoal`；显式 `metric_goals` 优先。格式见 `Agent_LLM_BO/circuit_agent/METRIC_GOALS.md`。

## 系统与层级规则

- 固定决策顺序：`顶层指标 → 系统架构 → block graph → child targets/接口 → child topology → sizing/BO`。
- child targets 必须包含来源、裕量、PVT target、负载/摆幅/共模和电源域；不得直接复制顶层指标。
- parent BO 不展开 child W/L；child 与 parent 必须匹配 PDK profile、voltage domain、subckt 和端口。
- parent 失败时依次检查接口/testbench、预算假设、child PVT 裕量、child topology、系统架构。
- 自动拓扑升级默认关闭。
- 当前已接入 `bandgap_ptat`；ADC 架构、预算器和 topologies 尚未实现。
- 具体架构规则读取 `knowledge_base/System_knowledge_base/system_architecture_selection_guide.md`。

```bash
python hierarchical_flow.py --project <top_project>
python hierarchical_flow.py --project <top_project> --simulate
```

## PDK 规则

- PDK 路径、section、model、VDD、gm/Id 表、PVT、Spectre options、Virtuoso tech library 和 topology preset 统一由 `pdk_profiles.py` 管理。
- 如果需要切换工艺库，必须先在仓库根目录的 `PDK_Info_Json/` 中完善对应的工艺信息文件，文件名统一为 `<厂商>_<工艺节点名称>_Information.json`；该文件未完善并通过校验前，不得开始新工艺下的设计、仿真或物理实现。
- topology 中不得硬编码 PDK 路径、model、电源默认值或工艺专用初始 W/L。
- 晶体管类型使用 profile 的 `nmos_model/pmos_model` 或 LVT 等对应字段。
- `vdd` 是默认值，`vdd_min/vdd_max` 是允许范围；搜索 VDD 时必须显式加入参数空间。
- 工艺专用初值/范围优先写入 `topology_presets`。
- 分析结果前检查 `outputs/<project>/pdk_profile_used.json`。

### 切换 PDK 前置准备

更换 PDK 不是只替换 model path。以下项目全部完成前，不得在新工艺下运行 BO、
无源映射、PVT 或版图导出：

1. 在 `PDK_Info_Json/<厂商>_<工艺节点名称>_Information.json` 登记并核对 model
   bundle、section、voltage domain、VDD 范围、gm/Id 表、PVT corners、Spectre
   options、Virtuoso tech library 和 PDK library 路径；路径不得硬编码到 topology。
2. 为实际使用的每个 R/C PCell 配置 `passive_devices` 和 `passive_role_map`，至少包含
   Spectre model、OA library/cell/view、端口顺序、CDF 到 Spectre 的 `parameter_map`、
   合法几何范围/grid、固定参数、串并联上限、容差和 `evaluator_key`。不得复用其他
   PDK 的 PCell 参数、LUT、方阻或 callback key。
3. 适配新 PCell 的 CDF callback evaluator。电容应从 callback 读取派生电容值和
   callback 最终接受的尺寸；电阻优先采用同类 callback。若电阻暂用方阻公式，必须
   重新提取该 PDK/器件的 sheet resistance，不能沿用旧工艺数值。
4. 对每种 R/C 在小值、中值、大值和几何边界选取黄金点：电阻用 Spectre DC `V/I`，
   电容用 AC `imag(Y)/(2*pi*f)`，比较 CDF/公式与 Spectre。记录 corner、温度、偏置、
   频率和误差；所有点满足配置容差后，才能将器件标记为可自动映射。无需制作密集
   LUT，但不得跳过该资格验证。
5. 在真实 Cadence 主机运行 profile 校验和 OA 文件检查。缺少 PCell、callback、模型、
   gm/Id 表或资格证据时，流程必须报 `configure_pdk_passives`/配置错误并停止，禁止
   静默回退到旧 PDK、旧 LUT、简化电容公式或 `analogLib`。
6. 新项目首次运行后检查 `outputs/<project>/pdk_profile_used.json`，确保 BO、R/C
   映射、Spectre、PVT 和 Virtuoso 导出使用同一 profile/voltage domain。映射网表
   必须保存 callback 回写后的参数并重新通过 nominal/PVT；导出后继续做 DRC/LVS，
   关键设计做 post-layout 仿真。

```bash
python pdk_profiles.py --validate --require-gmid --require-virtuoso
# 真实 Cadence 机器可追加 --check-files
```

## 生成与优化

```bash
python -c "
from topologies import get_topology
from models import DesignTarget

topo = get_topology('5t_ota')
targets = DesignTarget(gain_db=40, bandwidth_hz=500e6,
                       phase_margin_deg=60, power_w=1e-3)
topo.write_project('5t_ota', targets=targets,
                   original_requirement='5T OTA example')
"
```

```bash
python main.py \
  --netlist <project>/<circuit>.cir \
  --testbench <project>/tb_<circuit>_ac.scs \
  --requirements <project>/requirements.json
```

- 运放必须传 AC testbench；仅在指标包含 SR/ST 时追加对应 testbench。
- Bandgap 使用 startup、PSRR、temperature/nonlinearity、line-regulation 专用 testbench，不套用运放 AC/SR/ST 链路。
- 常用参数：`--max-iter`、`--dry-run`、`--verbose`、`--project`、`--gain`、`--gbw`、`--pm`、`--power`、`--load-cap`、`--sr`、`--settling-time`。
- 无软目标时，BO early-stop 要求全部硬约束达标、仿真收敛且 critical MOS 不在线性区。
- 存在软目标时，即使已可行也继续到 `max_iter`，并按软目标在可行解中排序。

## 结果与 Review

- 先读 `outputs/<project>/results.json`：`all_targets_met`、`target_status`、`gap`、`metrics`、`params`、`operating_point_status`。
- `agent_context.md` 按路线索引 topology 知识、`parameter_effects.md`、`knowledge_analysis.md`、`optimization_log.json` 和必要 diagnostics。
- `optimization_metrics.csv` 仅供人查看；`sim.log/raw` 仅在收敛或解析异常时读取。
- `AGENT_REVIEW.md` 是人类说明，不作为 Agent evidence。

Review 路线：

- `audit_repair`：BO 已达标但 Design Audit 有 blocker；针对 blocker 检查 critical OP、尺寸/倍乘数、支路电流和参数边界。
- `failure_repair`：检查主导 gap、DC OP、topology 知识、理论与参数影响；决定 `modify`、`restart_bo` 或 `change_topology`。
- Review 必须使用 topology/domain profile：运放关注 Gain/GBW/PM/SR/settling，Bandgap 关注 startup/Vref/tempco/非线性/PSRR/线性调整率/功耗；禁止共用同一套参数建议。
- Review 直接读取当前 topology 知识库和 `metric_goals`，不把通用说明文档当作电路证据。

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --prepare-agent-review
```

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --patch-plan outputs/<project>/agent_review/patch_plan.json \
  --simulate
```

- Agent 只能对已有参数使用 `scale/set`；Python 负责校验和 clamp。
- `decision` 当前不是执行器硬分支；`restart_bo/change_topology` 不会自动执行。
- Design Audit blocker 阻止 PVT；warning 当前只记录。
- `design_flow_graph.py` 只在 BO 未达标或 Audit blocker 时提示 Review；Audit 无 blocker 的成功结果直接进入 PVT。
- Review candidate 进入 PVT 前必须检查 diagnostics。

## PVT 与导出

```bash
python pvt_simulation.py --results outputs/<project>/results.json --simulate
```

```bash
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib <tech_lib>
```

- 默认 PVT：`tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)`。
- PVT 失败先读 `pvt_report.md` 和失败 corner diagnostics。
- 导出器优先选择达标 Review candidate，否则选择 BO best。
- 仅在用户明确要求时使用 `--run-virtuoso`。

## 修改与验证

- 修复根因，保持改动最小；不要覆盖用户已有改动或修复无关问题。
- topology 管结构/参数空间；parser/simulator 管测量；不要在 `main.py` 增加 topology 专用硬编码。
- 修改后先跑局部测试，再运行：

```bash
python -m unittest discover -s tests
```

## 文档入口

- 完整项目流程：`FILE_FLOW.md`
- 系统架构：`knowledge_base/System_knowledge_base/system_architecture_selection_guide.md`
- 运放 topology：`knowledge_base/Opamp_knowledge_base/topology_selection_guide.md`
- topology Review：`knowledge_base/Opamp_knowledge_base/topologies/*_optimization.md`
- Bandgap：`knowledge_base/Bandgap_knowledge_base/topologies/bandgap_ptat_optimization.md`
- 结构化关系：`knowledge_base/circuit_design_relations.json`
- PDK：`knowledge_base/PDKs_info/pdk_profiles.md`、`Agent_LLM_BO/circuit_agent/pdk_profiles.py`
- 层级优化：`Agent_LLM_BO/circuit_agent/HIERARCHICAL_OPTIMIZATION.md`
- 系统分解：`Agent_LLM_BO/circuit_agent/SYSTEM_DECOMPOSITION.md`
- 指标策略：`Agent_LLM_BO/circuit_agent/METRIC_GOALS.md`
- Review：`Agent_LLM_BO/circuit_agent/AGENT_REVIEW.md`
- gm/Id：`Agent_LLM_BO/circuit_agent/SIZING_MODES.md`
