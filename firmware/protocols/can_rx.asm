; CAN 2.0A receiver, 50 kbit/s: receives standard data frames from the
; bus, checks stuffing, CRC-15 and frame form, ACKs good frames, and hands
; each one to the host. TX = pin 0 to a CAN transceiver's TXD (used only
; to drive ACK), RX = pin 1 from its RXD.
;
; Bit timing follows the transmitter, as CAN requires: hard sync on SOF,
; then resync on every recessive-to-dominant edge -- after a recessive
; bit, WAIT RX,0 with a timeout looks for the next edge; if one comes, the
; next sample is timed from it, else the nominal grid continues. Sample
; point 75%. A transmitter whose clock is off by a percent is still read
; correctly (test/test_can_rx.py runs them +-1% off).
;
; Host protocol: after each frame the host asks (OUT handshake) for a
; status byte: 0 = good frame, 1 = stuff error, 2 = CRC error, 3 = form
; error (CRC delimiter, ACK delimiter or EOF dominant). For 0 it then asks
; for ID[10:8], ID[7:0], DLC and the data bytes. A good frame is ACKed on
; the bus; a bad one is not. Frames arriving while the host reads are
; missed (no buffer).
;
; No indexed STORE, so received bytes go through a shift FIFO: each
; completed byte (ID hi, ID lo, DLC, data) moves slots 1-10 down one and
; lands in slot 10; the frame is then the last 3 + DLC slots, read back
; with LOADX.
;
; Fields (as in can_tx.asm): 0 SOF(1) 1 ID[10:8](3) 2 ID[7:0](8) 3 RTR(1)
; 4 IDE,r0(2) 5 DLC(4) 6-13 data(8) [after field 5+dlc -> 14] 14 CRC hi(8)
; 15 CRC lo(7) 16 CRC delimiter(1) 17 ACK slot(1) 18 ACK delim + EOF(8).
; Destuffing covers fields 0-15 (and the stuff bit after the CRC), CRC
; is run over fields 0-15: data and received CRC together leave a zero
; remainder exactly when the CRC is right. Field kinds, acted on when a
; field completes:
;   0 nothing  1 push to the FIFO  5 CRC delimiter (must be recessive;
;   the CRC, run over the received CRC bits too, must leave 0; ACK if all
;   good)  6 end of frame (ACK delimiter + EOF recessive)

.equ TX, 0
.equ RX, 1

; data region layout
.equ FN, 16        ; field lengths 0-18
.equ FK, 40        ; field kinds 0-18
.equ FIFO, 64      ; slots 0-10 (10 = newest)
.equ PTR, 96
.equ NB, 97
.equ ACC, 98       ; current field's bits, newest in bit 0
.equ RUN, 99
.equ LAST, 100
.equ CRCH, 101
.equ CRCL, 102
.equ LIM, 103
.equ STAT, 104     ; 0 ok, 1 stuff, 2 CRC, 3 form
.equ DONE, 105
.equ B, 106        ; the bit just sampled
.equ DLC, 107

; ---- timing, cycles. c = commit of the sampling INB (it reads the pin as
; it was at c - 2). PCYC = CALL proc .. RET, the same on every path.
.equ PCYC, 462
; dominant bit: TX released at the next bit boundary, c + 248 (an ACK we
; drove ends there; otherwise TX is already high), proc, next sample at
; c + 1000
.equ D1, 248 - 15
.equ D2, 1000 - 248 - PCYC - 24
; recessive bit, edge found: WAIT exits at e + 3; next INB at e + 752
.equ D3, 752 - 3 - 3 - PCYC - 27
; recessive bit, no edge: the WAIT (timeout 352) ends at c + 364
.equ D4, 1000 - 364 - 3 - PCYC - 21
; SOF: WAIT exits at e + 3; first INB at e + 752 (two DELAYs: max 511 each)
.equ SOFD, 752 - 3 - 403 - 3 - 6

        SET  TX, pp, 1            ; RX (pin 1) is an input from reset

start:  LDI  R0, 0
        STORE R0, PTR
        STORE R0, ACC
        STORE R0, RUN
        STORE R0, CRCH
        STORE R0, CRCL
        STORE R0, STAT
        STORE R0, DONE
        LDI  R0, 1
        STORE R0, NB              ; field 0 (SOF) is 1 bit
        STORE R0, LAST            ; bus idle is recessive
        LDI  R0, 255
        STORE R0, LIM             ; set when the DLC field ends

idle:   WAIT RX, 0, 11264         ; 11 recessive bits (11264 cycles) of
        BNE  idle                 ; idle; a dominant bit first: retry
        WAIT RX, 0                ; SOF: hard sync
        DELAY 400
        DELAY SOFD

; ------------------------------------------------------------------ bit loop
sample: LDI  R1, 0
        INB  R1, RX, 0
        STORE R1, B
        TEST R1, 0
        BEQ  rec
        DELAY D1                  ; dominant bit
        SET  TX, 1                ; next boundary: end of an ACK we drove
        CALL proc
        DELAY D2
        JMP  next
rec:    WAIT RX, 0, 352           ; recessive: look for the next edge
        BEQ  tmo                  ; flag = timed out
        CALL proc                 ; edge: resync on it
        DELAY D3
        JMP  next
tmo:    CALL proc
        DELAY D4
next:   LOAD R0, DONE
        TEST R0, 0
        BEQ  report
        JMP  sample

report: LOAD R0, STAT
        CALL putb                 ; (leaves R0)
        LDI  R1, 0
        CMP  R0, R1
        BNE  start                ; bad frame: status only
        LOAD R2, DLC              ; good: 3 + dlc bytes from the FIFO
        LDI  R1, 3
        ADD  R2, R1
        LDI  R1, 255              ; R3 = FIFO + 11 - R2
        XOR  R1, R2
        LDI  R0, 1
        ADD  R1, R0
        LDI  R3, FIFO + 11
        ADD  R3, R1
outb:   LOADX R0, R3
        CALL putb
        LDI  R0, 1
        ADD  R3, R0
        LOOP outb, R2
        JMP  start

; ------------------------------------------------------------------ proc
; Processes bit B in exactly PCYC cycles (every branch padded; block
; lengths in brackets). Sets STAT, and DONE at the end of the frame or on
; a stuff error.
proc:   LOAD R3, B                ; [3]
        LOAD R0, RUN              ; stuff bit due?                   [12]
        LDI  R1, 5
        CMP  R0, R1
        BEQ  stf
        ; -- data bit: 441 cycles to the RET --
        LOAD R0, LAST             ; stuffing run                     [24]
        CMP  R0, R3
        BEQ  same
        LDI  R1, 1
        STORE R1, RUN
        DELAY 0
        JMP  rund
same:   LOAD R1, RUN
        LDI  R0, 1
        ADD  R1, R0
        STORE R1, RUN
rund:   STORE R3, LAST
        LOAD R2, PTR              ; fields 0-15: CRC; 16+: no stuffing  [84]
        LDI  R1, 240
        ADD  R1, R2               ; flag = PTR >= 16
        BEQ  hi16
        LOAD R0, CRCH             ; ---- CRC block, 69 cycles: as can_tx.asm
        LOAD R1, CRCL
        LDI  R2, 0
        TEST R0, 7
        BNE  c0
        LDI  R2, 1
        JMP  c1
c0:     DELAY 3
c1:     XOR  R2, R3
        ADD  R0, R0
        ADD  R1, R1
        BNE  k0
        LDI  R3, 1                ; (R2 holds c14 ^ b; R3 is reloaded below)
        ADD  R0, R3
        JMP  k1
k0:     DELAY 6
k1:     TEST R2, 0
        BNE  x0
        LDI  R2, 0x8B
        XOR  R0, R2
        LDI  R2, 0x32
        XOR  R1, R2
        JMP  x1
x0:     DELAY 12
x1:     STORE R0, CRCH
        STORE R1, CRCL
        JMP  crcd
hi16:   LDI  R1, 0
        STORE R1, RUN
        DELAY 63
crcd:   LOAD R3, B                ; ACC = ACC << 1 | b                 [15]
        LOAD R0, ACC
        ADD  R0, R0
        ADD  R0, R3
        STORE R0, ACC
        LOAD R0, NB               ; NB--, field end on 0                [21]
        LDI  R1, 255
        ADD  R0, R1
        STORE R0, NB
        LDI  R1, 0
        CMP  R0, R1
        BNE  noend
        ; ---- field end: 294 cycles ----
        LOAD R2, PTR              ; kind                                [15]
        LDI  R1, FK
        ADD  R1, R2
        LOADX R1, R1
        LOAD R0, ACC
        LDI  R2, 1                ; kind 1: push ACC to the FIFO        [75]
        CMP  R1, R2
        BNE  nk1
        LOAD R3, FIFO+1
        STORE R3, FIFO+0
        LOAD R3, FIFO+2
        STORE R3, FIFO+1
        LOAD R3, FIFO+3
        STORE R3, FIFO+2
        LOAD R3, FIFO+4
        STORE R3, FIFO+3
        LOAD R3, FIFO+5
        STORE R3, FIFO+4
        LOAD R3, FIFO+6
        STORE R3, FIFO+5
        LOAD R3, FIFO+7
        STORE R3, FIFO+6
        LOAD R3, FIFO+8
        STORE R3, FIFO+7
        LOAD R3, FIFO+9
        STORE R3, FIFO+8
        LOAD R3, FIFO+10
        STORE R3, FIFO+9
        STORE R0, FIFO+10
        JMP  dk1
nk1:    DELAY 63
dk1:    LDI  R2, 5                ; kind 5: CRC delimiter               [84]
        CMP  R1, R2
        BNE  nk5
        LDI  R3, 1                ; delimiter recessive                 [18]
        CMP  R0, R3
        BEQ  k5ok
        LDI  R3, 3
        STORE R3, STAT
        JMP  k5c
k5ok:   DELAY 6
k5c:    LOAD R3, CRCH             ; CRC remainder 0                     [36]
        LDI  R2, 0
        CMP  R3, R2
        BNE  cbad1
        LOAD R3, CRCL
        CMP  R3, R2
        BNE  cbad2
        DELAY 6
        JMP  cgd
cbad1:  DELAY 6
cbad2:  LDI  R3, 2
        STORE R3, STAT
        DELAY 3
cgd:    LOAD R3, STAT             ; all good: ACK (dominant now, released  [18]
        LDI  R2, 0                ; at the next boundary)
        CMP  R3, R2
        BNE  k5no
        SET  TX, 0
        JMP  k5d
k5no:   DELAY 3
k5d:    JMP  dk5
nk5:    DELAY 72
dk5:    LDI  R2, 6                ; kind 6: ACK delim + EOF recessive   [36]
        CMP  R1, R2
        BNE  nk6
        LDI  R3, 255
        CMP  R0, R3
        BEQ  k6ok
        LDI  R3, 3
        STORE R3, STAT
        JMP  k6d
k6ok:   DELAY 6
k6d:    LDI  R3, 1
        STORE R3, DONE
        JMP  dk6
nk6:    DELAY 24
dk6:    LOAD R2, PTR              ; DLC field (5) ended: LIM, DLC       [42]
        LDI  R1, 5
        CMP  R2, R1
        BNE  nlim
        LDI  R3, 247              ; DLC > 8 counts as 8
        ADD  R3, R0
        BNE  dok
        LDI  R0, 8
        JMP  dd
dok:    DELAY 3
dd:     STORE R0, DLC
        LDI  R3, 6
        ADD  R3, R0
        STORE R3, LIM
        JMP  dlim
nlim:   DELAY 27
dlim:   LDI  R0, 0                ; next field                          [42]
        STORE R0, ACC
        LDI  R1, 1
        ADD  R2, R1
        LOAD R3, LIM
        CMP  R2, R3
        BNE  nj
        LDI  R2, 14               ; after the data: the CRC fields
        JMP  njd
nj:     DELAY 3
njd:    STORE R2, PTR
        LDI  R1, FN
        ADD  R1, R2
        LOADX R0, R1
        STORE R0, NB
        JMP  pend                 ; [3]
noend:  DELAY 294                 ; [297]
pend:   RET
        ; -- stuff bit: must differ from the run; 441 cycles to the RET --
stf:    LOAD R0, LAST             ; [21]
        CMP  R0, R3
        BNE  stfok
        LDI  R0, 1                ; stuff error
        STORE R0, STAT
        STORE R0, DONE
        JMP  stfd
stfok:  LDI  R0, 1
        STORE R0, RUN
        STORE R3, LAST
        DELAY 0
stfd:   DELAY 417                 ; [420]
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
        ; FN (16-34): field lengths
        .byte 1, 3, 8, 1, 2, 4, 8, 8, 8, 8, 8, 8, 8, 8, 8, 7, 1, 1, 8
        .byte 0, 0, 0, 0, 0
        ; FK (40-58): field kinds
        .byte 0, 1, 1, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 5, 0, 6
