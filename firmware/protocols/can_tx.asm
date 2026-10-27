; CAN 2.0A transmitter, 50 kbit/s: standard data frame (11-bit ID,
; 0-8 data bytes) with bit stuffing, CRC-15, ACK check, arbitration and
; bit-error detection. TX = pin 0 to a CAN transceiver's TXD, RX = pin 1
; from its RXD (the transceiver loops the bus back: RX is the bus level).
;
; Not a protocol this chip was designed around. It needs XOR/ADD (CRC-15,
; 16-bit shifts with carry, counters) and exact bit timing while the
; per-bit work branches -- every branch below is balanced (the shorter arm
; is padded with a DELAY of exactly the missing cycles), so every bit is
; exactly BIT cycles whichever path runs. test/test_can.py measures every
; edge.
;
; Host protocol (docs/architecture.md Host handshake): IN id_hi (bits
; 10-8 of the ID), IN id_lo (bits 7-0), IN dlc (0-8), then IN 8 data bytes
; (unused ones are ignored); the firmware waits for 11 recessive bits of
; bus idle, sends the frame, and answers OUT status:
;   0 = sent and acknowledged    1 = no ACK (ACK slot stayed recessive)
;   2 = lost arbitration         3 = bit error (read back != sent)
; On anything but 0 it releases TX (recessive) at once.
;
; Frame, as a table of fields walked with LOADX (there is no indexed
; STORE, so the table is preloaded from the boot image's data region and
; the host's bytes go to fixed slots). Each field's value is MSB-aligned:
;   0 SOF(1)  1 ID[10:8](3)  2 ID[7:0](8)  3 RTR(1)  4 IDE,r0(2)  5 DLC(4)
;   6-13 data bytes(8)  -- after field 5+dlc the walk jumps to 14 --
;   14 CRC[14:7](8)  15 CRC[6:0](7)  16 CRC delimiter(1)  17 ACK slot(1)
;   18 ACK delimiter + EOF 1-6(7)  19 EOF 7(1)
; Stuffing covers fields 0-15 (including a stuff bit right after the CRC);
; the CRC covers fields 0-13. Field 17 is sent recessive and must be read
; back dominant (another node's ACK).
;
; CRC-15 (poly 0x4599) is kept left-aligned in CRCH:CRCL (c14..c7 : c6..c0,0)
; so a 16-bit shift drops c14 by itself; XOR with 0x8B32 = 0x4599 << 1.
;
; Bit timing, BIT = 1000 cycles (50 kbit/s, a standard CAN rate), sample
; point 75%. Per bit: drive the bit prepared last time, copy its
; attributes, part 1 (prepare the next bit), part 2 (its bookkeeping: CRC,
; stuffing run, field walk), wait to the sample point, sample and compare,
; wait out the bit. Parts 1 and 2 take a fixed P1 and P2 cycles whatever
; path runs. Their 381 cycles are why the rate is 50 and not 125 kbit/s:
; at 400 cycles per bit they don't fit before a 75% sample point.

.equ TX, 0
.equ RX, 1
.equ BIT, 1000

; data region layout
.equ FV, 16        ; field values (fields 0-20)
.equ FN, 48        ; field lengths in bits (0 = sentinel)
.equ FX, 80        ; 1 = expect the inverse of what is sent (ACK slot)
.equ PTR, 112      ; current field
.equ CUR, 113      ; current field's remaining bits, MSB first
.equ NB, 114       ; bits left in the current field
.equ RUN, 115      ; identical bits in a row (stuffing)
.equ LAST, 116     ; last bit sent (stuffing)
.equ CRCH, 117
.equ CRCL, 118
.equ LIM, 119      ; 6 + dlc: first field after the data
.equ NEXTB, 120    ; next bit to send
.equ NEXP, 121     ; level expected back for it
.equ NARB, 122     ; 1 if it is in the arbitration field
.equ NFIN, 123     ; 1 if it is the last bit of the frame
.equ ISSTF, 124    ; 1 if it is a stuff bit
.equ CURB, 125     ; the same four, for the bit on the bus now
.equ CEXP, 126
.equ CARB, 127
.equ CFIN, 128

; cycle budget (every block below states its own length)
.equ P1, 111                      ; part 1, CALL to RET inclusive
.equ P2, 231                      ; part 2, CALL to RET inclusive
.equ DRIVE, 15 + 24               ; drive (5 instr) + copy attributes (8 instr)
.equ EDGE, 12                     ; TX changes on the drive's 4th instruction
; sample at 75% after the edge: the INB commits DRIVE + P1 + P2 + (3 +
; SPD) + 6 cycles into the bit, reading the pin as it was 2 cycles earlier
; (input synchronizer)
.equ SPD, EDGE + BIT * 3 / 4 - DRIVE - P1 - P2 - 3 - 6 + 2
; rest: sample/compare (5 instr) + final check (3) + DELAY + JMP back
.equ ENDD, BIT - DRIVE - P1 - P2 - (3 + SPD) - 15 - 9 - 3 - 3

        SET  TX, pp, 1            ; recessive
        SET  RX, in, 0

main:   CALL getb                 ; ID[10:8] -> field 1, MSB-aligned
        SHIFT R0, L
        SHIFT R0, L
        SHIFT R0, L
        SHIFT R0, L
        SHIFT R0, L
        STORE R0, FV+1
        CALL getb                 ; ID[7:0] -> field 2
        STORE R0, FV+2
        CALL getb                 ; DLC -> field 5, LIM = 6 + dlc
        LDI  R1, 6
        ADD  R1, R0
        STORE R1, LIM
        SHIFT R0, L
        SHIFT R0, L
        SHIFT R0, L
        SHIFT R0, L
        STORE R0, FV+5
        CALL getb                 ; 8 data bytes -> fields 6-13
        STORE R0, FV+6
        CALL getb
        STORE R0, FV+7
        CALL getb
        STORE R0, FV+8
        CALL getb
        STORE R0, FV+9
        CALL getb
        STORE R0, FV+10
        CALL getb
        STORE R0, FV+11
        CALL getb
        STORE R0, FV+12
        CALL getb
        STORE R0, FV+13

        LDI  R0, 0                ; walk starts at field 0 (SOF: 1 bit, 0)
        STORE R0, PTR
        STORE R0, CUR
        STORE R0, RUN
        STORE R0, CRCH
        STORE R0, CRCL
        LDI  R0, 1
        STORE R0, NB
        STORE R0, LAST            ; bus idle is recessive

idle:   LDI  R2, 11               ; 11 recessive bit times of bus idle
idlebit: DELAY 30, 1              ; ~1 bit (960 + the loop's own cycles)
        LDI  R1, 0
        INB  R1, RX, 0
        TEST R1, 0
        BNE  idle                 ; dominant: someone is talking, start over
        LOOP idlebit, R2

        CALL part1                ; prepare SOF
        CALL part2

; ------------------------------------------------------------------ bit loop
bitloop: LOAD R0, NEXTB           ; drive: 5 instructions on both paths,
        TEST R0, 0                ; the pin changes on the 4th
        BEQ  drv1
        SET  TX, 0
        JMP  drvd
drv1:   SET  TX, 1
        NOP
drvd:   LOAD R0, NEXP             ; this bit's attributes (8 instructions)
        STORE R0, CEXP
        LOAD R0, NARB
        STORE R0, CARB
        LOAD R0, NFIN
        STORE R0, CFIN
        LOAD R0, NEXTB
        STORE R0, CURB
        CALL part1                ; next bit: P1 cycles
        CALL part2                ; its bookkeeping: P2 cycles
        DELAY SPD
        LDI  R1, 0                ; sample (75%) and compare
        INB  R1, RX, 0
        LOAD R0, CEXP
        CMP  R0, R1
        BNE  mism
        LOAD R0, CFIN             ; last bit of the frame?
        TEST R0, 0
        BEQ  sent
        DELAY ENDD
        JMP  bitloop

sent:   LDI  R0, 0
        JMP  report
mism:   SET  TX, 1                ; release the bus at once
        LOAD R0, CEXP             ; expected dominant (ACK slot) -> no ACK
        TEST R0, 0
        BNE  noack
        LOAD R0, CARB             ; sent recessive, read dominant
        TEST R0, 0
        BNE  biterr
        LDI  R0, 2                ;   in the arbitration field: lost
        JMP  report
noack:  LDI  R0, 1
        JMP  report
biterr: LDI  R0, 3
report: CALL putb
        JMP  main

; ------------------------------------------------------------------ part 1
; Prepares the next bit: NEXTB, NEXP, NARB, NFIN, ISSTF. P1 cycles.
part1:  LOAD R2, PTR              ; NARB = PTR < 4 (SOF, ID, RTR)   [18 cyc]
        LDI  R1, 252
        ADD  R1, R2               ; flag = PTR >= 4
        LDI  R1, 1
        BNE  arb1                 ; [9 cyc both paths]
        LDI  R1, 0
        JMP  arbd
arb1:   DELAY 3
arbd:   STORE R1, NARB            ; [3]
        LOAD R0, RUN              ; stuff bit due?                 [12]
        LDI  R1, 5
        CMP  R0, R1
        BEQ  p1stf
        ; -- field bit -- (from here to p1done: 69 cycles incl. JMP)
        LOAD R0, CUR              ; b = MSB of CUR, shift CUR       [12]
        LDI  R3, 0
        ADD  R0, R0               ; flag = b
        STORE R0, CUR
        BNE  b0                   ; [9 both paths]
        LDI  R3, 1
        JMP  bd
b0:     DELAY 3
bd:     STORE R3, NEXTB           ; [3]
        LDI  R1, FX               ; NEXP = b ^ FX[PTR]               [18]
        ADD  R1, R2
        LOADX R1, R1
        XOR  R1, R3
        STORE R1, NEXP
        LDI  R0, 0
        LDI  R1, 19               ; NFIN = PTR == 19                 [6]
        CMP  R2, R1
        BNE  f0                   ; [9 both paths]
        LDI  R0, 1
        JMP  fd
f0:     DELAY 3
fd:     STORE R0, NFIN            ; [3]
        LDI  R0, 0                ; [6]
        STORE R0, ISSTF
        JMP  p1done               ; [3]
        ; -- stuff bit: the inverse of the last bit -- (69 cycles to p1done)
p1stf:  LOAD R0, LAST             ; [24]
        LDI  R1, 1
        XOR  R0, R1
        STORE R0, NEXTB
        STORE R0, NEXP
        STORE R1, ISSTF
        LDI  R1, 0
        STORE R1, NFIN
        DELAY 42                  ; [45]: 24 + 45 = 69, the field path
p1done: RET                       ; [3]

; ------------------------------------------------------------------ part 2
; Bookkeeping for the bit part 1 prepared: stuffing run, CRC, field walk.
; P2 cycles.
part2:  LOAD R0, ISSTF            ; [9]
        TEST R0, 0
        BEQ  s2stf
        ; -- field bit -- (to p2done: P2 - 3 (CALL) - 9 - 3 (RET) cycles)
        LOAD R3, NEXTB            ; [6]
        LOAD R2, PTR
        LDI  R1, 242              ; CRC over fields 0-13              [9]
        ADD  R1, R2               ; flag = PTR >= 14
        BEQ  crcno
        LOAD R0, CRCH             ; ---- CRC block: 69 cycles ----
        LOAD R1, CRCL
        LDI  R2, 0                ; R2 = c14
        TEST R0, 7
        BNE  c0                   ; [9 both paths]
        LDI  R2, 1
        JMP  c1
c0:     DELAY 3
c1:     XOR  R2, R3               ; R2 = c14 ^ b
        ADD  R0, R0               ; 16-bit shift left
        ADD  R1, R1               ; flag = carry out of the low byte
        BNE  k0                   ; [12 both paths]
        LDI  R3, 1
        ADD  R0, R3
        JMP  k1
k0:     DELAY 6
k1:     TEST R2, 0
        BNE  x0                   ; [18 both paths]
        LDI  R3, 0x8B
        XOR  R0, R3
        LDI  R3, 0x32
        XOR  R1, R3
        JMP  x1
x0:     DELAY 12
x1:     STORE R0, CRCH
        STORE R1, CRCL
        JMP  crcd                 ; [3]
crcno:  DELAY 69                  ; [72] = block + JMP
crcd:   LOAD R3, NEXTB            ; stuffing run over fields 0-15     [15]
        LOAD R2, PTR
        LDI  R1, 240
        ADD  R1, R2               ; flag = PTR >= 16
        BEQ  runno
        LOAD R0, LAST             ; [6]
        CMP  R0, R3
        BEQ  same                 ; [15 both paths]
        LDI  R1, 1
        STORE R1, RUN
        DELAY 0
        JMP  rund
same:   LOAD R1, RUN
        LDI  R0, 1
        ADD  R1, R0
        STORE R1, RUN
rund:   STORE R3, LAST            ; [3]
        JMP  runout               ; [3]
runno:  LDI  R1, 0                ; [27] = 9 + 12 + 3 + 3
        STORE R1, RUN
        DELAY 18
runout: LOAD R0, NB               ; NB--, advance on 0              [21]
        LDI  R1, 255
        ADD  R0, R1
        STORE R0, NB
        LDI  R1, 0
        CMP  R0, R1
        BNE  advno
        LDI  R1, 1                ; ---- advance: 63 cycles ----
        ADD  R2, R1               ; next field
        LOAD R3, LIM
        CMP  R2, R3
        BNE  nojump               ; [21 both paths]
        LDI  R2, 14               ; end of data: on to the CRC fields,
        LOAD R0, CRCH             ; which hold the CRC as it stands now
        STORE R0, FV+14
        LOAD R0, CRCL
        STORE R0, FV+15
        JMP  jd
nojump: DELAY 15
jd:     STORE R2, PTR             ; load the field's value and length
        LDI  R1, FV
        ADD  R1, R2
        LOADX R0, R1
        STORE R0, CUR
        LDI  R1, FN
        ADD  R1, R2
        LOADX R0, R1
        STORE R0, NB
        JMP  advd                 ; [3]
advno:  DELAY 60                  ; [63]
advd:   JMP  p2done               ; [3]
        ; -- stuff bit: run restarts at 1, LAST = the stuff bit --
s2stf:  LDI  R1, 1                ; [12] ... padded to the field path
        STORE R1, RUN
        LOAD R0, NEXTB
        STORE R0, LAST
        DELAY S2PAD
p2done: RET

; field path of part 2, from after the ISSTF test to p2done:
;   6 + 9 + 72 + 15 + 27 + 21 + 63 + 3 = 216; the stuff path matches it
.equ S2PAD, 216 - 12 - 3

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

.data
        .byte 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
        ; FV (16-36): SOF, ID hi, ID lo, RTR, IDE+r0, DLC, data x8, CRC x2,
        ; CRC delim, ACK, ACK delim + EOF1-6, EOF7, sentinel
        .byte 0x00, 0, 0, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
        .byte 0x80, 0x80, 0xFE, 0x80, 0xFF
        .byte 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
        ; FN (48-68)
        .byte 1, 3, 8, 1, 2, 4, 8, 8, 8, 8, 8, 8, 8, 8, 8, 7
        .byte 1, 1, 7, 1, 0
        .byte 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
        ; FX (80-100): only the ACK slot (field 17) expects the inverse
        .byte 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0
