* tb_5t_ota_dc.sp -- Five-Transistor OTA DC Operating Point Testbench
.include "5t_ota.cir"

.param VDD=1.1 VBIAS=0.6

.options list node post

VDD   vdd   0  DC 'VDD'
VSS   vss   0  DC 0
VBIAS vbias 0  DC 'VBIAS'

VIP   vip   0  DC 0.3
VIN   vin   0  DC 0.3

Xdut vip vin vout vbias vdd vss ota_5t
CL   vout 0 1p

.op
.end
