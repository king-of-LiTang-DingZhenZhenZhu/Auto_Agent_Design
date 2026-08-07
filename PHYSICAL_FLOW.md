# 单仓库前后端全流程

本仓库已经内置 `analogskills` 物理后端，不需要在服务器上另外安装或
检出 `analog_skills`。前端选择出的最终 Review/BO 网表是唯一电气真值。

## 服务器准备

```bash
conda activate Auto_Agent_Design
python -m pip install -r requirements/physical.txt
cp config/physical.example.env config/physical.env
```

根据服务器实际安装修改 `config/physical.env`，然后加载环境变量：

```bash
set -a
source config/physical.env
set +a
```

`--run-signoff` 启动前会检查 Virtuoso、Calibre、CRN28 PDK library、DRC
deck 和 LVS deck。任何缺失都会 fail-closed，不会生成伪造的 clean 状态。

推荐复用 analog-skills 原有的常驻 CIW/SKILL server。先从仓库根目录启动
一次 Virtuoso，并在 CIW 中执行：

```skill
load("/absolute/path/to/Auto_Agent_Design/analogskills/eda/skill_server.il")
```

然后把生成的 `skill_server_port.txt` 绝对路径配置到
`ANALOGSKILLS_SKILL_SERVER_PORT_FILE`。`ANALOGSKILLS_VIRTUOSO_EXECUTION=auto`
会优先复用该会话；设为 `skill_server` 时，server 不可用会直接失败而不会
反复启动 batch Virtuoso。Python 完成后只断开 ZMQ，不关闭 CIW。没有运行中
server 时，`auto` 使用合并后的 batch replay，并自动取消退出时的
`display.drf` 保存对话框。

OA schematic/layout 脚本具有内容指纹。相同输入重复执行时会复用已完成的
OA 写入，状态记录在 `physical/oa/oa_stage_state.json`；网表或版图变化后会
自动重新写入。

## 运行

只准备完整原理图或导入到常驻 CIW：

```bash
python run_full_flow.py \
  --project Agent_LLM_BO/circuit_agent/outputs/<project> \
  --prepare-schematic \
  --lib BO_Designs

python run_full_flow.py \
  --project Agent_LLM_BO/circuit_agent/outputs/<project> \
  --import-schematic \
  --lib BO_Designs
```

`--export-virtuoso` 已删除。上述两个入口和 `--run-signoff` 使用同一套
analogskills handoff、OA plan 和完整 SKILL writer。

从结构化用户需求执行完整流程：

```bash
python run_full_flow.py \
  --requirements config/two_stage_ota.example.json \
  --project-name my_ota \
  --max-iter 50 \
  --run-pvt --simulate \
  --run-signoff \
  --lib BO_Designs \
  --max-eco-iterations 5
```

结构化输入可用顶层 `pvt_targets`/`pvt_metric_goals` 声明独立于 nominal BO
指标的 PVT 验收预算；也可通过 `--pvt-requirements <json>` 单独提供，后者优先。

该命令依次执行 Auto 前端需求分析、topology 选择、网表/testbench 生成、
gm/Id/BO、Design Audit、必要的自动 Review、真实 PVT，以及物理后端。
自然语言入口为 `--request "..."`，并复用 Auto 前端的 DeepSeek LLM 配置。

从已有 BO 输出恢复，或只生成可检查的物理执行包：

```bash
python run_full_flow.py \
  --project Agent_LLM_BO/circuit_agent/outputs/<project> \
  --prepare-physical \
  --lib BO_Designs
```

只有真实 `pvt/pvt_results.json` 中 `pvt_pass=true` 才会进入物理流程。
支持的首版 topology 是当前前端的 `two_stage_ota` 和 11 管
`strongarm_latch`。其它结构会返回 `physical_adapter_required`。

全部版图、GDS、DRC/LVS、ECO checkpoint、manifest 和最终报告位于
`Agent_LLM_BO/circuit_agent/outputs/<project>/physical/`。只有真实 DRC 零 violation 且 LVS 无 issue
时，`physical_state.json` 才会记录 `status=done`。
