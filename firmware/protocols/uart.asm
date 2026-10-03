; UART 8N1 transmitter: bytes from the host go out on the TX pin.
;
; Host side: the IN handshake from docs/architecture.md (host drives
; ui_in and raises HOST_GO; firmware captures, acks on HOST_STATUS; host
; drops HOST_GO; firmware clears HOST_STATUS). The byte is then sent LSB
; first: start bit (0), 8 data bits, stop bit (1). Line idles high.
;
; Bit timing. Every instruction costs 3 cycles (FETCH_LO, FETCH_HI,
; EXECUTE) and a pin changes when its instruction commits, so the time
; between two pin edges is the cycle cost of every instruction after the
; first edge, up to and including the one making the next edge. DELAY N
; costs 3 + N. Set BAUD_CYCLES = clock / baud (50 MHz: 115200 -> 434,
; 9600 -> 5208); override with  asm.py uart.asm -D BAUD_CYCLES=5208.

.equ BAUD_CYCLES, 434
.equ TX, 0                         ; pin 0 = UART TX (docs/architecture.md Pin map)

; start bit: DELAY + LDI + the first OUTB      = 3+N + 3 + 3 = 9 + N
.equ N_START, BAUD_CYCLES - 9
; data bit:  DELAY + SHIFT + LOOP + next OUTB  = 3+N + 3 + 3 + 3 = 12 + N
;            (last data bit -> stop bit: same, with SET in place of OUTB)
.equ N_DATA,  BAUD_CYCLES - 12
; stop bit: held at least one bit time (DELAY + JMP), longer while the
; host hands over the next byte
.equ N_STOP,  BAUD_CYCLES - 6

        SET  TX, pp, 1             ; configure push-pull, idle high

next:   WAIT HOST_GO, 1            ; host has a byte for us
        IN   R0
        SET  HOST_STATUS, 1        ; got it
        WAIT HOST_GO, 0
        SET  HOST_STATUS, 0        ; handshake done

        SET  TX, 0                 ; start bit
        DELAY N_START
        LDI  R1, 8
bit:    OUTB R0, TX, 0             ; drive R0[0] (LSB first) ...
        DELAY N_DATA
        SHIFT R0, R                ; ... then shift it out
        LOOP bit, R1
        SET  TX, 1                 ; stop bit
        DELAY N_STOP
        JMP  next
