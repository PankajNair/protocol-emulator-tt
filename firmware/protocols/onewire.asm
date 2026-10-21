; 1-Wire controller (standard speed), e.g. for a DS18B20 temperature
; sensor: reset/presence, write byte, read byte, host-driven.
;
; Not a protocol this chip was designed around (docs/architecture.md
; plans UART, SPI and I2C): one open-drain wire carries time slots of
; different lengths in both directions, with the device answering inside
; the controller's slot.
;
; Pin: DQ = 0, open-drain, needs an external pull-up (4.7 kOhm to 3.3 V):
; writing 1 releases the line, 0 pulls it low.
;
; Host protocol, lockstep like i2c.asm (IN = host sends a byte, OUT = host
; asks for one; docs/architecture.md Host handshake):
;   IN 1           reset; OUT presence: 0 = a device answered, 1 = none
;   IN 2, IN data  write the byte (LSB first); OUT 0 when done
;   IN 3           read a byte (LSB first); OUT the byte
; Any other command byte raises HOST_ERROR (sticky) and is ignored.
;
; Slot timing: Maxim AN126 recommended values, in 50 MHz cycles (a 3-cycle
; instruction is 60 ns, well inside every tolerance; DELAY N costs 3 + N).
;   write 1: low A (6 us), release B (64 us)
;   write 0: low C (60 us), release D (10 us)
;   read:    low A (6 us), release, sample after E (8 us; total ~14 us,
;            inside the 15 us the device holds its answer), wait F (55 us)
;   reset:   low H (>= 480 us), release, sample presence after I (70 us),
;            wait J (410 us) before the next slot

.equ DQ, 0
.equ OP_RESET, 1
.equ OP_WRITE, 2
.equ OP_READ,  3

        SET  DQ, od, 1             ; release the line (idle high)

main:   CALL getb                  ; R0 = command
        LDI  R1, OP_RESET
        CMP  R0, R1
        BEQ  reset
        LDI  R1, OP_WRITE
        CMP  R0, R1
        BEQ  write
        LDI  R1, OP_READ
        CMP  R0, R1
        BEQ  read
        SET  HOST_ERROR, 1         ; unknown command
        JMP  main

reset:  SET  DQ, 0
        DELAY 24, 2                ; H: 24576 cycles = 491.5 us
        SET  DQ, 1
        DELAY 109, 1               ; I: 3488 cycles = 69.8 us
        LDI  R0, 0
        INB  R0, DQ, 0             ; presence: a device pulls DQ low
        DELAY 21, 2                ; J: 21504 cycles = 430 us
        CALL putb
        JMP  main

write:  CALL getb                  ; R0 = data byte
        LDI  R2, 8
wbit:   TEST R0, 0                 ; next bit, LSB first
        BEQ  w1
        SET  DQ, 0                 ; write 0
        DELAY 94, 1                ; C: 3008 cycles = 60.2 us
        SET  DQ, 1
        DELAY 500                  ; D: 10 us
        JMP  wnext
w1:     SET  DQ, 0                 ; write 1
        DELAY 300                  ; A: 6 us
        SET  DQ, 1
        DELAY 100, 1               ; B: 3200 cycles = 64 us
wnext:  SHIFT R0, R
        LOOP wbit, R2
        LDI  R0, 0                 ; done
        CALL putb
        JMP  main

read:   LDI  R2, 8
rbit:   SET  DQ, 0
        DELAY 300                  ; A: 6 us
        SET  DQ, 1                 ; release: the device holds it low for a 0
        DELAY 400                  ; E: 8 us
        SHIFT R0, R
        INB  R0, DQ, 7             ; sample; after 8 shifts the first bit is bit 0
        DELAY 86, 1                ; F: 2752 cycles = 55 us
        LOOP rbit, R2
        CALL putb
        JMP  main

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
