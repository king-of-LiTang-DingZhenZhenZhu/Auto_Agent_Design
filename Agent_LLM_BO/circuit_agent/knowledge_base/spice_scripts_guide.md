# 模拟 IC SPICE 脚本编写规范
## 1. 总体原则
* **模块化设计**：将被测电路（DUT）与测试激励分离。电路定义为子电路（`.subckt`），测试台调用子电路，便于复用和不同仿真模式的切换。
* **高可读性**：善用注释、空行和统一的命名规范，让脚本像代码一样易读。
* **健壮性**：采用闭环等稳定直流工作点的测试方法，避免开环 OTA 仿真中常见的直流不收敛问题。
---
## 2. 文件与目录结构规范
建议将不同功能的文件分目录存放，引用时使用相对路径：
```text
project/
├── pdk/           # 这个不做要求，一般PDK 库文件放在别的地方
├── design/        # 电路网表
├── tb/            # 测试台脚本
└── simulation/    # 仿真输出数据
```
---
## 3. 命名规范
* **文件命名**：小写字母，下划线分隔，带前缀表明用途。如 `tb_dc.sp`（直流测试台）、`tb_ac.sp`（交流测试台）、`two_stage_ota.cir`（电路网表）。
* **节点命名**：
  * 电源/地：`vdd`, `vss` 或 `gnd`。
  * 偏置：`ibias`, `vbp`, `vbn`。
  * 差分对：`vinp`, `vinn`。
  * 内部关键节点：带物理意义，如 `ntail`（尾电流源漏极）、`vx`（第一级输出）、`vout`（最终输出）。
* **器件命名**：
  * MOS 管：`M` + 类型 + 编号/功能。如 `MP1`（PMOS1）, `MN3`（NMOS3）, `MPTAIL`（尾电流管）。
  * 电容/电阻：`Cc`（补偿电容）, `Rz`（调零电阻）, `CL`（负载电容）。
---
## 4. 脚本内容结构与顺序
一个标准的测试台脚本应遵循以下从上到下的顺序：
### 4.1 头部注释
说明脚本名称、功能、特殊测试方法（如闭环法）。
```spice
* tb_ac.sp -- Two-Stage OTA AC Analysis (Closed-Loop Method C)
```
### 4.2 全局选项与库引入
```spice
.option compat=ps           /* 兼容性选项 */
.lib "../../pdk/...lib" tt  /* 引入工艺库和工艺角 */
.include "../design/...cir" /* 引入被测电路网表 */
```
### 4.3 电源与偏置定义
先定义全局电源，再定义偏置电流/电压。
```spice
VDD vdd 0 DC 1.2
VSS vss 0 DC 0
Ibias vdd ibias DC 50u
```
### 4.4 输入激励设置
明确共模电平（Vcm），在此基础上加交流小信号。
```spice
Vcm vcm 0 DC 300mV
Vinp vinp vcm DC 0 AC 1   /* 交流正输入 */
Vinn vinn 0  DC 0          /* 交流接地（配合闭环网络） */
```
### 4.5 闭环反馈网络（针对高增益 OTA 推荐）
使用超大电阻（1G）和超大电容（1F）构成低通网络，直流时强闭环稳定工作点，交流时开环测增益。
```spice
Rfb vout vinn 1G
Cfb vinn 0 1
```
### 4.6 被测电路调用 (DUT) 与负载
```spice
Xdut vinp vinn vout ibias vdd vss two_stage_ota_se
CL vout 0 500f
```
### 4.7 仿真命令与控制块
使用 `.control ... .endc` 将运行指令包裹，确保跨平台兼容性（特别是 Ngspice/Xyce）。
---
## 5. 电路网表编写规范
参考 `two_stage_ota.cir`，子电路内部应结构分明：
1. **声明端口**：`.subckt` 端口顺序应规范，建议顺序为：输入 -> 输出 -> 偏置 -> 电源 -> 地。
   ```spice
   .subckt two_stage_ota_se vinp vinn vout ibias vdd vss
   ```
2. **分块注释**：用 `====` 或 `---` 将偏置链、第一级、第二级、补偿网络分开，极大提高可读性。
3. **参数靠右对齐**：`W=`, `L=`, `m=` 尽量对齐。
   ```spice
   MP1 vx_l vinp ntail vdd pch W=7u L=0.24u m=1
   MP2 vx   vinn ntail vdd pch W=7u L=0.24u m=1
   ```
4. **明确结束**：子电路结尾必须使用 `.ends`，且最好带上子电路名（`.ends two_stage_ota_se`），防止嵌套时出错。
---
## 6. 仿真控制与测量规范
在 `.control` 块中，推荐按以下逻辑编写：
1. **环境设置**：`set temp=27`
2. **执行运行**：`run`
3. **工作点检查**（必须在 AC 之前）：
   ```spice
   setplot op1
   print v(vout)  /* 确保输出共模电平合理 */
   ```
4. **AC 数据处理**：
   * 先计算派生变量（dB值、相位值）：
     ```spice
     setplot ac1
     let gain_db = db(abs(v(vout)) + 1e-20)  /* 加1e-20防log(0)报错 */
     let phase_deg = 180/PI * vp(vout)
     ```
5. **特征值提取**：
   * 使用 `meas` 语句提取关键指标（DC增益、GBW、相位裕度），避免人眼查看波形误差。
   * 计算相位裕度 (PM) 的标准公式：`PM = 180 - (Phase_DC - Phase_at_UGF)` 或者 `PM = 180 + Phase_at_UGF`（取决于相位定义）。
     ```spice
     meas ac gain_dc find gain_db at=1k
     meas ac gbw_hz when gain_db=0 cross=1
     meas ac phase_dc find phase_deg at=1k
     meas ac phase_at_ugf find phase_deg when gain_db=0 cross=1
     let pm_deg = 180 - (phase_dc - phase_at_ugf)
     ```
6. **输出与保存**：
   * 用 `echo` 打印摘要到终端。
   * 用 `wrdata` 导出数据供 Python/MATLAB 后处理或绘图。
     ```spice
     echo "gain_dc=$&gain_dc gbw=$&gbw_hz pm=$&pm_deg"
     wrdata ../simulation/tb_ac/bode.csv frequency gain_db phase_deg
     ```
7. **诊断 Dump**（高级技巧）：
   * 仿真结束时，强制打印所有管子的 Vgs, Vds, Vth, Vdsat, Gm, Gds, Id。这对调试电路为什么增益不够、管子是否进入线性区至关重要。
