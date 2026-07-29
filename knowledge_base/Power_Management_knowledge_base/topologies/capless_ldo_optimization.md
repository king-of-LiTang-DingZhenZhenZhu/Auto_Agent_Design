# Cap-less LDO 优化知识

## 1. 当前架构

- 输入/输出：`VIN=1.8 V`，`VOUT=0.9 V`。
- pass device：高边 PMOS，源极接 VIN，漏极接 VOUT。
- error amplifier：冻结的 `two_stage_ota` 子模块。
- reference：首版由父模块 `vref` 端口输入 `0.45 V`，不在 LDO BO 内展开。
- feedback：`RfbTop/RfbBottom/Cff` 汇合到 `vfb`，再统一经过 `iprobe`
  接入 error-amplifier 输入，保证 STB 截获全部反馈路径。
- compensation：`Rgate`、`Ccomp`、`Cff` 和零负载 `Rbleed`。
- 外接负载电容范围：`1 pF` 近似最小寄生至 `200 pF`。

## 2. PDK 硬约束

该拓扑不能使用默认 `core_0p9` 管承受 1.8 V。运行前必须通过外部
`PDK_PROFILE_FILE` 配置经过验证的 1.8 V IO voltage domain，包括：

- Spectre model path/section；
- IO NMOS/PMOS model 名；
- `max_device_voltage >= 1.8 V`；
- IO 管适用的 W/L 限制；
- 子运放使用 gm/Id 时对应的 IO lookup table。

父级与 error-amplifier child 必须使用同一 PDK profile 和 voltage domain。

## 3. 指标定义

| 指标 | 当前目标 | 仿真 |
|---|---:|---|
| 输出电压 | `0.9 V ± 10 mV` | `0-10 mA` DC load sweep 的空载点 |
| 环路 DC 增益 | `> 60 dB` | 零负载、最小 CL 的 Spectre STB |
| 环路 GBW | `> 1 MHz` | STB `loopGain` 首次 0 dB 交越 |
| 相位裕度 | `> 60°` | 零负载、最小 CL 的 STB |
| Load regulation | `< 30 uV/mA = 0.03 V/A` | `ptp(VOUT)/10 mA` |
| DC PSR | `< -62 dB` | 1 mHz 近 DC 的 `20log10(abs(VOUT/VIN))` |
| Overshoot | `< 250 mV` | 10 mA→0、10 ns load edge |
| Undershoot | `< 280 mV` | 0→10 mA、10 ns load edge |

> 用户原文为 `LDR<30uA/mA`。当前实现按常见 LDO 电压调整率单位解释为
> `30 uV/mA`；开始真实 BO 前应再次确认。

## 4. 一阶关系

反馈输出：

```text
VOUT ~= VREF * (1 + RfbTop/RfbBottom)
     = VREF * (1 + feedback_ratio)
```

负载调整率：

```text
LDR = abs(delta_VOUT / delta_ILOAD)
```

闭环调节能力随环路 DC 增益提高而改善，但 pass 管尺寸、输出极点、error
amplifier 输出电阻及其 gate-drive 能力会共同改变环路极点。

## 5. 参数影响

- `Wpass` 增大：通常改善满载压降和负载调整率，但 gate capacitance 增大，
  可能降低非主极点并恶化空载 PM、瞬态和面积。
- `Lpass` 增大：可能提高输出电阻和 DC 增益，但降低单位宽度驱动能力。
- `feedback_ratio`：主要调输出 DC 电压；不应替代 loop-gain 修复。
- `Rfb_bottom` 增大：降低 divider quiescent current，但提高噪声和寄生敏感度。
- `Rgate`：隔离 error amplifier 与大 pass gate；过大可能引入低频极点。
- `Ccomp` 增大：通常降低 GBW、改善部分稳定性条件，但可能拖慢恢复。
- `Cff`：在反馈路径引入前馈零点；方向必须结合 STB 波形判断。
- `Rbleed` 减小：提高零负载最小电流并可能稳定输出极点，但增加静态功耗。

## 6. Review 顺序

### BO 成功

1. 确认 PDK/voltage domain 和所有器件端压安全。
2. 检查 Mpass 满载电流密度、栅压范围、尺寸和 OP 区域。
3. 检查 error amplifier 输出级是否能充放 pass gate。
4. 检查零负载 STB，不只读取单个 PM 数字。
5. 检查 divider/bleed/bias 静态电流和隐藏过设计。
6. 检查 1 pF 与 200 pF、0 与 10 mA 的 PVT 稳定性和瞬态。

### BO 失败

1. 先确认 DC 输出、满载供电能力和 pass gate headroom。
2. DC 正常后再分析 STB 极点/零点及 PM/GBW。
3. 将 load regulation、PSR 和瞬态 gap 分开诊断。
4. 只有证据表明 child drive/gain 不足时才重新优化 error amplifier。
5. 补偿方向不明确时先做局部参数扰动，不直接大范围改搜索空间。

## 7. 首版边界

- 不优化 reference 子模块；`vref` 必须由外部已验证参考源提供。
- 不做 child-parent joint optimization。
- 不包含封装、电源走线 ESR/ESL、负载板级寄生和 Monte Carlo。
- 真实 Spectre 首跑需要验证 `stb probe=Xdut.Iloop` 和 PSF `loopGain` 命名
  是否与当前 Cadence 版本一致。
