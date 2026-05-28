# SPICE 脚本编写规范 — 索引

适用于 Spectre 仿真器的 SPICE 格式脚本编写规范。按文件类型分为两个独立文档：

| 文档 | 适用文件 | 内容 |
|------|---------|------|
| [circuit_cir_guide.md](circuit_cir_guide.md) | `.cir` 子电路网表 | 总体原则、命名规范、子电路编写规范、参数声明、PDK 约束 |
| [testbench_sp_guide.md](testbench_sp_guide.md) | `.sp` 仿真 testbench | 总体原则、命名规范、testbench 编写规范、仿真指令、.measure 测量语句 |

## 通用原则

- **模块化设计**：被测电路（DUT）与测试激励分离。电路定义为子电路（`.subckt`），testbench 调用子电路。
- **高可读性**：善用注释、空行和统一的命名规范。
- **健壮性**：高增益 OTA 推荐闭环法稳定直流工作点，避免开环仿真中的收敛问题。
- **Spectre 兼容**：Spectre（SPICE 语法模式）**不支持** `.control/.endc` 块，所有仿真指令以独立行书写。

## 参考

- PDK 约束详见 [knowledge_base/pdk_constraints.md](../circuit_agent/knowledge_base/pdk_constraints.md)
- Spectre SCS 格式规范详见 [Scs_Scirpts/Spectre.scs脚本编写规范.md](../Scs_Scirpts/Spectre.scs脚本编写规范.md)
