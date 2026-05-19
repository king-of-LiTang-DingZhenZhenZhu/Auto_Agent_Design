* ============================================================
* Circuit: 5-Transistor OTA (Single-Stage)
* Process: TSMC N28
* Description: Basic differential pair with active load
* ============================================================

* --- Include PDK ---
.lib '/mnt/hgfs/Share/PDKS/TSMC28nm/models/spectre/toplevel.scs' top_tt

* --- Global Nodes ---
.global vdd! gnd!

* --- Optimizable Parameters ---
.param W1 = 2u
.param W3 = 4u
.param W5 = 4u
.param L1 = 60n
.param Ibias = 50u

* --- Core Circuit ---
.subckt ota_5t INP INN OUT VDD VSS

* Input differential pair (NMOS)
MN1  net_outp  INP  net_tail  VSS  nch_mac  w=W1  l=L1  nf=4  m=1
MN2  OUT       INN  net_tail  VSS  nch_mac  w=W1  l=L1  nf=4  m=1

* PMOS active load (current mirror)
MP3  net_outp  net_outp  VDD  VDD  pch_mac  w=W3  l=L1  nf=4  m=1
MP4  OUT       net_outp  VDD  VDD  pch_mac  w=W3  l=L1  nf=4  m=1

* Tail current source
MN5  net_tail  net_bias  VSS  VSS  nch_mac  w=W5  l=L1  nf=4  m=1

.ends ota_5t

* --- Testbench ---
* Power supply
Vdd  vdd!  gnd!  DC 0.9

* Bias current (ideal current source to set bias voltage)
Ibias_src  vdd!  net_bias  DC Ibias
MN_bias  net_bias  net_bias  gnd!  gnd!  nch_mac  w=W5  l=L1  nf=4  m=1

* Common mode
Vcm  net_cm  gnd!  DC 0.45

* Differential AC input
Vip  net_inp  net_cm  DC 0  AC  0.5
Vin  net_inn  net_cm  DC 0  AC -0.5

* DUT instantiation
XDUT  net_inp  net_inn  net_out  vdd!  gnd!  ota_5t

* Load capacitor
CL  net_out  gnd!  500f

* --- Simulation Control ---
.op
.ac dec 20 1 10G

* --- Measurements ---
.meas ac gain_db MAX VDB(net_out)
.meas ac ugf WHEN VDB(net_out)=0 CROSS=1
.meas ac phase_margin FIND VP(net_out) WHEN VDB(net_out)=0 CROSS=1
.meas dc power_total PARAM='-I(Vdd)*0.9'

.end
