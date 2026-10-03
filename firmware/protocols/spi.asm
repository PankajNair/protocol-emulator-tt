; SPI master, mode 0 (CPOL=0: SCLK idles low; CPHA=0: data sampled on the
; rising edge, changed on the falling edge), MSB first, full duplex.
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
; CS stays low for the whole transaction; SCLK simply holds low while the
; host hands over the next byte (SPI has no minimum clock rate).
;
; One register carries both directions: OUTB drives MOSI from R0 bit 7,
; then SHIFT left frees bit 0, then INB captures MISO there -- the
; TX-before-shift / RX-after-shift order docs/isa.md's Worked idioms
; prescribe, which here lets the outgoing and incoming byte share R0.
;
; Timing, 50 MHz cycles. SCK_HALF = SCLK half period (default 25 ->
; 1 MHz). Each half has 12 cycles of fixed instruction cost:
;   high: SET SCLK,1 SHIFT INB DELAY SET SCLK,0 = 12 + T_HI
;   low:  SET SCLK,0 LOOP OUTB DELAY SET SCLK,1 = 12 + T_LO
; so SCK_HALF >= 12 (fastest SCLK ~2.08 MHz). MISO is sampled 6 cycles
; after the rising edge (plus the input synchronizer); the slave only
; changes MISO on the falling edge, so it is stable there.

.equ SCK_HALF, 25
.equ MOSI, 0
.equ MISO, 1
.equ SCLK, 2
.equ CS, 3
.equ T_HI, SCK_HALF - 12
.equ T_LO, SCK_HALF - 12

        SET   CS, pp, 1               ; deselected
        SET   SCLK, pp, 0             ; mode 0 idle level
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
bit:    OUTB  R0, MOSI, 7             ; MOSI while SCLK low
        DELAY T_LO
        SET   SCLK, 1                 ; rising edge: both sides sample
        SHIFT R0, L
        INB   R0, MISO, 0
        DELAY T_HI
        SET   SCLK, 0                 ; falling edge: slave shifts MISO
        LOOP  bit, R1
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
