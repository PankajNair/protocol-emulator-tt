; UART loopback self-test. Jumper TX (pin 0) to RX (pin 1), boot, start.
;
; Sends every byte in PATTERNS as an 8N1 frame on TX while receiving it
; on RX on the same bit grid (one program can't run two independent
; timing loops -- docs/isa.md Known limitations -- but loopback shares
; the transmitter's grid, which is exactly the supported case). Each
; received byte must equal the one sent and its stop bit must read 1.
;
;   pass: HOST_STATUS = 1, uo_out = 0xA5, HOST_ERROR = 0
;   fail: HOST_ERROR = 1, uo_out = the byte actually received
;
; The same image works in simulation (test/test_uart_selftest.py on the
; board testbench) and on real silicon as a bring-up check: TX-RX jumper,
; read the two host pins (or LEDs on them) and uo_out.
;
; Registers: R0 = byte being sent (shifted out), R1 = bit counter,
; R2 = scratch, R3 = byte being received. No ADD exists, so the frame
; loop counts DOWN with LOOP, keeps its count in data memory (CNT), and
; doubles as the table index: frame k sends PATTERNS[k], k = N..1.
;
; Bit timing (BAUD_CYCLES per bit; every instruction 3 cycles, DELAY N is
; 3 + N; a pin changes when its instruction commits):
;   data bit, edge to edge: OUTB DELAY SHIFT INB SHIFT DELAY LOOP
;                           = 3 + (3+H1) + 3 + 3 + 3 + (3+H2) + 3 = BAUD
;   RX sample (INB) lands 9 + H1 cycles after the edge -> mid-bit
;   start bit: SET, DELAY N_START, LDI, then the first OUTB = 9 + N_START
;   stop sample: SET, DELAY, INB = 6 + P1 -> mid-bit

.equ BAUD_CYCLES, 434
.equ TX, 0
.equ RX, 1
.equ N, 8                              ; patterns at PATTERNS[1..N]
.equ CNT, 511                          ; data-memory slot for the frame count

.equ H1, BAUD_CYCLES / 2 - 9
.equ H2, BAUD_CYCLES - 21 - H1
.equ N_START, BAUD_CYCLES - 9
.equ P1, BAUD_CYCLES / 2 - 6
.equ P2, BAUD_CYCLES / 2               ; rest of the stop bit before the next frame

        SET   TX, pp, 1                ; TX push-pull, idle high
        SET   RX, in, 0                ; RX input
        DELAY BAUD_CYCLES              ; idle line before the first frame
        LDI   R2, N

frame:  STORE R2, CNT
        LOADX R0, R2                   ; R0 = PATTERNS[k]
        LDI   R3, 0
        SET   TX, 0                    ; start bit
        DELAY N_START
        LDI   R1, 8
bit:    OUTB  R0, TX, 0                ; send bit (LSB first)
        DELAY H1
        SHIFT R3, R                    ; receive the same bit at mid-bit
        INB   R3, RX, 7
        SHIFT R0, R
        DELAY H2
        LOOP  bit, R1
        SET   TX, 1                    ; stop bit
        DELAY P1
        INB   R2, RX, 0                ; stop bit as received
        TEST  R2, 0
        BNE   fail                     ; stop bit read 0: framing error
        LOAD  R2, CNT
        LOADX R2, R2                   ; expected byte
        CMP   R3, R2
        BNE   fail
        DELAY P2
        LOAD  R2, CNT
        LOOP  frame, R2

        LDI   R0, 0xA5                 ; pass
        OUT   R0
        SET   HOST_STATUS, 1
        HALT

fail:   OUT   R3                       ; what actually came back
        SET   HOST_ERROR, 1
        HALT

; PATTERNS: data offset 0 is unused (k runs N..1); edges of every kind
; -- alternating bits, all-zero / all-one (no data edges), single bits
; at each end, nibble splits.
.data
.byte 0x00
.byte 0x55, 0xAA, 0x00, 0xFF, 0x01, 0x80, 0x3C, 0xC3
