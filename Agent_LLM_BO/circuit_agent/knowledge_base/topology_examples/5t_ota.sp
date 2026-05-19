* HSpice Netlist — Five-Transistor OTA
* TSMC 28nm CLN28HPC+ | VDD=1.1V | Vbias=600mV

* ------- 1. 引入工艺库 -------
.lib '/your/hpc/path/TSMC28nm/models/hspice/toplevel.l' TOP_TT

* ------- 2. 参数定义 -------
.param VDD=1.1 VBIAS=0.6
.param Wcm=2u  Lcm=150n
.param Wdp=2u  Ldp=150n
.param Wtail=2u Ltail=200n

* ------- 3. 电源 & 激励源 -------
VDD   vdd   0  DC 'VDD'
VBIAS vbias 0  DC 'VBIAS'

* 差分输入：共模 0.55V，AC 差分小信号（用负号表示反相，最稳妥）
VIP   vip   0  DC 0.3  AC 0.5
VIN   vin   0  DC 0.3  AC -0.5

* ------- 4. 晶体管拓扑（D G S B）-------
M5 tail  vbias vdd  vdd  pch_mac w='Wtail' l='Ltail' nf=1 M=4
M1 lout  vip   tail vdd  pch_mac w='Wdp'   l='Ldp'   nf=1
M2 vout  vin   tail vdd  pch_mac w='Wdp'   l='Ldp'   nf=1
M3 lout  lout  0    0    nch_mac w='Wcm'   l='Lcm'   nf=1
M4 vout  lout  0    0    nch_mac w='Wcm'   l='Lcm'   nf=1

* ------- 5. 负载电容 -------
CL vout 0 1p

* ------- 6. 仿真控制 -------
.options list node post
.op
.ac dec 20 0.1k 10g
.print ac vdb(vout) vp(vout)
.end