; PS/2 host, receive side: bytes from a PS/2 keyboard or mouse go to the
; host, one per OUT handshake.
;
; Not a protocol this chip was designed around: the device owns the
; clock (10-16.7 kHz), the chip follows it with WAIT, and holds the
; device off while it is busy -- PS/2's own flow control.
;
; Pins: DATA = 0, CLK = 1, both open-drain (the device and the chip only
; ever pull low; external pull-ups, or the device's own).
;
; Frame (device to host): start 0, 8 data bits LSB first, odd parity,
; stop 1; the host samples DATA on each falling CLK edge. After a frame
; the firmware pulls CLK low ("inhibit"): the device must not start the
; next frame until CLK is released, so a byte waits in the device, not
; here. The firmware then hands the byte over (OUT handshake,
; host-initiated) and releases CLK.
;
; Parity without an XOR instruction: R2 counts down once per 1 bit (a
; LOOP to the very next instruction is a plain decrement), so bit 0 of
; R2 is the parity of the count. 8 data bits + the parity bit must hold
; an odd number of 1s.
;
; A bad start bit, parity or stop bit raises HOST_ERROR (sticky) and the
; byte is dropped; the next frame is received normally.

.equ DATA, 0
.equ CLK,  1

        SET  DATA, od, 1           ; release both lines
        SET  CLK, od, 1

frame:  WAIT CLK, 0                ; start bit
        INB  R3, DATA, 0           ; R3 bit 0 = start bit
        LDI  R2, 0                 ; 1-bit counter (parity)
        LDI  R1, 8
dbit:   WAIT CLK, 1
        WAIT CLK, 0
        SHIFT R0, R
        INB  R0, DATA, 7           ; LSB first: after 8 shifts the first bit is bit 0
        TEST R0, 7
        BNE  d0                    ; a 0 bit
        LOOP d0, R2                ; a 1 bit: count it (decrement)
d0:     LOOP dbit, R1

        WAIT CLK, 1                ; parity bit
        WAIT CLK, 0
        INB  R1, DATA, 0
        TEST R1, 0
        BNE  p0
        LOOP p0, R2
p0:     WAIT CLK, 1                ; stop bit
        WAIT CLK, 0
        INB  R1, DATA, 0
        SET  CLK, 0                ; inhibit: hold the next byte in the device

        TEST R3, 0                 ; start bit must be 0
        BEQ  bad
        TEST R2, 0                 ; odd number of 1s
        BNE  bad
        TEST R1, 0                 ; stop bit must be 1
        BNE  bad
        CALL putb
        JMP  release
bad:    SET  HOST_ERROR, 1
release: SET CLK, 1
        WAIT CLK, 1                ; see the release first: our own low is still in
        JMP  frame                 ; the input synchronizer (and a pull-up is slow)

; OUT handshake (host-initiated): R0 -> host
putb:   WAIT HOST_GO, 1
        OUT  R0
        SET  HOST_STATUS, 1
        WAIT HOST_GO, 0
        SET  HOST_STATUS, 0
        RET
