; WS2812 / WS2812B ("NeoPixel") LED driver: a frame of GRB bytes from the
; data region goes out on pin 0, then the line is held low to latch it.
;
; Not a protocol this chip was designed around (docs/architecture.md
; plans UART, SPI and I2C): a single-wire, self-clocked, sub-microsecond
; pulse-width code, written only from the existing ISA.
;
; Frame layout in the data region (512 + offset), so LOADX can walk it
; with the LOOP counter, which counts down: offset NBYTES holds the first
; byte sent, offset 1 the last; offset 0 is unused. Each LED takes 3
; bytes in WS2812 order: green, red, blue, sent MSB first. At most 255
; bytes (85 LEDs), LOADX's reach.
;
; Host side: after a frame is latched, firmware raises HOST_STATUS. To
; send it again, the host raises HOST_GO, waits for HOST_STATUS low, then
; drops HOST_GO (the host-initiated handshake, docs/architecture.md).
; New colours mean a new boot image (the chip is reloaded, not patched).
;
; Bit timing at 50 MHz (20 ns/cycle). Every instruction costs 3 cycles,
; DELAY N costs 3 + N, and a pin changes when its instruction commits.
; Per bit, from the rising edge:
;   T0H = DELAY N1 + OUTB                                = 6 + N1  = 20 (400 ns)
;   T1H = T0H + DELAY N2 + SET                           = 12+N1+N2 = 40 (800 ns)
;   bit = T1H + SHIFT + DELAY N3 + LOOP + next SET        = 24+N1+N2+N3 = 58 (1.16 us)
; so T0L = 38 (760 ns) and T1L = 18 (360 ns). The last bit of a byte is
; 9 cycles longer (LOOP, LOADX, LDI): T0L 47 (940 ns), T1L 27 (540 ns).
; WS2812B datasheet: T0H 400, T1H 800, T0L 850, T1L 450 ns, each +-150 ns;
; every case above is inside, including the byte-boundary bits. Reset
; (latch): line low > 50 us; 300 us here, for the newer parts that need it.

.equ D,  0                         ; data pin
.equ N1, 14
.equ N2, 14
.equ N3, 6
.equ NBYTES, 9                     ; bytes in the frame below (-D to change)

        SET  D, pp, 0              ; drive low: idle / reset
        DELAY 469, 1               ; 15008 cycles = 300 us: latch any previous data

frame:  LDI  R2, NBYTES            ; R2 = byte index, counts down to 1
byte:   LOADX R0, R2               ; R0 = next byte
        LDI  R1, 8                 ; R1 = bits left in it
bit:    SET  D, 1                  ; rising edge
        DELAY N1
        OUTB R0, D, 7              ; a 0 bit ends here ...
        DELAY N2
        SET  D, 0                  ; ... a 1 bit here
        SHIFT R0, L                ; next bit to bit 7
        DELAY N3
        LOOP bit, R1
        LOOP byte, R2

        DELAY 469, 1               ; latch: 300 us low
        SET  HOST_STATUS, 1        ; frame shown
        WAIT HOST_GO, 1            ; host asks for a resend
        SET  HOST_STATUS, 0
        WAIT HOST_GO, 0
        JMP  frame

; Demo frame: three LEDs, dim red, green and blue.
; Sent in this order (GRB per LED): 00 20 00 | 20 00 00 | 00 00 20
.data
        .byte 0                    ; offset 0: unused
        .byte 0x20, 0x00, 0x00     ; offsets 1-3: LED 2 (blue), last byte first
        .byte 0x00, 0x00, 0x20     ; offsets 4-6: LED 1 (green)
        .byte 0x00, 0x20, 0x00     ; offsets 7-9: LED 0 (red), sent first
