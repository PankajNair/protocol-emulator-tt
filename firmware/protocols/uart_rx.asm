; UART 8N1 receiver: frames arriving on the RX pin go to the host.
;
; Each byte is sampled LSB first at the centre of every bit, the stop bit
; is checked, and the byte is handed over with the host-initiated OUT
; handshake (docs/architecture.md): host raises HOST_GO to request a
; byte, firmware drives uo_out and raises HOST_STATUS, host reads and
; drops HOST_GO, firmware clears HOST_STATUS. A stop bit sampled low
; (framing error) raises HOST_ERROR, sticky until reset; the byte is
; still delivered.
;
; Timing. Instructions cost 3 cycles (DELAY N: 3 + N). WAIT detects the
; start edge and INB samples the pin through the same 2-flop
; synchronizer, so that latency cancels out of the sample-point
; arithmetic below; offsets are measured from WAIT's exit.
;   first sample: DELAY+LDI+LDI + DELAY+SHIFT+INB = 18 + N_HALF + N_BIT
;   each next:    LOOP + DELAY+SHIFT+INB          = 12 + N_BIT
; so N_BIT = BAUD - 12 gives one sample per bit, and N_HALF = BAUD/2 - 6
; puts the first one at 1.5 bits (centre of data bit 0).
;
; Real-time limit: there is no receive buffer. Between the stop-bit
; sample (9.5 bits) and the next start edge (10 bits, back-to-back
; frames) the host must complete the hand-over -- about 0.4 bit, ~180
; cycles at 115200. A host that keeps its next request (HOST_GO) raised
; ahead of time meets this easily; a slower one needs gaps between
; frames.

.equ BAUD_CYCLES, 434
.equ RX, 1                         ; pin 1 = UART RX (docs/architecture.md Pin map)

.equ N_HALF, BAUD_CYCLES / 2 - 6
.equ N_BIT,  BAUD_CYCLES - 12
; stop-bit centre: LOOP + DELAY + INB after the last data sample = 9 + N
.equ N_STOP, BAUD_CYCLES - 9

        SET  RX, in, 0             ; input (also the reset state)

next:   WAIT RX, 1                 ; line idle first: after a framing error or break
                                   ; it can still be low, which would misframe
        WAIT RX, 0                 ; start bit's falling edge
        DELAY N_HALF
        LDI  R1, 8
        LDI  R0, 0
bit:    DELAY N_BIT
        SHIFT R0, R                ; make room at bit 7 ...
        INB  R0, RX, 7             ; ... and capture there (LSB first)
        LOOP bit, R1

        DELAY N_STOP               ; centre of the stop bit
        INB  R2, RX, 0
        TEST R2, 0
        BEQ  good
        SET  HOST_ERROR, 1         ; framing error

good:   WAIT HOST_GO, 1            ; host's request for the byte
        OUT  R0
        SET  HOST_STATUS, 1        ; data valid
        WAIT HOST_GO, 0            ; host has read it
        SET  HOST_STATUS, 0
        JMP  next
