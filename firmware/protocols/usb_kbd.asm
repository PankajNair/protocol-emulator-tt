; USB low-speed HID keyboard: plug the chip into a PC (D+ on pin 0, D- on
; pin 1, 1.5 kOhm pull-up on D-), it enumerates as a boot keyboard and
; types the text at the end of this file.
;
; The serial engine (docs/serial_engine.md) does the bit level: 1.5
; Mbit/s timing, NRZI, bit stuffing, the CRC16 of data packets, the EOP.
; This firmware does the rest: token checks (CRC5 included), control
; transfers (GET_DESCRIPTOR, SET_ADDRESS, SET_CONFIGURATION, anything
; else acknowledged with no data), data toggles, the interrupt
; endpoint, bus reset.
;
; 314 words: more than the 256-word program region, so the subroutines
; live in the data region's upper half (.org 384), which the CPU runs like
; any other code (docs/isa.md).
;
; Timing: a byte on the wire is 264 clocks (88 instructions); the device
; must answer a token within 7.5 bit times (83 instructions) of its EOP.
;
; VID/PID 1209:0001 is pid.codes' shared test ID.

; ------------------------------------------------------------ data map
.equ DEV, 0                     ; device descriptor, 18 bytes
.equ CFG, 18                    ; configuration descriptor set, 34 bytes
.equ REP, 52                    ; HID report descriptor, 63 bytes
.equ REPLEN, 63
.equ TXT, 115                   ; text to type: HID usage | 0x80 for shift, 0-terminated
.equ RPT, 160                   ; the 8-byte keyboard report sent on EP1
; variables (LOAD/STORE only)
.equ ADDR, 200                  ; device address
.equ NEWA, 201                  ; address to take after the status stage
.equ T0B, 202                   ; expected 2nd token byte for EP0 (CRC5 << 3)
.equ T1A, 203                   ; expected 1st token byte for EP1 (addr | 0x80)
.equ T1B, 204
.equ STAGE, 205                 ; EP0 IN: 0 nothing (NAK), 1 data, 2 status ZLP, 3 STALL
.equ PTR, 206                   ; EP0 data: next descriptor byte
.equ LEFT, 207                  ; EP0 data: bytes left
.equ NCH, 208                   ; bytes in the chunk last sent
.equ TOG0, 209                  ; next DATA PID on EP0
.equ TOG1, 210                  ; next DATA PID on EP1
.equ CONFD, 211                 ; configured
.equ KPTR, 212                  ; next character of TXT
.equ KREL, 213                  ; 1: the next report releases the key
.equ S_REQ, 214                 ; SETUP: bRequest, wValue L/H, wLength L/H
.equ S_VL, 215
.equ S_VH, 216
.equ S_LL, 217
.equ S_LH, 218

.equ PIDSETUP, 0x2D
.equ PIDIN, 0x69
.equ PIDOUT, 0xE1
.equ DATA0, 0xC3
.equ DATA1, 0x4B
.equ ACK, 0xD2
.equ NAK, 0x5A
.equ STALL, 0x1E
.equ MODE_DATA, 0xE7            ; nrzi diff se0 crc_tx crc_inv rx_en
.equ MODE_HS, 0x87              ; the same without the CRC
.equ BT, 608                    ; byte timeout inside a packet (clocks; ~2.3 bytes)

; ------------------------------------------------------------ engine set-up
        LDI  R0, 33             ; bit period (1.5 Mbit/s at 50 MHz, 1% fast)
        SCFG R0, 0
        LDI  R0, 16             ; sample mid-bit
        SCFG R0, 2
        LDI  R0, 0xF7           ; stuff after 7 equal levels, CRC width 16
        SCFG R0, 5
        LDI  R0, 0x05           ; CRC16 polynomial 0x8005
        SCFG R0, 6
        LDI  R0, 0x80
        SCFG R0, 7
        LDI  R0, 0x12           ; CRC from byte 2 (after SYNC, PID), all ones
        SCFG R0, 8
        LDI  R0, MODE_DATA
        SCFG R0, 4

; ------------------------------------------------------------ bus reset
reset:  LDI  R0, 0
        STORE R0, ADDR
        STORE R0, NEWA
        STORE R0, STAGE
        STORE R0, CONFD
        STORE R0, KPTR
        STORE R0, KREL
        LDI  R0, DATA0
        STORE R0, TOG1
newadr: LOAD R0, NEWA           ; take the address, precompute its tokens
        STORE R0, ADDR
        CALL crc5               ; EP0: addr, CRC5(addr, 0)
        STORE R1, T0B
        LOAD R0, ADDR
        LDI  R1, 0x80
        XOR  R0, R1
        STORE R0, T1A
        CALL crc5               ; EP1: addr | 0x80, CRC5(addr, 1)
        STORE R1, T1B

; ------------------------------------------------------------ packets
idle:   SRX  R0, 4000
        BEQ  quiet
        LDI  R1, 0x80           ; SYNC
        CMP  R0, R1
        BNE  drain
        SRX  R0, BT             ; PID
        BEQ  idle
        LDI  R1, PIDSETUP
        CMP  R0, R1
        BEQ  setup
        LDI  R1, PIDIN
        CMP  R0, R1
        BEQ  intok
        LDI  R1, PIDOUT
        CMP  R0, R1
        BEQ  outtok
drain:  SRX  R0, BT             ; not for us: skip to its end
        BNE  drain
        JMP  idle

; nothing for 80 us: a bus reset is SE0 for >= 2.5 us (a keep-alive
; EOP is 1.3 us), so SE0 seen twice 5 us apart is a reset
quiet:  SRD  R0, 0
        TEST R0, 6
        BNE  idle
        DELAY 250
        SRD  R0, 0
        TEST R0, 6
        BNE  idle
        JMP  reset

; ------------------------------------------------------------ SETUP
setup:  CALL tok
        BNE  idle
        SRX  R0, BT             ; DATA0: SYNC, PID
        SRX  R0, BT
        SRX  R0, BT             ; bmRequestType
        SRX  R0, BT
        STORE R0, S_REQ
        SRX  R0, BT
        STORE R0, S_VL
        SRX  R0, BT
        STORE R0, S_VH
        SRX  R0, BT             ; wIndex
        SRX  R0, BT
        SRX  R0, BT
        STORE R0, S_LL
        SRX  R0, BT
        STORE R0, S_LH
        SRX  R0, BT             ; CRC16
        SRX  R0, BT
        CALL crcok              ; flag 1: good packet
        BNE  idle
        CALL sendack
        LDI  R0, DATA1          ; the data/status stage starts with DATA1
        STORE R0, TOG0
        LOAD R0, ADDR
        STORE R0, NEWA
        LDI  R2, 0
        LOAD R0, S_REQ
        LDI  R1, 6
        CMP  R0, R1
        BEQ  getdsc
        LDI  R1, 5
        CMP  R0, R1
        BNE  setcfg
        LOAD R0, S_VL           ; SET_ADDRESS: after the status stage
        STORE R0, NEWA
        JMP  stage2
setcfg: LDI  R1, 9              ; SET_CONFIGURATION (or a class request)
        CMP  R0, R1
        BNE  stage2
        LDI  R0, 1
        STORE R0, CONFD
stage2: LDI  R0, 2              ; status stage: a zero-length IN
        JMP  setstg

getdsc: LOAD R0, S_VH
        LDI  R3, 18
        LDI  R1, 1
        CMP  R0, R1
        BEQ  gd                 ; device: R2 = DEV = 0
        LDI  R2, CFG
        LDI  R3, 34
        LDI  R1, 2
        CMP  R0, R1
        BEQ  gd
        LDI  R2, REP
        LDI  R3, REPLEN
        LDI  R1, 0x22
        CMP  R0, R1
        BEQ  gd
        LDI  R0, 3              ; unknown descriptor: STALL
        JMP  setstg
gd:     STORE R2, PTR
        LOAD R0, S_LH           ; LEFT = min(length, wLength)
        LDI  R1, 0
        CMP  R0, R1
        BNE  gd1
        LOAD R0, S_LL
        LDI  R1, 0xFF           ; carry iff wLength > length
        XOR  R1, R3
        ADD  R1, R0
        BEQ  gd1
        LDI  R3, 0
        XOR  R3, R0
gd1:    STORE R3, LEFT
        LDI  R0, 1
setstg: STORE R0, STAGE
        JMP  idle

; ------------------------------------------------------------ OUT (EP0 status)
outtok: CALL tok
        BNE  idle
odrain: SRX  R0, BT             ; the data packet (a ZLP, or SET_REPORT data)
        BNE  odrain
        CALL crcchk             ; (odrain took the end already)
        BNE  idle
        CALL sendack
        JMP  idle

; ------------------------------------------------------------ IN
intok:  CALL tok
        BEQ  in0
        LOAD R0, T1A            ; EP1?
        CMP  R2, R0
        BNE  idle
        LOAD R0, T1B
        CMP  R3, R0
        BNE  idle
        ; EP1: the keyboard report
        LOAD R0, CONFD
        TEST R0, 0
        BNE  sendnak
        LDI  R0, 0              ; release: no modifier, no key
        LDI  R1, 0
        LOAD R3, KREL
        TEST R3, 0
        BEQ  report
        LOAD R2, KPTR
        LDI  R3, TXT
        ADD  R2, R3
        LOADX R1, R2            ; key | 0x80 for shift
        CMP  R1, R0
        BEQ  sendnak                ; all typed
        TEST R1, 7
        BNE  report
        LDI  R0, 0x80           ; shift: clear bit 7 (carry out), modifier 2
        ADD  R1, R0
        LDI  R0, 2
report: STORE R0, RPT
        STORE R1, RPT+2
        LOAD R0, TOG1
        LDI  R2, RPT
        LDI  R3, 8
        CALL txd
        CALL rxack
        BNE  idle
        LOAD R0, TOG1
        LDI  R1, 0x88           ; DATA0 <-> DATA1
        XOR  R0, R1
        STORE R0, TOG1
        LOAD R0, KREL           ; press -> release -> next character
        LDI  R1, 1
        XOR  R0, R1
        STORE R0, KREL
        TEST R0, 0
        BEQ  idle               ; pressed: the release comes next
        LOAD R0, KPTR
        ADD  R0, R1
        STORE R0, KPTR
        JMP  idle

in0:    LOAD R0, STAGE
        LDI  R1, 1
        CMP  R0, R1
        BEQ  in0dat
        LDI  R1, 2
        CMP  R0, R1
        BEQ  in0zlp
        LDI  R1, 3
        CMP  R0, R1
        BNE  sendnak
        LDI  R0, STALL
        JMP  hsgo
sendnak: LDI R0, NAK
hsgo:   CALL hs
        JMP  idle

in0zlp: LDI  R0, DATA1          ; status stage: zero-length DATA1
        LDI  R3, 0
        CALL txd
        CALL rxack
        BNE  idle
        LDI  R0, 0
        STORE R0, STAGE
        JMP  newadr             ; SET_ADDRESS takes effect now

in0dat: LOAD R1, LEFT           ; chunk = min(8, LEFT)
        LDI  R3, 0xF8
        ADD  R3, R1             ; carry iff LEFT >= 8
        LDI  R3, 8
        BEQ  in0c
        LDI  R3, 0
        XOR  R3, R1
in0c:   STORE R3, NCH
        LOAD R0, TOG0
        LOAD R2, PTR
        CALL txd
        CALL rxack
        BNE  idle               ; no ACK: the host repeats the IN, we resend
        LOAD R1, NCH
        LOAD R0, PTR
        ADD  R0, R1
        STORE R0, PTR
        LDI  R0, 0xFF           ; LEFT -= NCH: LEFT + ~NCH + 1
        XOR  R1, R0
        LDI  R0, 1
        ADD  R1, R0
        LOAD R0, LEFT
        ADD  R0, R1
        STORE R0, LEFT
        LOAD R0, TOG0
        LDI  R1, 0x88
        XOR  R0, R1
        STORE R0, TOG0
        JMP  idle

; ------------------------------------------------------------ subroutines
; In the data region's upper half (words 384-511 = data offsets 256-511;
; the descriptors and variables sit below offset 256, where LOADX reaches).
        .org 384
; tok: read a token's two bytes into R2, R3 and its end; flag 1 iff it
; is for EP0 at our address.
tok:    SRX  R2, BT
        SRX  R3, BT
        SRX  R0, BT
        LOAD R0, ADDR
        CMP  R2, R0
        BNE  tokx
        LOAD R0, T0B
        CMP  R3, R0
tokx:   RET

; crcok: wait for the end of a data packet; flag 1 iff its CRC16 is good
crcok:  SRX  R0, BT
crcchk: SRD  R0, 1              ; residual 0x800D
        LDI  R1, 0x0D
        CMP  R0, R1
        BNE  crcx
        SRD  R0, 2
        LDI  R1, 0x80
        CMP  R0, R1
crcx:   RET

; rxack: receive the host's handshake; flag 1 iff it is an ACK
rxack:  SRD  R0, 0              ; our packet is still going out
        TEST R0, 5
        BEQ  rxack
        SRX  R0, 992            ; SYNC (16 bit times + slack)
        SRX  R0, BT             ; PID
        SRX  R1, BT             ; end
        LDI  R1, ACK
        CMP  R0, R1
        RET

; txd: data packet: SYNC, PID R0, R3 bytes from data offset R2, CRC16
txd:    DELAY 40                ; >= 2 bit times after the host's EOP
        LDI  R1, MODE_DATA
        SCFG R1, 4
        LDI  R1, 0x80
        STX  R1
        STX  R0
        LDI  R0, 0
        CMP  R3, R0
        BEQ  txdx
        LDI  R0, 1
txdl:   LOADX R1, R2
        STX  R1
        ADD  R2, R0
        LOOP txdl, R3
txdx:   RET

; sendack / hs: handshake packet: SYNC, PID R0, no CRC
sendack: LDI R0, ACK
hs:     DELAY 40
        LDI  R1, MODE_HS
        SCFG R1, 4
        LDI  R1, 0x80
        STX  R1
        STX  R0
        RET

; crc5: R1 = CRC5 of a token's 11 bits (R0 = first byte, then three 0s:
; endpoint bits 1-3), shifted into place (<< 3) for the second byte.
crc5:   LDI  R1, 0x1F
        LDI  R2, 11
c5l:    LDI  R3, 0
        XOR  R3, R1
        XOR  R3, R0             ; bit 0: crc ^ data
        SHIFT R1, R
        SHIFT R0, R
        TEST R3, 0
        BNE  c5n
        LDI  R3, 0x14
        XOR  R1, R3
c5n:    LOOP c5l, R2
        LDI  R3, 0x1F
        XOR  R1, R3
        SHIFT R1, L
        SHIFT R1, L
        SHIFT R1, L
        RET

; ------------------------------------------------------------ data
.data
; device descriptor
.byte 18, 1, 0x10, 0x01, 0, 0, 0, 8
.byte 0x09, 0x12, 0x01, 0x00, 0x00, 0x01, 0, 0, 0, 1
; configuration, interface (HID boot keyboard), HID, endpoint 0x81
.byte 9, 2, 34, 0, 1, 1, 0, 0x80, 50
.byte 9, 4, 0, 0, 1, 3, 1, 1, 0
.byte 9, 0x21, 0x11, 0x01, 0, 1, 0x22, REPLEN, 0
.byte 7, 5, 0x81, 3, 8, 0, 10
; report descriptor: HID 1.11 appendix B.1, the boot keyboard
.byte 0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7
.byte 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01
.byte 0x75, 0x08, 0x81, 0x01, 0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01
.byte 0x29, 0x05, 0x91, 0x02, 0x95, 0x01, 0x75, 0x03, 0x91, 0x01, 0x95, 0x06
.byte 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65
.byte 0x81, 0x00, 0xC0
; text: "Hello, TT!" then Enter (usages; 0x80 = with shift)
.byte 0x0B+0x80, 0x08, 0x0F, 0x0F, 0x12, 0x36, 0x2C, 0x17+0x80, 0x17+0x80, 0x1E+0x80
.byte 0x28, 0
