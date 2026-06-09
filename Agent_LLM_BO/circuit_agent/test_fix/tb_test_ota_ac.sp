* tb_test_ota_ac.sp -- 测试 AC testbench
.include "./circuit.cir"

* --- Power supply ---
VDD vdd 0 DC 0.9
VSS vss 0 DC 0
Vbias vtail 0 DC 0.5

* --- Input stimulus ---
Vcm vcm 0 DC 0.45
Vinp vinp vcm DC 0 AC 1
Vinn vinn 0  DC 0

* --- Closed-loop feedback ---
Rfb vout vinn 1G
Cfb vinn 0 1

* --- DUT ---
Xdut vinp vinn vout vtail vdd vss test_ota
CL vout 0 500f

* --- Analysis ---
.op
.ac dec 20 1 1g
.temp 27

* --- Measurements ---
.measure ac gain_dc find vdb(vout) at=1k
.measure ac phase_dc find vp(vout) at=1k
.measure ac gbw_hz when vdb(vout)=0 cross=1
.measure ac phase_at_ugf find vp(vout) when vdb(vout)=0 cross=1
.measure dc power_total PARAM='-I(Vdd)*0.9'

.end
