# Agent 辅助模拟电路设计项目
## 1. 项目概述
本项目构建了一个 **Agent 驱动的模拟电路自动设计与优化闭环系统**。用户仅需输入电路需求（可指定拓扑结构或仅给性能指标），系统即可自动完成：SPICE 网表生成 → Spectre 仿真 → 结果分析 → 参数优化 → 迭代再仿真，直至性能达标或达到迭代步数上限。
### 核心思想
```
用户需求 → Agent 生成网表 → Spectre 仿真 → 结果对比目标 → 算法优化 → 新参数 → 再仿真 → ... → 达标输出
                ↑_____________________________________________________________________________↓
                                    闭环自动优化
```
---
## 2. 系统架构
```
┌─────────────────────────────────────────────────────────────────────┐
│                         用户交互层                                   │
│                    Agent 聊天窗口（输入需求）                          │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ 需求描述
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Agent 推理层                                    │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────────┐    │
│  │  知识库/示例  │  │ 设计规则约束  │  │ SPICE/SCS 脚本规范       │    │
│  └──────┬──────┘  └──────┬───────┘  └───────────┬─────────────┘    │
│         │                │                      │                    │
│         └────────────────┼──────────────────────┘                    │
│                          ▼                                          │
│              Agent 生成初始 SPICE 网表                               │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ 初始网表文件
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    自动化优化闭环层                                    │
│                                                                     │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐     │
│   │ Spectre  │───▶│ 读取结果  │───▶│ 对比目标   │───▶│ LLM+BO   │     │
│   │  仿真     │    │ 解析参数  │    │ 计算差距   │    │  优化    │    │
│   └──────────┘    └──────────┘    └──────────┘    └────┬─────┘     │
│         ▲                                              │           │
│         │              修改网表参数                      │           │
│         └──────────────────────────────────────────────┘           │
│                                                                     │
│   终止条件：达标 ∨ 达到步数上限                                       │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ 最终结果
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       输出层                                         │
│            最终 SPICE 网表 + 优化过程报告 + 性能指标                     │
└─────────────────────────────────────────────────────────────────────┘
```
---
## 3. 详细流程
### 3.1 阶段一：需求输入与网表生成（Agent 交互）
1. **用户在 Agent 聊天窗口输入需求**，例如：
   - 指定结构：*"设计一个两级OTA，采用SMIC 180nm工艺，负载电容5pF"*
   - 仅给指标：*"增益≥60dB，带宽≥10MHz，功耗≤2mW"*
2. **Agent 参考以下资源生成初始 SPICE 网表**：
   - 当前目录下的 **示例网表**（参考拓扑与写法）
   - **知识库**（器件模型、设计经验）
   - **SPICE 脚本规范**（语法、子电路调用约定）
   - **工艺库约束**（器件尺寸范围、模型名称等）
3. **Agent 输出**：符合规范的 `.sp` 或 `.scs` 网表文件
### 3.2 阶段二：自动化优化闭环（`optimizer.py`）
> Agent 生成初始网表后，后续流程全部由自动化脚本完成，无需人工介入。
```
┌─────────────────────────────────────────────────────┐
│                 optimizer.py 主循环                   │
│                                                     │
│  初始化: 读取初始网表、目标指标、优化配置              │
│                                                     │
│  FOR step = 1 to MAX_STEPS:                         │
│    ① 调用 Spectre 执行仿真                           │
│    ② 解析仿真结果，提取性能参数                        │
│    ③ 与目标指标对比，计算目标函数                      │
│    ④ 若达标 → 输出结果，退出循环                      │
│    ⑤ LLM+BO 算法优化，生成新设计参数                  │
│    ⑥ 根据新参数修改网表                               │
│  END FOR                                            │
│                                                     │
│  输出: 最终网表 + 优化历史 + 性能报告                  │
└─────────────────────────────────────────────────────┘
```
各步骤详细说明：
| 步骤 | 操作 | 说明 |
|------|------|------|
| ① | Spectre 仿真 | 调用 `spectre` 命令运行网表，生成仿真原始数据 |
| ② | 解析结果 | 从 Spectre 输出（如 `.raw`、`.print` 等）中提取关键性能参数（增益、带宽、功耗、相位裕度等） |
| ③ | 目标对比 | 计算当前性能与目标值的差距，构建目标函数 / 奖励值 |
| ④ | 达标判断 | 所有指标满足要求则终止；否则继续 |
| ⑤ | LLM+BO 优化 | LLM 提供设计直觉与参数调整建议，BO 提供数学驱动的全局搜索，二者协同生成下一组参数 |
| ⑥ | 修改网表 | 将新参数写入网表文件（替换器件尺寸、偏置电压等可调参数） |
### 3.3 阶段三：结果输出
- **最终 SPICE 网表**：满足性能要求的设计
- **优化过程报告**：每一步的参数变化与性能指标
- **性能对比表**：目标值 vs 最终值
---
## 4. 优化算法：LLM + BO
### 4.1 方法概述
| 方法 | 角色 | 优势 |
|------|------|------|
| **BO（贝叶斯优化）** | 全局搜索，建立代理模型，采集函数指导探索 | 样本效率高，适合仿真成本高的场景 |
| **LLM（大语言模型）** | 提供电路设计直觉，解释参数-性能关系，生成合理的搜索方向 | 利用先验知识，避免无意义探索 |
### 4.2 协同策略
```
当前参数 + 性能结果
        │
        ├──▶ BO: 高斯过程建模 → 采集函数(如EI) → 建议参数区域
        │
        ├──▶ LLM: 分析当前结果 → 给出调整建议(如"增大W提高增益") → 缩小搜索空间
        │
        └──▶ 融合: LLM 约束/引导 BO 的搜索空间 → 输出下一组设计参数
```
- **LLM 作为先验**：将 LLM 的建议转化为 BO 的约束或初始点
- **BO 作为验证**：在 LLM 建议的方向上进行精细化搜索
- **交替/并行**：两者可交替提供候选参数，选择更优者执行
### 4.3 `optimizer.py` 核心接口
```python
class CircuitOptimizer:
    def __init__(self, netlist_path, targets, max_steps=50):
        """
        netlist_path: 初始网表路径
        targets: 目标指标字典, e.g. {"gain": 60, "bw": 10e6, "power": 2e-3}
        max_steps: 最大优化步数
        """
        pass
    def run_spectre(self, netlist_path) -> dict:
        """调用 Spectre 仿真并解析结果"""
        pass
    def evaluate(self, results: dict) -> float:
        """计算目标函数值（性能与目标的差距）"""
        pass
    def llm_suggest(self, history) -> dict:
        """LLM 分析历史数据，给出参数调整建议"""
        pass
    def bo_suggest(self, history) -> dict:
        """BO 基于代理模型，给出下一组候选参数"""
        pass
    def update_netlist(self, params: dict) -> str:
        """根据参数更新网表文件"""
        pass
    def optimize(self):
        """主优化循环"""
        for step in range(self.max_steps):
            results = self.run_spectre(self.current_netlist)
            score = self.evaluate(results)
            if self.is_target_met(results):
                print(f"✅ 目标达成！步数: {step+1}")
                break
            # LLM+BO 协同优化
            llm_params = self.llm_suggest(self.history)
            bo_params  = self.bo_suggest(self.history)
            next_params = self.merge_suggestions(llm_params, bo_params)
            self.current_netlist = self.update_netlist(next_params)
            self.history.append((next_params, results, score))
        else:
            print(f"⚠️ 达到步数上限 ({self.max_steps})，未完全达标")
```
---
## 5. 约束与规范
### 5.1 SPICE/SCS 脚本规范
- 网表文件须包含清晰的 **子电路定义**，可调参数使用 `.param` 声明
- 仿真控制语句（`.dc`、`.ac`、`.tran` 等）须完整
- 输出语句须便于自动化解析（如 `.print`、`.meas`）
- 示例结构：
```spice
* OTA Two-Stage Design
.param W1=10u L1=180n W2=20u L2=180n
.param CC=1p Vbias=0.8
.include 'smic18mm.lib'
.subckt OTA vin vip vout vdd vss
    ... (子电路实现)
.ends OTA
X1 vin vip vout vdd vss OTA
* 仿真设置
.ac dec 10 1 10G
.meas ac gain_db find vdb(vout) at=1
.meas ac bw when vdb(vout)=0
...
.print ac vdb(vout)
.end
```
### 5.2 工艺库约束
- 器件尺寸范围（如 W: 1u~100u, L: 180n~5u）
- 模型名称与库文件路径
- 电压域限制（VDD 范围等）
- 匹配规则（差分对尺寸一致等）
---
## 6. 环境部署（CentOS 7 虚拟机）
### 6.1 系统要求
- **OS**: CentOS 7
- **EDA**: Cadence Spectre（已安装并配置 license）
- **Python**: ≥ 3.8
- **依赖**: 见 `requirements.txt`
### 6.2 环境变量配置
#### API Key 设置
```bash
# 若 env 脚本中已包含则无需配置，否则手动添加：
echo 'export DEEPSEEK_API_KEY="sk-xxxxx"' >> ~/.bashrc
source ~/.bashrc
```
#### Spectre 环境变量
```bash
# 根据实际安装路径配置
echo 'export CDS_HOME=/opt/cadence' >> ~/.bashrc
echo 'export PATH=$CDS_HOME/tools/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=$CDS_HOME/tools/lib:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```
#### Python 依赖安装
```bash
pip install -r requirements.txt
```
`requirements.txt` 示例：
```text
openai>=1.0.0
bayesian-optimization>=1.4.0
numpy>=1.21.0
scipy>=1.7.0
pandas>=1.3.0
```
### 6.3 完整部署脚本
```bash
#!/bin/bash
# deploy.sh — CentOS7 一键部署脚本
set -e
echo "===== 1. 配置 API Key ====="
if [ -z "$DEEPSEEK_API_KEY" ]; then
    echo 'export DEEPSEEK_API_KEY="sk-xxxxx"' >> ~/.bashrc
    echo "DEEPSEEK_API_KEY 已写入 ~/.bashrc"
else
    echo "DEEPSEEK_API_KEY 已存在，跳过"
fi
echo "===== 2. 配置 Spectre 环境 ====="
CDS_HOME=${CDS_HOME:-/opt/cadence}
echo "export CDS_HOME=$CDS_HOME" >> ~/.bashrc
echo "export PATH=\$CDS_HOME/tools/bin:\$PATH" >> ~/.bashrc
echo "export LD_LIBRARY_PATH=\$CDS_HOME/tools/lib:\$LD_LIBRARY_PATH" >> ~/.bashrc
echo "===== 3. 安装 Python 依赖 ====="
pip install -r requirements.txt
echo "===== 4. 刷新环境 ====="
source ~/.bashrc
echo "===== 部署完成 ====="
```
---
## 7. 项目目录结构
```
project/
├── agent/                    # Agent 相关模块
│   ├── prompt_templates/     # Agent 提示词模板
│   ├── knowledge_base/       # 知识库（设计规则、经验等）
│   └── netlist_writer.py     # 网表生成逻辑
│
├── examples/                 # 示例网表
│   ├── ota_two_stage.sp
│   ├── folded_cascode.sp
│   └── ...
│
├── optimizer.py              # 🔑 核心优化脚本（LLM+BO 闭环）
├── spectre_runner.py         # Spectre 仿真调用与结果解析
├── config.yaml               # 优化配置（目标、步数、参数范围等）
├── requirements.txt          # Python 依赖
├── deploy.sh                 # 环境部署脚本
│
├── output/                   # 优化结果输出
│   ├── final_netlist.sp
│   ├── optimization_log.csv
│   └── report.html
│
└── README.md                 # 本文件
```
---
## 8. 快速开始
```bash
# 1. 部署环境
bash deploy.sh
# 2. 在 Agent 聊天窗口输入需求，生成初始网表
#    → 自动生成 output/initial_netlist.sp
# 3. 启动优化闭环
python optimizer.py \
    --netlist output/initial_netlist.sp \
    --config config.yaml \
    --max_steps 50
# 4. 查看结果
cat output/optimization_log.csv
```
---
## 9. 配置文件示例（`config.yaml`）
```yaml
# 目标性能指标
targets:
  gain_db: 60        # 增益 ≥ 60dB
  bw_mhz: 10         # 带宽 ≥ 10MHz
  power_mw: 2        # 功耗 ≤ 2mW
  phase_margin_deg: 60  # 相位裕度 ≥ 60°
# 可调参数及其范围
parameters:
  W1:  { min: 1e-6,  max: 100e-6,  init: 10e-6  }
  L1:  { min: 180e-9, max: 5e-6,   init: 180e-9 }
  W2:  { min: 1e-6,  max: 100e-6,  init: 20e-6  }
  CC:  { min: 0.1e-12, max: 10e-12, init: 1e-12  }
  Vbias: { min: 0.4,  max: 1.2,    init: 0.8    }
# 优化设置
optimization:
  max_steps: 50
  bo:
    n_initial: 5        # BO 初始随机采样数
    acquisition: EI     # 采集函数类型
  llm:
    model: deepseek-chat
    temperature: 0.3
# 仿真设置
simulation:
  spectre_bin: spectre
  timeout: 300          # 单次仿真超时（秒）
```
