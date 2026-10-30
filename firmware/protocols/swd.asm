; SWD (ARM Serial Wire Debug) host: line reset and DP/AP register
; transfers -- enough to read a Cortex-M's IDCODE and drive its debug port.
;
; Not a protocol this chip was designed around: SWDIO turns around mid-
; transfer (the chip drives the request, the target drives ACK and read
; data, each with a turnaround cycle where neither does), and both the
; request and the data carry even parity, computed with XOR folds.
;
; Pins: SWDIO = 0 (push-pull while the chip drives it, input while the
; target does), SWCLK = 2 (push-pull, idles low). Bits are LSB first. The
; chip sets SWDIO while SWCLK is low and raises SWCLK (the target samples
; on the rising edge); the target drives after a rising edge and the chip
; samples while SWCLK is low, before the next rise. SWCLK ~= 1.2 MHz.
;
; Host protocol (IN = host sends a byte, OUT = host asks for one):
;   IN 1                 line reset: JTAG-to-SWD switch (56 ones, 0xE79E,
;                        56 ones), then 2 idle cycles; OUT 0
;   IN 2, IN h           transfer; h = APnDP | RnW<<1 | A2<<2 | A3<<3
;     write (RnW = 0):   IN 4 data bytes (LSB first); OUT ack
;     read  (RnW = 1):   OUT ack, then OUT 4 data bytes (LSB first) and
;                        OUT 1 if the target's data parity was right, else 0
;   ack is the target's 3-bit ACK: 1 = OK, 2 = WAIT, 4 = FAULT (7: no
;   response). Data is only exchanged after OK; otherwise the transfer ends.

.equ DIO, 0
.equ CLK, 2
.equ H, 6                          ; half-period padding (cycles)
.equ D0, 0                         ; data bytes, data region 0-3
.equ ACKV, 4

        SET  CLK, pp, 0
        SET  DIO, pp, 1

main:   CALL getb
        LDI  R1, 1
        CMP  R0, R1
        BEQ  lreset
        LDI  R1, 2
        CMP  R0, R1
        BEQ  xfer
        SET  HOST_ERROR, 1        ; unknown command
        JMP  main

; ------------------------------------------------------------ line reset
lreset: SET  DIO, pp, 1
        LDI  R3, 7                ; 56 ones
lr1:    LDI  R0, 0xFF
        LDI  R2, 8
        CALL wbits
        LOOP lr1, R3
        LDI  R0, 0x9E             ; JTAG-to-SWD select, 0xE79E LSB first
        LDI  R2, 8
        CALL wbits
        LDI  R0, 0xE7
        LDI  R2, 8
        CALL wbits
        LDI  R3, 7                ; 56 ones again
lr2:    LDI  R0, 0xFF
        LDI  R2, 8
        CALL wbits
        LOOP lr2, R3
        LDI  R0, 0                ; idle cycles
        LDI  R2, 2
        CALL wbits
        LDI  R0, 0
        CALL putb
        JMP  main

; ------------------------------------------------------------ transfer
xfer:   CALL getb                 ; R0 = h (4 bits)
        STORE R0, ACKV            ; (keep h: RnW is bit 1)
        LDI  R1, 0                ; parity of the 4 header bits:
        XOR  R1, R0               ; R1 = h
        LDI  R3, 0                ; h ^= h >> 2
        XOR  R3, R1
        SHIFT R3, R
        SHIFT R3, R
        XOR  R1, R3
        LDI  R3, 0                ; h ^= h >> 1: bit 0 = b0^b1^b2^b3
        XOR  R3, R1
        SHIFT R3, R
        XOR  R1, R3
        ; request = 1 | h << 1 | p << 5 | 0 << 6 | 1 << 7
        SHIFT R0, L               ; h << 1
        LDI  R3, 0x81             ; start + park
        XOR  R0, R3
        TEST R1, 0
        BNE  np
        LDI  R3, 0x20
        XOR  R0, R3
np:     SET  DIO, pp, 0
        LDI  R2, 8
        CALL wbits                ; request
        SET  DIO, in, 0           ; turnaround: release SWDIO
        LDI  R2, 1
        CALL cycle
        LDI  R2, 3                ; ACK, 3 bits
        CALL rbits                ; -> R0 bits 7..5
        SHIFT R0, R               ; -> bits 2..0
        SHIFT R0, R
        SHIFT R0, R
        SHIFT R0, R
        SHIFT R0, R
        LOAD R1, ACKV             ; h
        STORE R0, ACKV            ; ack
        LDI  R3, 1
        CMP  R0, R3
        BNE  notok
        TEST R1, 1                ; RnW
        BEQ  rd
        ; ---- write: turnaround, 32 data bits, parity
        LDI  R2, 1
        CALL cycle
        SET  DIO, pp, 0
        CALL getb
        STORE R0, D0
        LDI  R2, 8
        CALL wbits
        CALL getb
        STORE R0, D0+1
        LDI  R2, 8
        CALL wbits
        CALL getb
        STORE R0, D0+2
        LDI  R2, 8
        CALL wbits
        CALL getb
        STORE R0, D0+3
        LDI  R2, 8
        CALL wbits
        CALL dpar                 ; R1 bit 0 = parity of D0-D3
        LDI  R0, 0
        XOR  R0, R1
        LDI  R2, 1
        CALL wbits
        LOAD R0, ACKV
        CALL putb
        JMP  idle
        ; ---- read: 32 data bits, parity, turnaround
rd:     LOAD R0, ACKV
        CALL putb
        LDI  R2, 8
        CALL rbits
        STORE R0, D0
        LDI  R2, 8
        CALL rbits
        STORE R0, D0+1
        LDI  R2, 8
        CALL rbits
        STORE R0, D0+2
        LDI  R2, 8
        CALL rbits
        STORE R0, D0+3
        LDI  R2, 1
        CALL rbits                ; target's parity -> R0 bit 7
        STORE R0, ACKV
        LDI  R2, 1                ; turnaround back to the chip
        CALL cycle
        SET  DIO, pp, 0
        LOAD R0, D0
        CALL putb
        LOAD R0, D0+1
        CALL putb
        LOAD R0, D0+2
        CALL putb
        LOAD R0, D0+3
        CALL putb
        CALL dpar                 ; R1 bit 0 = computed parity
        LOAD R0, ACKV             ; bit 7 = received parity
        LDI  R3, 0
        TEST R0, 7
        BEQ  rp1
        JMP  rp
rp1:    LDI  R3, 1
rp:     XOR  R3, R1               ; R3 bit 0 = 0 iff they agree
        LDI  R0, 1
        TEST R3, 0
        BNE  rpok
        LDI  R0, 0
rpok:   CALL putb
        JMP  idle
notok:  LDI  R2, 1                ; no OK: turnaround, report, end
        CALL cycle
        SET  DIO, pp, 0
        LOAD R0, ACKV
        CALL putb
idle:   SET  DIO, pp, 0           ; a few idle cycles
        LDI  R0, 0
        LDI  R2, 2
        CALL wbits
        JMP  main

; ------------------------------------------------------------ bit level
; wbits: drive the low R2 bits of R0, LSB first, one SWCLK cycle each.
wbits:  OUTB R0, DIO, 0
        DELAY H
        SET  CLK, 1               ; target samples on the rise
        DELAY H
        SET  CLK, 0
        SHIFT R0, R
        LOOP wbits, R2
        RET

; rbits: R2 cycles reading SWDIO into R0 from the top (LSB first: after 8
; reads the first bit is bit 0; after n < 8 it is bit 8 - n).
rbits:  DELAY H
        SHIFT R0, R
        INB  R0, DIO, 7           ; sample while SWCLK is low
        SET  CLK, 1               ; target drives the next bit after this
        DELAY H
        SET  CLK, 0
        LOOP rbits, R2
        RET

; cycle: R2 clock cycles with SWDIO left alone (turnaround).
cycle:  DELAY H
        SET  CLK, 1
        DELAY H
        SET  CLK, 0
        LOOP cycle, R2
        RET

; dpar: R1 bit 0 = even parity of D0-D3 (XOR the bytes, then fold).
dpar:   LOAD R1, D0
        LOAD R3, D0+1
        XOR  R1, R3
        LOAD R3, D0+2
        XOR  R1, R3
        LOAD R3, D0+3
        XOR  R1, R3
        LDI  R3, 0                ; fold: x ^= x >> 4, >> 2, >> 1
        XOR  R3, R1               ; leaves the XOR of all 8 bits in bit 0
        SHIFT R3, R
        SHIFT R3, R
        SHIFT R3, R
        SHIFT R3, R
        XOR  R1, R3
        LDI  R3, 0
        XOR  R3, R1
        SHIFT R3, R
        SHIFT R3, R
        XOR  R1, R3
        LDI  R3, 0
        XOR  R3, R1
        SHIFT R3, R
        XOR  R1, R3
        RET

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
