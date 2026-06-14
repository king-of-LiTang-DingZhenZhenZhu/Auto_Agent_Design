# Spectre 脚本编写规范 — 索引

适用于 Spectre 仿真器的 Spectre native syntax 脚本编写规范。按文件类型分为两个独立文档：

| 文档 | 适用文件 | 内容 |
|------|---------|------|
| [circuit_cir_guide.md](../../.claude/rules/circuit_cir_guide.md) | `.cir` 子电路网表 | 总体原则、命名规范、子电路编写规范、参数声明、PDK 约束 |
| [testbench_sp_guide.md](../../.claude/rules/testbench_sp_guide.md) | `.scs` 仿真 testbench | 总体原则、命名规范、testbench 编写规范、仿真指令、save/info 数据导出 |

## 通用原则

- **模块化设计**：被测电路（DUT）与测试激励分离。电路定义为子电路（`subckt`），testbench 调用子电路。
- **高可读性**：使用 `//` 注释、空行和统一的命名规范。
- **健壮性**：高增益 OTA 推荐闭环法稳定直流工作点，避免开环仿真中的收敛问题。
- **Spectre native syntax**：全程使用 Spectre 原生语法（`vsource`、`capacitor`、`ac`、`tran`、`save` 等），不使用 SPICE 兼容模式。

## 参考

- PDK 约束详见 [knowledge_base/pdk_constraints.md](../circuit_agent/knowledge_base/pdk_constraints.md)
- Spectre 详细语法规范详见 [Scs_Scirpts/Spectre.scs脚本编写规范.md](../Scs_Scirpts/Spectre.scs脚本编写规范.md)
