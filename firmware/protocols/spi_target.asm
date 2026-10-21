; SPI target (peripheral), mode 0: the chip is the device, an external
; controller drives the clock.
;
; The rest of the firmware here is controller-side and self-paced (DELAY
; sets every edge). Here the controller owns the clock and the chip
; follows it with WAIT on SCLK, so the target role needs no hardware
; change either.
;
; Pins, as for the SPI controller firmware but with directions reversed:
;   0 = MOSI (in)   1 = MISO (out, Hi-Z while CS is high)
;   2 = SCLK (in)   3 = CS (in, active low)
; Mode 0: SCLK idles low; both sides sample on the rising edge and change
; on the falling edge; MSB first.
;
; Behaviour: one register (R0) works as the shift register, as in a
; hardware SPI peripheral: bit 7 goes out on MISO while the MOSI bit is
; shifted into bit 0. So each transaction replies REPLY0 to the first
; byte, then echoes every byte one byte late, and the last byte received
; is shown on uo_out.
;
; Timing (50 MHz, 3 cycles per instruction, a pin input reaches the core
; 2 cycles after it changes). From SCLK rising, MOSI is sampled by the
; INB about 7 cycles later; from SCLK falling, MISO changes about 9
; cycles later (12 after CS falls, for the first bit). test_spi_target.py
; measures the fastest clock that works: see firmware/README.md.
;
; A transaction ends when CS goes high. If SCLK stops for GAP cycles
; while CS is still low, the firmware checks CS and keeps waiting; GAP
; only bounds how long MISO stays driven after CS rises.

.equ MOSI, 0
.equ MISO, 1
.equ SCLK, 2
.equ CS,   3
.equ REPLY0, 0x96                  ; reply to the first byte of a transaction
.equ GAP, 992                      ; cycles (~20 us, exactly encodable) without SCLK before checking CS

idle:   SET  MISO, in, 0           ; deselected: release MISO
        LDI  R0, REPLY0
        WAIT CS, 0                 ; selected
        SET  MISO, pp, 0
        LDI  R2, 8                 ; bits left in this byte
bit:    OUTB R0, MISO, 7           ; present the bit before SCLK rises
        SHIFT R0, L
rise:   WAIT SCLK, 1, GAP
        BEQ  pause                 ; no clock for GAP cycles
        INB  R0, MOSI, 0           ; sample MOSI into bit 0
        LOOP fall, R2
        OUT  R0                    ; whole byte received: show it
        LDI  R2, 8
fall:   WAIT SCLK, 0
        JMP  bit

pause:  INB  R3, CS, 0             ; R3 bit 0 = CS
        TEST R3, 0
        BEQ  idle                  ; CS high: transaction over
        JMP  rise                  ; CS still low: keep waiting for SCLK
