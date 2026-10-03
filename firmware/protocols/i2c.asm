; I2C master (7-bit addressing): host-driven read and write transactions.
;
; Pins (docs/architecture.md Pin map): SDA = 0, SCL = 1, both open-drain
; (external pull-ups): writing 1 releases the line, 0 pulls it low.
;
; Host protocol -- lockstep, so HOST_STATUS never means two things:
;   1. IN  addr_rw          7-bit address << 1 | R/W (1 = read)
;   2. IN  len              number of data bytes, 1..255
;   3. firmware: START + address byte; OUT status (host-initiated)
;        0 = ACK, 1 = NACK, 3 = SCL stuck (clock-stretch timeout)
;      status != 0: firmware sends STOP and goes idle -- transaction over
;   4a. write: per byte: IN data; firmware sends it; OUT status (0/1/3);
;       status != 0 ends the transaction the same way
;   4b. read:  per byte: OUT data (host-initiated). The master ACKs every
;       byte but the last, which it NACKs, then sends STOP.
; A stretch timeout also raises HOST_ERROR (sticky): a slave holding SCL
; low forever can never hang the chip (docs/isa.md WAIT row).
;
; Timing, in 50 MHz cycles: SCL low time SCL_LOW, high time SCL_HIGH
; (defaults: 100 kHz standard mode, 5 us + 5 us; 400 kHz: -D SCL_LOW=70
; -D SCL_HIGH=55). Each instruction is 3 cycles (DELAY N: 3 + N), a pin
; changes when its instruction commits; each DELAY below is the phase
; length minus the fixed instruction cost around it (worked out per
; sequence in the comments). Clock stretching and host hand-offs only
; ever lengthen SCL low, which I2C allows.

.equ SCL_LOW,  250
.equ SCL_HIGH, 250
.equ SDA, 0
.equ SCL, 1
.equ T_STRETCH, 32768                 ; max SCL hold by a slave (~655 us)
.equ ADDR, 0                          ; data memory: saved addr_rw byte

; write bit: low = SHIFT LOOP OUTB DELAY SET = 15 + W_LOW
;            high = WAIT BEQ DELAY SET     = 12 + W_HIGH
.equ W_LOW,  SCL_LOW - 15
.equ W_HIGH, SCL_HIGH - 12
; read bit:  low = LOOP DELAY SET = 9 + R_LOW
;            high: sample (INB) at WAIT BEQ DELAY SHIFT INB = 15 + R_H1,
;            then DELAY SET -> total 21 + R_H1 + R_H2
.equ R_LOW,  SCL_LOW - 9
.equ R_H1,   SCL_HIGH / 2 - 15
.equ R_H2,   SCL_HIGH - 21 - R_H1
; ACK/NACK bit (clock_bit / ack read): low = (enter) OUTB DELAY SET,
;            high: sample at WAIT BEQ DELAY INB = 12 + A_H1,
;            then DELAY SET -> total 18 + A_H1 + A_H2
.equ A_LOW,  SCL_LOW - 15
.equ A_H1,   SCL_HIGH / 2 - 12
.equ A_H2,   SCL_HIGH - 18 - A_H1

        SET   SDA, od, 1              ; both lines released (idle)
        SET   SCL, od, 1

; ---------------------------------------------------------------- idle
idle:   WAIT  HOST_GO, 1              ; IN addr_rw
        IN    R0
        SET   HOST_STATUS, 1
        WAIT  HOST_GO, 0
        SET   HOST_STATUS, 0
        STORE R0, ADDR
        WAIT  HOST_GO, 1              ; IN len
        IN    R2
        SET   HOST_STATUS, 1
        WAIT  HOST_GO, 0
        SET   HOST_STATUS, 0

        SET   SDA, 0                  ; START: SDA falls while SCL high
        DELAY SCL_HIGH                ; hold time
        SET   SCL, 0
        CALL  send_byte               ; address byte (R0); R3 = status
        CALL  report                  ; OUT R3 to the host
        LDI   R1, 0
        CMP   R3, R1
        BNE   abort
        LOAD  R0, ADDR
        TEST  R0, 0
        BEQ   rd

; ---------------------------------------------------------------- write
wr:     WAIT  HOST_GO, 1              ; IN data byte
        IN    R0
        SET   HOST_STATUS, 1
        WAIT  HOST_GO, 0
        SET   HOST_STATUS, 0
        CALL  send_byte
        CALL  report
        LDI   R1, 0
        CMP   R3, R1
        BNE   abort
        LOOP  wr, R2
        JMP   finish

; ---------------------------------------------------------------- read
rd:     CALL  read_byte               ; R0 = byte, R3 = 0 or 3
        WAIT  HOST_GO, 1              ; OUT data byte (host-initiated)
        OUT   R0
        SET   HOST_STATUS, 1
        WAIT  HOST_GO, 0
        SET   HOST_STATUS, 0
        LDI   R1, 0
        CMP   R3, R1
        BNE   abort
        LDI   R3, 0                   ; ACK ...
        LOOP  more, R2                ; ... if more bytes follow
        LDI   R3, 1                   ; last byte: NACK
        CALL  clock_bit
        JMP   finish
more:   CALL  clock_bit
        JMP   rd

finish:
abort:  SET   SCL, 0                  ; STOP: SDA low while SCL low ...
        SET   SDA, 0
        DELAY SCL_LOW
        SET   SCL, 1
        WAIT  SCL, 1, T_STRETCH
        DELAY SCL_HIGH                ; setup time for STOP
        SET   SDA, 1                  ; ... then SDA rises while SCL high
        DELAY SCL_LOW                 ; bus free time before the next START
        JMP   idle

; ---------------------------------------------------------------- subroutines
; send_byte: R0 out MSB first, then read the slave's ACK.
;   out: R3 = 0 ACK, 1 NACK, 3 SCL stuck.  uses R1.
send_byte:
        LDI   R3, 0
        LDI   R1, 8
sb:     OUTB  R0, SDA, 7              ; data while SCL low
        DELAY W_LOW
        SET   SCL, 1
        WAIT  SCL, 1, T_STRETCH       ; slave may stretch
        BEQ   stuck
        DELAY W_HIGH
        SET   SCL, 0
        SHIFT R0, L
        LOOP  sb, R1
        SET   SDA, 1                  ; release SDA for the ACK bit
        DELAY A_LOW
        SET   SCL, 1
        WAIT  SCL, 1, T_STRETCH
        BEQ   stuck
        DELAY A_H1
        INB   R3, SDA, 0              ; 0 = ACK, 1 = NACK
        DELAY A_H2
        SET   SCL, 0
        RET

; read_byte: 8 bits into R0, MSB first (ACK/NACK sent by clock_bit).
;   out: R0 = byte, R3 = 0 or 3 (stuck).  uses R1.
read_byte:
        LDI   R3, 0
        LDI   R0, 0
        SET   SDA, 1                  ; release SDA: slave drives
        LDI   R1, 8
rb:     DELAY R_LOW
        SET   SCL, 1
        WAIT  SCL, 1, T_STRETCH
        BEQ   stuck
        DELAY R_H1
        SHIFT R0, L
        INB   R0, SDA, 0              ; sample mid-high
        DELAY R_H2
        SET   SCL, 0
        LOOP  rb, R1
        RET

; clock_bit: drive R3[0] on SDA for one SCL clock (master ACK = 0 /
; NACK = 1), then release SDA.  out: R3 = 3 if stuck.
clock_bit:
        OUTB  R3, SDA, 0
        DELAY A_LOW
        SET   SCL, 1
        WAIT  SCL, 1, T_STRETCH
        BEQ   stuck
        DELAY A_H1
        DELAY A_H2
        SET   SCL, 0
        SET   SDA, 1
        RET

; stuck: SCL never went high within T_STRETCH. Release both lines, flag
; HOST_ERROR, return status 3 to whichever caller was clocking.
stuck:  SET   SDA, 1
        SET   SCL, 1
        SET   HOST_ERROR, 1
        LDI   R3, 3
        RET

; report: OUT R3 to the host (host-initiated OUT handshake).
report: WAIT  HOST_GO, 1
        OUT   R3
        SET   HOST_STATUS, 1
        WAIT  HOST_GO, 0
        SET   HOST_STATUS, 0
        RET
