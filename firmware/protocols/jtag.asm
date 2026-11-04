; JTAG (IEEE 1149.1) host: TAP reset and IR/DR scans of any length up to
; 255 bits -- enough to read the IDCODEs of a scan chain, find its length
; with BYPASS, and load and read data registers.
;
; The chip walks the TAP state machine with TMS: every scan starts and
; ends in Run-Test/Idle, so scans can follow each other with no state
; kept on the host. The last bit of a scan is shifted with TMS = 1 (it
; leaves Shift-xR for Exit1-xR), then Update-xR, then back to Idle.
;
; Pins: TDI = 0, TMS = 1, TCK = 2 (push-pull outputs, TCK idles low),
; TDO = 3 (input). The chip changes TDI and TMS only while TCK is low and
; samples TDO just before raising TCK; the target samples TDI and TMS on
; the rising edge and changes TDO on the falling edge. TCK ~= 1.1 MHz.
; Bits are LSB first.
;
; Host protocol (IN = host sends a byte, OUT = host asks for one):
;   IN 1               TAP reset: 6 clocks with TMS = 1 (Test-Logic-Reset
;                      from any state), 1 with TMS = 0 (Idle); OUT 0
;   IN 2, IN n         IR scan of n bits (n = 1..255)
;   IN 3, IN n         DR scan of n bits
;     then for each byte of the scan, ceil(n / 8) times:
;                      IN the next 8 TDI bits, OUT the 8 TDO bits shifted
;                      out meanwhile. In the last byte only the low
;                      n mod 8 bits (all 8 if n is a multiple of 8) are
;                      shifted; the rest of the TDO byte reads 0.
;   n = 0, or any other command, sets HOST_ERROR.

.equ TDI, 0
.equ TMS, 1
.equ TCK, 2
.equ TDO, 3
.equ H, 6                          ; half-period padding (cycles)
.equ NB, 0                         ; data region: bits left to scan
.equ PAD, 1                        ; 8 - bits in the last byte

        SET  TCK, pp, 0
        SET  TMS, pp, 1
        SET  TDI, pp, 0
        SET  TDO, in, 0

main:   CALL getb
        LDI  R1, 1
        CMP  R0, R1
        BEQ  treset
        LDI  R1, 2
        CMP  R0, R1
        BEQ  irscan
        LDI  R1, 3
        CMP  R0, R1
        BEQ  drscan
bad:    SET  HOST_ERROR, 1        ; unknown command, or n = 0
        JMP  main

; ------------------------------------------------------------ TAP reset
treset: SET  TMS, 1
        LDI  R2, 6
tr1:    CALL tclk
        LOOP tr1, R2
        SET  TMS, 0
        CALL tclk                 ; -> Run-Test/Idle
        LDI  R0, 0
        CALL putb
        JMP  main

; ------------------------------------------------------------ scans
; Idle -> Select-DR (1) -> Select-IR (1) -> Capture-IR (0) -> Shift-IR (0)
irscan: SET  TMS, 1
        CALL tclk
        JMP  sel
; Idle -> Select-DR (1) -> Capture-DR (0) -> Shift-DR (0)
drscan: SET  TMS, 1
sel:    CALL tclk
        CALL getb                 ; n
        LDI  R1, 0
        CMP  R0, R1
        BEQ  bad                  ; (TAP is in Select-xR: the next IN 1 recovers)
        STORE R0, NB
        SET  TMS, 0
        CALL tclk                 ; Capture-xR
        CALL tclk                 ; Shift-xR

byte:   CALL getb                 ; R0 = next 8 TDI bits
        LDI  R1, 0                ; TDO accumulator
        LOAD R3, NB
        LDI  R2, 0xF8             ; R2 = NB - 8, carry iff NB >= 8
        ADD  R2, R3
        BNE  short
        STORE R2, NB
        LDI  R3, 0
        CMP  R2, R3
        BEQ  last8                ; exactly 8 left
        LDI  R2, 8                ; a full byte, more to come
b8:     CALL bit
        LOOP b8, R2
        CALL put1
        JMP  byte

last8:  LDI  R3, 0
        STORE R3, PAD
        LDI  R2, 7
        JMP  lastn
short:  LDI  R2, 0xFF             ; PAD = 8 - NB = (NB ^ 0xFF) + 9
        XOR  R2, R3
        LDI  R1, 9
        ADD  R2, R1
        STORE R2, PAD
        LDI  R1, 0
        LDI  R2, 0xFF             ; R2 = NB - 1 bits before the last
        ADD  R2, R3
        LDI  R3, 0
        CMP  R2, R3
        BEQ  lastb                ; a 1-bit tail: no bits before the last
lastn:  CALL bit                  ; R2 bits with TMS = 0
        LOOP lastn, R2
lastb:  SET  TMS, 1               ; the last bit: Shift-xR -> Exit1-xR
        CALL bit
        LOAD R2, PAD              ; right-align a short last byte
        LDI  R3, 0
        CMP  R2, R3
        BEQ  done
align:  SHIFT R1, R
        LOOP align, R2
done:   CALL tclk                 ; Exit1-xR -> Update-xR (TMS still 1)
        SET  TMS, 0
        CALL tclk                 ; -> Run-Test/Idle
        CALL put1
        JMP  main

; ------------------------------------------------------------ bit level
; bit: shift R0 bit 0 out on TDI and TDO into R1 from the top, one TCK
; cycle, at whatever TMS is set to.
bit:    OUTB R0, TDI, 0
        SHIFT R0, R
        DELAY H
        SHIFT R1, R
        INB  R1, TDO, 7           ; sample before the rise
        SET  TCK, 1               ; target samples TDI/TMS on the rise
        DELAY H
        SET  TCK, 0               ; target moves TDO on the fall
        RET

; tclk: one TCK cycle at the current TMS, TDO ignored.
tclk:   DELAY H
        SET  TCK, 1
        DELAY H
        SET  TCK, 0
        RET

; put1: R1 -> host
put1:   LDI  R0, 0
        XOR  R0, R1
        JMP  putb

; IN handshake: host byte -> R0
getb:   WAIT HOST_GO, 1
        IN   R0
        SET  HOST_STATUS, 1
        WAIT HOST_GO, 0
        SET  HOST_STATUS, 0
        RET

; OUT handshake (host-initiated): R0 -> host
putb:   WAIT HOST_GO, 1
        OUT  R0
        SET  HOST_STATUS, 1
        WAIT HOST_GO, 0
        SET  HOST_STATUS, 0
        RET
