; SPI master, all four modes, MSB first, full duplex.
;
;   MODE  CPOL  CPHA   SCLK idle   data sampled on    data changed on
;    0     0     0       low       rising (leading)   falling (trailing)
;    1     0     1       low       falling (trailing) rising (leading)
;    2     1     0       high      falling (leading)  rising (trailing)
;    3     1     1       high      rising (trailing)  falling (leading)
; Select with -D MODE=n (default 0). "Leading" is the first edge after
; CS falls (idle -> active level), "trailing" the return to idle.
;
; Pins (docs/architecture.md Pin map): MOSI = 0, MISO = 1, SCLK = 2,
; CS = 3 (active low). MOSI/SCLK/CS push-pull, MISO input.
;
; Host protocol (lockstep, as in i2c.asm):
;   1. IN  len                 bytes in this transaction, 1..255
;   2. firmware drops CS
;   3. per byte: IN tx byte -> exchanged on the bus -> OUT rx byte
;      (host-initiated OUT handshake)
;   4. after the last byte firmware raises CS and goes idle
; CS stays low for the whole transaction; SCLK holds its idle level while
; the host hands over the next byte (SPI has no minimum clock rate).
;
; One register carries both directions: OUTB drives MOSI from R0 bit 7,
; then SHIFT left frees bit 0, then INB captures MISO there -- the
; TX-before-shift / RX-after-shift order of docs/isa.md's Worked idioms.
;
; Timing, 50 MHz cycles; SCK_HALF = SCLK half period (default 25 ->
; 1 MHz). Fixed instruction cost per half (every instruction 3 cycles,
; DELAY N 3 + N; a pin changes when its instruction commits):
;   CPHA 0  active: SET SHIFT INB DELAY SET       = 12 + T_ACT
;           idle:   SET LOOP OUTB DELAY SET       = 12 + T_IDL
;   CPHA 1  active: SET OUTB DELAY SET            =  9 + T_ACT
;           idle:   SET SHIFT INB DELAY LOOP SET  = 15 + T_IDL
; so SCK_HALF >= 12 (CPHA 0) or >= 15 (CPHA 1). MISO is sampled 6 cycles
; after the sampling edge (plus the input synchronizer); the slave only
; changes it on the other edge, so it is stable there.

.equ MODE, 0
.equ SCK_HALF, 25
.equ MOSI, 0
.equ MISO, 1
.equ SCLK, 2
.equ CS, 3
.equ CPOL, MODE // 2
.equ CPHA, MODE - 2 * CPOL
.equ SCK_IDLE, CPOL
.equ SCK_ACTIVE, 1 - CPOL
.if CPHA
.equ T_ACT, SCK_HALF - 9
.equ T_IDL, SCK_HALF - 15
.else
.equ T_ACT, SCK_HALF - 12
.equ T_IDL, SCK_HALF - 12
.endif

        SET   CS, pp, 1               ; deselected
        SET   SCLK, pp, SCK_IDLE          ; CPOL idle level
        SET   MOSI, pp, 0
        SET   MISO, in, 0

idle:   WAIT  HOST_GO, 1              ; IN len
        IN    R2
        SET   HOST_STATUS, 1
        WAIT  HOST_GO, 0
        SET   HOST_STATUS, 0
        SET   CS, 0                   ; select the slave
        DELAY SCK_HALF                ; CS setup before the first edge

xfer:   WAIT  HOST_GO, 1              ; IN tx byte
        IN    R0
        SET   HOST_STATUS, 1
        WAIT  HOST_GO, 0
        SET   HOST_STATUS, 0
        LDI   R1, 8
.if CPHA
bit:    SET   SCLK, SCK_ACTIVE            ; leading edge: both sides change data
        OUTB  R0, MOSI, 7
        DELAY T_ACT
        SET   SCLK, SCK_IDLE              ; trailing edge: both sides sample
        SHIFT R0, L
        INB   R0, MISO, 0
        DELAY T_IDL
        LOOP  bit, R1
.else
bit:    OUTB  R0, MOSI, 7             ; data while SCLK idle
        DELAY T_IDL
        SET   SCLK, SCK_ACTIVE            ; leading edge: both sides sample
        SHIFT R0, L
        INB   R0, MISO, 0
        DELAY T_ACT
        SET   SCLK, SCK_IDLE              ; trailing edge: slave changes MISO
        LOOP  bit, R1
.endif
        WAIT  HOST_GO, 1              ; OUT rx byte (host-initiated)
        OUT   R0
        SET   HOST_STATUS, 1
        WAIT  HOST_GO, 0
        SET   HOST_STATUS, 0
        LOOP  xfer, R2

        DELAY SCK_HALF                ; CS hold after the last edge
        SET   CS, 1
        SET   MOSI, 0
        JMP   idle
