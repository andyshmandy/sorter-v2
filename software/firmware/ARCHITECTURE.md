# Firmware & Hardware Architecture

This document describes the Raspberry Pi Pico firmware and hardware communication layer.

## Hardware Topology

### Two Independent Picos

Each machine has two Raspberry Pi Picos connected to the Orange Pi 5 via USB:

```
Orange Pi 5 (Main Controller)
  ├─ USB─serial─────┬─────► Feeder Pico (SorterInterface)
  │                 │
  │                 └─────► Distribution Pico (SorterInterface)
  │
  └─ USB─video ────► Cameras (OV9732, IMX415)
```

**Feeder Pico**:
- Stepper drivers for C-channel rotors (up to 3)
- GPIO for limit switches and status LEDs
- Optional servo driver (for older 2-channel servo designs)

**Distribution Pico**:
- Stepper driver for carousel and chute motors
- Servo driver (PCA9685 or Waveshare servo bus) for layer positioning
- GPIO for endstops and status signals

### Motor Control

**Steppers**: TMC2209 drivers on shared UART bus
- STEP/DIR control for motion
- StallGuard detection (back-EMF stall monitoring)
- Current control registers (IRUN, IHOLD)
- Addressable by UART slave address (0x00-0x03)

**Servos**: Two options per machine
1. **PCA9685** — I2C, 16-channel PWM (older designs)
   - Resolution: 4096 steps per servo
   - Frequency: 50 Hz (20 ms period)
   - Range: ~1000-2000 μs pulse width
   
2. **Waveshare Servo Bus** — UART, addressable servo protocol
   - 253 addressable servos per bus
   - Protocol: 8 bytes per command
   - Range: 500-2500 μs pulse width

### Communication Protocol

**Serial Link** (Pico → Orange Pi):
- Baud rate: 576 kbaud
- Encoding: COBS (Consistent Overhead Byte Stuffing)
- Framing: [header:1] [length:2] [data:N] [CRC32:4]
- Timeout: 100 ms for response

**Command/Response Pattern**:
```
Host → Pico:
  [0x01] [0x00, 0x08] [MOVE_STEPS] [steps:4] [speed:2] [accel:2] [CRC32:4]

Pico → Host:
  [0x01] [0x00, 0x04] [ACK] [result:4] [CRC32:4]
```

## Firmware Architecture

### Startup Sequence

```c
main() {
  1. stdio_init_all()           // Serial REPL (for debug)
  2. gpio_init_all()            // Initialize pins
  3. uart_init(uart0, 576000)   // Main serial link to backend
  4. i2c_init(i2c0, 400000)     // PCA9685 or other I2C devices
  
  5. register_devices()         // Detect TMC2209 addresses, PCA9685
  6. SorterInterface_init()     // Send device list to host
  
  7. while (true) {
       handle_incoming_commands()
       update_stall_flags()       // Check StallGuard periodically
       heartbeat_watchdog()
     }
}
```

### Command Handler

```c
void handle_incoming_command() {
  Packet pkt = receive_packet();  // COBS decode + CRC check
  
  switch (pkt.command) {
    case STEPPER_MOVE_STEPS:
      handle_stepper_move(pkt);
      break;
    case STEPPER_HOME:
      handle_stepper_home(pkt);
      break;
    case READ_STALL:
      handle_read_stall(pkt);
      break;
    case SERVO_SET_POSITION:
      handle_servo_move(pkt);
      break;
    // ... other commands
  }
}
```

## Device Drivers

### TMC2209 Stepper Driver

**Interface** (`firmware/tmc2209/`):

```c
typedef struct {
  uint8_t uart_slave_addr;  // 0x00-0x03 (DAT0/DAT1 pin setting)
  uint16_t microsteps;      // 1, 2, 4, 8, 16
  uint16_t run_current_ma;  // Motor current in mA
  uint16_t hold_current_ma;
  bool stall_enabled;
  uint8_t stall_threshold;  // 0-255 (sensitivity)
} TMC2209_Config;

void tmc2209_init(TMC2209_Config cfg);
void tmc2209_move_steps(int32_t steps, uint16_t speed, uint8_t accel);
void tmc2209_move_at_speed(int16_t speed);
void tmc2209_home(GPIO pin_limit_switch);
void tmc2209_stop();
uint16_t tmc2209_read_stall_count();
```

**Registers**:
- `GCONF` (0x00) — Global config
- `IHOLD_IRUN` (0x10) — Current settings
- `CHOPCONF` (0x6C) — Microstepping config
- `COOLCONF` (0x6D) — StallGuard config

### PCA9685 PWM Driver

**Interface** (`firmware/pca9685/`):

```c
void pca9685_init(uint8_t i2c_addr);
void pca9685_set_frequency(uint16_t freq_hz);  // Usually 50 Hz
void pca9685_set_pulse(uint8_t channel, uint16_t pulse_us);  // 1000-2000 μs
```

## Command Reference

### Stepper Commands

**MOVE_STEPS** (0x21)
- Input: device_addr, steps (int32), speed (uint16), accel (uint8)
- Output: ACK
- Behavior: Blocking step motion; returns when complete

**MOVE_AT_SPEED** (0x22)
- Input: device_addr, speed (int16)
- Output: ACK
- Behavior: Continuous motion until STOP received

**STOP** (0x23)
- Input: device_addr
- Output: ACK
- Behavior: End current motion, hold position

**HOME** (0x24)
- Input: device_addr, home_pin, inverted (bool)
- Output: ACK
- Behavior: Move toward limit switch until triggered, zero position

**READ_POSITION** (0x25)
- Input: device_addr
- Output: position (int32)

**READ_STALL_COUNT** (0x26)
- Input: device_addr
- Output: stall_count (uint16)

**SET_CURRENT** (0x27)
- Input: device_addr, irun_ma (uint16), ihold_ma (uint16)
- Output: ACK
- Behavior: Update motor current (takes effect immediately)

**SET_MICROSTEPS** (0x28)
- Input: device_addr, microsteps (uint16)
- Output: ACK
- Behavior: Change microstepping resolution (1, 2, 4, 8, or 16)

### Servo Commands

**SERVO_SET_POSITION** (0x31)
- Input: servo_id, pulse_us (uint16), 1000-2000
- Output: ACK

**SERVO_ENABLE** (0x32)
- Input: servo_id
- Output: ACK

**SERVO_DISABLE** (0x33)
- Input: servo_id
- Output: ACK

### GPIO Commands

**GPIO_SET** (0x41)
- Input: pin, value (bool)
- Output: ACK

**GPIO_READ** (0x42)
- Input: pin
- Output: value (bool)

## Firmware Build & Deployment

### Build Environment

```bash
cd firmware/sorter_interface_firmware/
export PICO_SDK_PATH=/opt/pico-sdk
cmake -B build -S .
cd build && make
```

### Flashing

**Method 1: BOOTSEL Button (Manual)**
1. Hold BOOTSEL while plugging in USB → mounts as `RPI-RP2`
2. Copy `.uf2` file to mounted drive
3. Auto-resets and runs new firmware

**Method 2: Backend Auto-Flash** (Linux/macOS)
```bash
# Pico detected as BOOTSEL device automatically
python firmware_flash.py --firmware build/sorter_interface.uf2 --device-name RPI-RP2
```

**Windows Fallback**:
- Detects `RPI-RP2` volume via Windows API
- Copies UF2 file
- Manual BOOTSEL if auto re-enum fails

### Build Variants

**Feeder Board**:
```cmake
set(NUM_STEPPER_DEVICES 3)  # c_channel_1, c_channel_2, c_channel_3
set(USE_PCA9685 OFF)
set(USE_WAVESHARE_SERVO OFF)
```

**Distribution Board**:
```cmake
set(NUM_STEPPER_DEVICES 2)  # carousel, chute
set(USE_PCA9685 ON)         # Or USE_WAVESHARE_SERVO
set(SERVO_COUNT 6)          # For multi-layer distribution
```

## Reliability & Safety

### Watchdog Timer

```c
void setup_watchdog() {
  watchdog_enable(1000, true);  // 1s timeout, reset on timeout
}

// In main loop:
watchdog_update();  // Kick watchdog every 100ms
```

**Purpose**: Detect firmware crash or command handling stall. Pico auto-resets if backend command handler takes >1s.

### Stall Detection

**StallGuard** (TMC2209 back-EMF monitoring):
- Monitors back-EMF while stepping
- Flags stall if EMF drops below threshold
- Firmware reads SGTSTP register every tick
- Cumulative stall counter sent to host

**Usage** (from backend):
```python
stepper.set_stall_config(threshold=20)  # Lower = more sensitive
while piece_moving:
  if stepper.get_stall_count() > 0:
    print("Stall detected! Piece jammed?")
    break
```

### Communication Reliability

**Timeout & Retry** (in firmware_flash.py and backend):
- Send command
- Wait 100 ms for response
- If timeout: retry up to 3 times
- If still timeout: raise MCUBusError

**Packet Validation**:
- CRC32 check on every frame
- Malformed frames are silently dropped (retry)
- Host resends on timeout

## Hardware-Backend Contract

### Device Discovery

**Feeder Pico announces**:
```
DEVICE: motor_0 type=stepper addr=0x00 role=c_channel_1_rotor
DEVICE: motor_1 type=stepper addr=0x01 role=c_channel_2_rotor
DEVICE: motor_2 type=stepper addr=0x02 role=c_channel_3_rotor
```

**Backend parses and binds** to logical roles via machine.toml:
```toml
[stepper_bindings]
carousel = "c_channel_4_rotor"  # Maps to "carousel" in firmware
```

### Firmware-Backend Data Contract

| Aspect | Firmware | Backend |
|--------|----------|---------|
| **Position** | Reads encoder/steps, never persists | Owns position state, sends MOVE commands |
| **Current** | Stores in chip registers, persists | Sends SET_CURRENT on boot and on demand |
| **Speed profile** | Executes acceleration ramp | Defines accel and speed per MOVE |
| **Stall detection** | Detects via back-EMF | Queries and interprets stall count |
| **Servo position** | Reads PWM pulse feedback (if available) | Owns servo state, sends SET_POSITION |

**Idempotency**: Backend can safely re-run the same command; firmware has no persistent memory of commands.

## Testing & Validation

### Firmware Unit Tests

```c
// test_tmc2209.c
void test_move_steps() {
  TMC2209_Config cfg = { .uart_slave_addr = 0 };
  tmc2209_init(cfg);
  tmc2209_move_steps(100, 500, 10);  // 100 steps
  ASSERT_EQ(tmc2209_read_position(), 100);
}
```

### Integration Tests (on-hardware)

```bash
# Feeder board
test_stepper_move        # Move each rotor, verify speed and stall
test_stepper_home        # Home against limit switch
test_gpio_read_switches  # Verify limit switch wiring

# Distribution board
test_carousel_speed      # Verify carousel timing
test_servo_positioning   # Sweep each servo across range
test_chute_calibration   # Verify chute geometry
```

### Stress Testing

```bash
# Run 1M moves (stress test for bit-flip or timing issues)
python stress_test.py --pico /dev/ttyACM0 --moves 1000000 --verify-position
```
