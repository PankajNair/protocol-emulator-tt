; NEC infrared remote-control transmitter: the host gives an address and
; a command byte, the firmware sends one NEC frame on pin 0, modulated
; on a 38 kHz carrier (drive an IR LED through a transistor).
;
; Not a protocol this chip was designed around: a carrier generated in
; firmware, keyed by a pulse-distance code with millisecond spacing.
;
; NEC frame: 9 ms carrier burst, 4.5 ms space, then 32 bits LSB first --
; address, ~address, command, ~command -- each a 562.5 us burst followed
; by a space of 562.5 us (0) or 1687.5 us (1), and a final 562.5 us burst.
; There is no NOT instruction, so the complement bytes go through `sendn`,
; a copy of `send` with the branch sense flipped.
;
; Host protocol (docs/architecture.md Host handshake): IN address, IN
; command; firmware sends the frame, then OUT 0 (host-initiated) when done.
;
; Carrier, in 50 MHz cycles (each instruction 3, DELAY N 3 + N, a pin
; changes when its instruction commits):
;   high = DELAY 433 + SET          = 439
;   low  = DELAY 27,1 + LOOP + SET  = 867 + 3 + 3 = 873
;   period 1312 cycles = 38.11 kHz, duty 33.5 %
; Bursts: 22 periods per bit mark (~560 us), 343 for the leader (~9 ms,
; in two loops: a LOOP count is at most 256). Spaces: the DELAY below plus
; the instructions between the last falling and the next rising edge.

.equ IR, 0
.equ ADDR, 0                       ; data memory: address byte
.equ CMD, 1                        ; data memory: command byte
.equ BIT_PERIODS, 22
.equ N_LEAD1, 172
.equ N_LEAD2, 171

        SET  IR, pp, 0             ; LED off

main:   CALL getb
        STORE R0, ADDR
        CALL getb
        STORE R0, CMD

        LDI  R1, N_LEAD1            ; leader burst, part 1
lead1:  SET  IR, 1
        DELAY 433
        SET  IR, 0
        DELAY 27, 1
        LOOP lead1, R1
        LDI  R1, N_LEAD2            ; part 2
lead2:  SET  IR, 1
        DELAY 433
        SET  IR, 0
        DELAY 27, 1
        LOOP lead2, R1
        DELAY 219, 2               ; leader space: 224256 cycles

        LOAD R0, ADDR
        CALL send
        LOAD R0, ADDR
        CALL sendn
        LOAD R0, CMD
        CALL send
        LOAD R0, CMD
        CALL sendn

        LDI  R1, BIT_PERIODS       ; final burst
stop:   SET  IR, 1
        DELAY 433
        SET  IR, 0
        DELAY 27, 1
        LOOP stop, R1

        LDI  R0, 0                 ; frame sent
        CALL putb
        JMP  main

; send R0, LSB first: per bit a burst, then a short (0) or long (1) space
send:   LDI  R2, 8
sbit:   LDI  R1, BIT_PERIODS
scar:   SET  IR, 1
        DELAY 433
        SET  IR, 0
        DELAY 27, 1
        LOOP scar, R1
        TEST R0, 0
        BEQ  sone
        DELAY 26, 2                ; 0: short space
        JMP  snext
sone:   DELAY 81, 2                ; 1: long space
snext:  SHIFT R0, R
        LOOP sbit, R2
        RET

; send ~R0: same, with the bit test inverted
sendn:  LDI  R2, 8
nbit:   LDI  R1, BIT_PERIODS
ncar:   SET  IR, 1
        DELAY 433
        SET  IR, 0
        DELAY 27, 1
        LOOP ncar, R1
        TEST R0, 0
        BNE  none                  ; bit 0 -> sends a 1
        DELAY 26, 2
        JMP  nnext
none:   DELAY 81, 2
nnext:  SHIFT R0, R
        LOOP nbit, R2
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
