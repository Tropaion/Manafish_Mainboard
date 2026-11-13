# Pico control protocol v1 (frozen implementation contract)

Scope: RP2040 Pico, SPI1 GP10 SCK/GP11 MOSI/GP12 MISO/GP13 CS. Legacy packets and ESC firmware upload remain unchanged. Internal motor commands are trunc(1000 + 1000 * clipped_thrust), 0..2000; downstream DShot/PWM conversion remains unchanged.

Mainboard scope: the same protocol also runs on the Manafish mainboard's RP2354A (`make build-mainboard`, `boards/manafish_mainboard.h`) with the same BMI270 SPI1 pins and motor pins (GP6-9, GP18-21). The mainboard build adds only the ESC power switch on GP14 (ESC_POWER below: request 0x26, feature bit 0x20, ATTITUDE health bits 4-6, ESC update error 11) and its board-defined IMU transform. Pico and Pico 2 builds keep the behaviour described here without ESC power.

## Negotiation and framing

Send the existing seven-byte C5 request with command=3 (GET_CONTROL_CAPABILITIES), nonzero request ID, protocol=0, speed=0, legacy XOR checksum. Old firmware rejects this complete fixed-size request without scanning its contents. Wait for capability response before sending extended frames. Capabilities response is extended type 0x81, session=0, sequence=request ID.

Extended frame (all integer fields little endian): start u8=0xF0, version u8=1, type u8, flags u8=0, payload_length u16, session u32, sequence u32, payload, CRC32C u32 over header including F0 and payload. Header=14 bytes, overhead=18. CRC32C reflected polynomial 0x82F63B78, initial/final XOR 0xffffffff. Maximum payload=768. Incomplete frame timeout=100ms. Invalid lengths/versions/CRC never execute. Parser consumes a complete extended frame as one unit; legacy tokens inside payload are not dispatched.

Capabilities payload: schema u16=1, max_payload u16=768, settings_size u16=628, max_nullspace u16=8, feature_bits u32=0x1f (control, settings, pressure, absolute quaternion, development identity; mainboard builds add 0x20 ESC power and report 0x3f, and hosts must not require 0x20), build_identity ASCII bytes to payload end. Build identity is development metadata, not a changed release version. Host may suppress bundled release auto-reconciliation only with build_identity prefixed pico-control-dev:, verified API capability, AND explicit MANAFISH_PICO_CONTROL_DEVELOPMENT=1 for the test deployment. The feature bit alone never disables updates; no pretending to be the bundled stable artifact.

HELLO type=0x01: empty payload, host chooses nonzero session and sequence=1. Device neutralizes output, clears pending work and sequence state, replies ACK. Only accept HELLO while neutral/idle, never during an active upload. Recovery permits neutral negotiation/maintenance entry, but never settings activation or nonneutral output. New session requires settings commit before control readiness. Other messages must match active session. Host uses one globally increasing u32 sequence across all extended requests; no wrap within a session (renegotiate). Ordered commands execute FIFO, never latest-only. Duplicate last request gets same result without execution; older sequence gets STALE. Host serializes reliable mutations and retries the same sequence/payload after timeout, or queries committed settings. Streaming messages are not retried. An in-flight mutation blocks subsequent requests with BUSY. Duplicate sequence with different content is INVALID; older requests remain STALE even if their operation once succeeded. COMMIT outcome recovery uses a new QUERY sequence and exact generation/digest, not replaying old setters. A settings application can take up to seven seconds internally; timeout leaves output inhibited and the outcome must be reconciled. A reliable mutation pauses streaming writes until acknowledged, preventing later sequence numbers overtaking retries. USB write lock covers whole frames.

ACK type=0x80: request_type u8, result u8, reserved u16=0, active_generation u32, active_settings_crc32c u32. Header sequence echoes request. Results: 0 APPLIED, 1 STAGED, 2 INVALID, 3 BUSY, 4 STALE, 5 NOT_READY, 6 UNSUPPORTED. Only APPLIED permits success toast. Settings/target mutations ACK only after core1 consumes them; telemetry cannot substitute for ACK. CONTROL/PRESSURE/RAW do not ACK successful streams; errors may ACK.

## Settings transaction

BEGIN 0x20: generation u32 (nonzero), total_size u32=628, settings_crc32c u32. CHUNK 0x21: offset u32 + bytes (bounded); sequential contiguous chunks, duplicate same bytes permitted. COMMIT 0x22: generation u32, crc32c u32. ABORT 0x23: empty. QUERY 0x24: empty, ACK carries active generation/digest. Transaction expires after 5s inactivity. One staging transaction. BEGIN/CHUNK ACK STAGED. COMMIT ACK APPLIED only after atomic core1 activation. No partial settings activation. A new-sequence COMMIT for matching active generation/digest returns APPLIED. QUERY and successful ABORT return APPLIED; ABORT cannot cancel an already applying COMMIT. Host timeout 8s, app timeout 12s.

Settings image exactly 628 bytes, packed explicitly (not native C struct):

| Offset | Field |
|---|---|
| 0 | roll kp,ki,kd,rate: 4xf32 |
| 16 | pitch kp,ki,kd,rate: 4xf32 |
| 32 | yaw kp,ki,kd,rate: 4xf32 |
| 48 | depth kp,ki,kd,rate: 4xf32 |
| 64 | fpv_mode u32 (0/1) |
| 68 | thrusters_limit,actions_limit,regulator_limit: 3xf32 percentages |
| 80 | surge,sway,heave coefficients: 3xf32 |
| 92 | allocation: 64xf32 row-major 8x8 |
| 348 | identifiers: 8xu8, each 0..7 (duplicates accepted, matching np.take) |
| 356 | spin_directions: 8xi8, each -1 or +1 |
| 364 | nullspace_count u32, 0..8 |
| 368 | nullspace vectors: 8x8xf32, unused rows zero |
| 624 | output protocol u16: PWM=0, DSHOT=1 |
| 626 | DShot speed u16: 150/300/600 (1200 only Pico2) |

All floats finite. Power limits 0..100. Nullspace count bounded, no silent truncation. Same-count updates preserve nullspace history; count changes reset it. COMMIT gates physical outputs neutral, finishes existing core0 protocol transition using the output suffix, activates matching pure settings on core1, then sends one APPLIED generation/digest ACK. A failed transition retains the old committed generation and inhibits outputs until reconciliation. A CONTROL or nonneutral RAW cannot clear this configuration-reconciliation gate; a matching applied settings result with the requested physical protocol ready is required. QUERY still reports the actual committed generation/digest, so observing the previous generation is not proof that a failed new configuration succeeded. Legacy C5/D5 remains for tools, not a substitute for atomic extended settings. New control sessions do not bypass protocol readiness or recovery lockout.

## Commands

CONTROL 0x10, payload 40 bytes: direction[8] f32; source_dt f32; flags u32. Direction order surge,sway,heave,pitch,yaw,roll,action1,action2. Flags bit0 stabilization, bit1 depth_hold, bit2 input_valid; all others zero. Values finite, direction [-1,1]. source_dt uses legacy clamp [1/120,1/6], nonfinite replaced by 1/60. Each fresh accepted command advances desired attitude, desired depth, depth PID, and nullspace decay eligibility exactly once. Core1 processes at most one CONTROL before a fresh-sample control step, retaining FIFO order for batched commands. Invalid/stale commands never advance targets/PID. Stale pressure inhibits depth-dependent commands. Source dt controls legacy integration; USB worker IRQ receipt time independently controls the 200ms lease. Time spent in USB or cross-core queues never renews that lease. Queue overflow inhibits output. Disabled input immediately neutralizes output; Pi must not perpetuate stale application input by sending valid zero commands. Host lease=200ms.

PRESSURE 0x11, payload 12 bytes: depth f32 (meters), depth_change f32 (meters/sec, Pi filtered derivative), healthy u32 (0/1). Send once per fresh Pi 15Hz measurement. Does not renew control lease or advance depth PID. Pressure expiry=500ms inhibits depth-dependent control; temperature/absolute pressure UI remain Pi-owned.

SET_ATTITUDE 0x12, payload 16 bytes: absolute quaternion [x,y,z,w] f32, body-to-world, normalized by receiver; reject nonfinite/near-zero norm. Does not enable stabilization or reset integral. Executes in sequence order; following CONTROL enable edge still has legacy leveling semantics. ACK after application.

SET_DEPTH 0x13, payload 4 bytes: desired_depth f32 meters, finite, clamped >=0. Stores pending target while depth hold disabled; updates active target while enabled. Disable edge clears pending target, matching Python. Only this explicit setter clamps to zero; ordinary desired-depth integration can become negative. ACK after application.

RAW_MOTORS 0x14, payload 16 bytes: 8xu16 internal 0..2000. Explicit manual calibration hardware-channel override, bypasses allocation/reorder/spin/power limits. Renews host lease. Next valid CONTROL exits override. Legacy 0x5A nonneutral packets are accepted only in unnegotiated legacy mode and never under the sticky maintenance latch. Negotiated calibration uses framed RAW. Neutral legacy packets always stop calculated output. Accepting RAW invalidates older queued CONTROL output; only a newer CONTROL can resume calculated output. Upload/recovery always outrank either mode.

ENTER_MAINTENANCE 0x25: empty payload, reliable. First send fresh framed RAW with all eight commands=1000. MCU inhibits calculated output and waits for neutral to be serviced (four DShot channel rounds or one PWM update; recovery/uninitialized outputs are already inhibited), then ACKs APPLIED and closes the extended session. A sticky maintenance latch survives session closure: nonneutral legacy5A and legacyC5 settings remain rejected; only neutral, info/capability and existing ESC E7/E8 maintenance operations are allowed. Active extended sessions reject legacy ESC/config commands, so corrupt/truncated extended tails cannot downgrade into those operations.

The Pi must suppress automatic HELLO/config/control synchronization throughout gate entry and ESC upload, including gaps before BEGIN, between chunks and after ABORT/COMPLETE. Upload success/abort does not restore motor authority. Exit requires explicit fresh HELLO, complete settings APPLIED and fresh valid CONTROL; failed flashing preserves the original recovery lockout and legacy recovery E7 remains usable. Exact retry of the successfully closed gate returns its cached ACK without re-execution until the next HELLO; afterward old-session gate retries are rejected. A gate timeout/disconnect never resumes previous targets or output. A hardware safety-timer starvation latch also poisons the failed session, cancels pending mutations with NOT_READY and enters sticky maintenance. Its session ID cannot be reused: wait for physical neutral, then choose a different HELLO session and reapply complete settings before fresh CONTROL. BUSY while neutral is still being serviced is not success. This prevents buffered pre-stall USB traffic from restoring authority.

## ESC power (mainboard)

Mainboard builds only (`MANAFISH_ESC_POWER_PIN`, GP14 = ESC_PWR_EN, `src/esc_power.*`). Pico/Pico 2 builds have no ESC power: no feature 0x20, 0x26 answers UNSUPPORTED, health bits 4-6 read 0, and outputs are never gated by it.

ESC_POWER 0x26, payload 4 bytes: on u32 (0 off, 1 on), reliable. The ACK comes at once (never pending). The 0x26 handler never answers NOT_READY, because the host drops its session on any NOT_READY; the generic session checks still do, so send 0x26 only in the active session. Generic STALE and in-flight BUSY apply as to every request. Results:

| Result | When |
|---|---|
| 0 APPLIED | on: switched on, or already starting, arming or on (repeating on is harmless). off: always, also when already off, except during ESC firmware recovery. |
| 3 BUSY | on: off hold not over, stopping, no settings applied since boot (extended COMMIT or legacy C5), protocol transition, ESC firmware upload or recovery. off: only while ESC firmware recovery is required (see ESC firmware update below). The host retries with a new request on its own timer; BUSY keeps the session. |
| 2 INVALID | payload not 4 bytes, or a value other than 0 and 1. |
| 6 UNSUPPORTED | build without ESC power. |

Timeline (constants in `src/esc_power.h`; the wire state is ATTITUDE health bits 4-6):

| Time | State (wire) | GP14 | Motor lines | Output |
|---|---|---|---|---|
| boot; after any off | OFF_HOLD (1) | low | parked | neutral |
| + 5 s (`OFF_HOLD_MS`) | OFF_READY (2) | low | parked | neutral |
| on applied, t0 | STARTING (3) | high | parked | neutral |
| t0 + 5 s (`SIGNAL_DELAY_MS`) | ARMING (3) | high | live; the protocol restarts on the freshly booted ESCs | neutral frames |
| t0 + 15 s (`THRUST_DELAY_MS`) | ON (4) | high | live | thrust permitted |
| off or host loss, t1 | STOPPING (5) | high | live | neutral frames |
| t1 + 2 s (`STOP_MS`) | lines parked, then GP14 low: OFF_HOLD (1) | low | parked | neutral |

An off during STARTING drops GP14 at once; the lines never went live. `SIGNAL_DELAY_MS` is tuned at bring-up B12 (measured ESC rail-up time plus 1 s). Parked lines sit on SIO, driven low: no waveform, no back-powering of the unpowered ESCs, no AM32 bootloader entry from an idle-high line. Settings keep reconciling while parked, and protocol initialization re-parks. Until ON, `thrust_permitted` holds every output path neutral (CONTROL, RAW, legacy 0x5A) and health bit2 stays clear; board current reads -1 (unavailable); ESC telemetry and quality warnings and ESC version discovery wait.

MCU guarantees: ESC power never switches on by itself; only an applied 0x26 on does. Nothing is persisted: every boot starts in OFF_HOLD. The first statement of `main()` drives GP14 low with 12 mA drive, before USB starts. There is no force-off: an off from ARMING or ON always runs the 2 s neutral STOPPING first. Host loss: 30 s (`HOST_LOSS_MS`) after the last host packet the MCU switches off as if 0x26 off had arrived. A host packet is every extended frame that decodes (length, version, CRC32C), also one the session rejects, and every valid legacy 0x5A, C5, E7 or E8 packet. The timer holds only while ESC firmware recovery is required, and restarts when an AM32 flash returns (it blocks core 0 for minutes). An upload does not hold it: every accepted E7 or E8 packet renews it, so a host lost mid-upload still switches ESC power off 30 s after its last packet. GP14 behaviour through a reset (watchdog reboot, RUN pulse from CM5 GPIO24) or BOOTSEL entry is to be confirmed at bring-up B11 (RP2350 pad isolation); until then assume ESC power can stay on while the MCU sends no DShot signal.

Host duties:

- Battery plug: only the CM5 sees a battery plug-in (LTC4162). Request on only after the battery has been present for at least 5 s, and only with the motors stopped (the switch has no current limit).
- Thrust: wait for state 4; output stays neutral before.
- Before flashing or resetting the MCU: neutral, send off, and wait for state 1 or 2 (or 5 s of MCU silence) before the GPIO24/GPIO25 reset or BOOTSEL. Never while ESC firmware recovery is required: off answers BUSY then, and a reset loses the MCU's recovery state (see ESC firmware update below).
- After a re-enumeration the MCU has rebooted into OFF_HOLD: show ESC power OFF and never re-request on automatically. On a reconnect without a reboot, adopt a reported 3 or 4 without sending on.
- ENTER_MAINTENANCE 0x25 closes the session, so a later 0x26 gets NOT_READY. Send every ESC_POWER request before the gate: an ESC firmware update needs state 4 at the gate, any other maintenance sends off first. After the gate, off needs a new HELLO, and HELLO answers BUSY while an upload is open (send E7 ABORT first); otherwise the host-loss timer switches off 30 s after the last E7, E8, C5 or 0x5A packet, also in the middle of an upload.
- Do not require feature 0x20.

ESC firmware update: E7 BEGIN, RECOVER_BEGIN and COMMIT need ESC power ON (state 4). Otherwise the MCU answers E9 status 7 FAILED with error 11 (`ESC_FIRMWARE_UPDATE_ERROR_ESC_POWER_OFF`); after a refused COMMIT the status value repeats whether recovery is still required (1 or 0), as a failed flash does. Error 11 never occurs on Pico/Pico 2 builds. A host must map error 11 before it updates ESCs on the mainboard.

ESC firmware recovery (a failed flash left an ESC partly programmed; the MCU reports recovery required): the ESCs wait in the AM32 bootloader on idle-high lines; an ESC that powers up on a low line starts its application instead. ESC power therefore stays on (state 4) until a recovery flash succeeds: 0x26 off answers BUSY and host loss does not switch off, so RECOVER_BEGIN and COMMIT always find state 4. The MCU keeps its recovery state in RAM only. An MCU reset meanwhile (watchdog, RUN pulse from CM5 GPIO24, BOOTSEL entry, or the magnet switch cutting 5 V) forgets the recovery state, and the reboot drops GP14. The next 0x26 on then powers the ESCs on parked (low) lines, the AM32 bootloader starts the partly programmed application, and ARMING sends it DShot frames. Hosts therefore never flash or reset the MCU while their recovery journal is set; how a host recovers ESCs after such a reset is an open question for the mainboard host (Phase 4).

## Telemetry

ATTITUDE 0x90 at 60Hz, payload 84 bytes: monotonic_us u64; current quaternion 4xf32; desired quaternion 4xf32; desired_depth f32; generation u32; last_control_sequence u32; health_flags u32; host_age_us u32; output_age_us u32; motor_commands 8xu16; work_percent u32 (pre-limit/pre-nullspace legacy indicator). Health bit0 IMU healthy, bit1 pressure healthy, bit2 actual output permitted by all gates, bit3 raw override, bits4-6 ESC power state (mainboard; 0 elsewhere): 0 absent, 1 off (hold), 2 off (ready), 3 starting or arming, 4 on, 5 stopping. Work is zero under raw override or any inhibited output, including stale input when power limits already make motors neutral. Quaternions begin at identity even while IMU is unhealthy. Header session active; telemetry sequence independent monotonically increasing.

STATS 0x91 every5s, payload 48 bytes: elapsed_us u64; completed_ahrs u32; completed_pid u32; completed_depth u32; missed_deadlines u32; average_execution_us u32; max_execution_us u32; sensor_errors u32; command_queue_overflows u32; max_host_age_us u32; max_output_age_us u32. Counts describe the elapsed window, not configured targets. AHRS counts fresh observed samples, PID counts enabled eligible attitude evaluations, depth counts accepted enabled command ticks; disabled/maintenance time is not reported as active PID execution. Existing framed INFO log reports measured rates/durations every5s. DShot per-channel output rate must be assessed independently from controller rate.

IMU 0x92 at60Hz: payload28 bytes, acceleration[3] f32 (m/s²), gyro[3] f32 (rad/s), temperature f32 (°C), board-defined sensor transform already applied (see below). Send the coherent sample corresponding to attitude processing; stale/failed sensor is reflected by ATTITUDE health, never synthesize raw IMU from quaternion.

## Controller C API and ownership

Pure module owns src/control/controller.h, src/control/controller.c and tests/test_controller.c only. No SDK, USB or logging dependencies. Header declares control_settings_t (roll,pitch,yaw,depth axis structs each kp/ki/kd/rate; bool fpv_mode; float power[3], coefficients[3], allocation[8][8]; uint8_t identifiers[8]; int8_t spin[8]; uint32_t nullspace_count; float nullspace[8][8]), control_state_t (fully defined static storage), control_sample_t (float accel[3],gyro[3]), control_output_t (float current_q[4],desired_q[4],desired_depth; uint16_t motors[8]; uint8_t work_percent).

Functions: void control_init(control_state_t *); bool control_validate_settings(const control_settings_t *); void control_apply_settings(control_state_t *, const control_settings_t *); void control_pressure(control_state_t *, float depth, float depth_change); void control_command(control_state_t *, const float direction[8], float source_dt, bool stabilization, bool depth_hold); bool control_set_attitude(control_state_t *, const float q[4]); void control_set_depth(control_state_t *, float depth); void control_step(control_state_t *, const control_sample_t *, float dt, control_output_t *). Additional lifecycle helpers in controller.h provide AHRS-only observation while control is inhibited and session reset preserving the physical attitude estimator.

control_command performs edge handling then target updates and depth PID exactly once. control_step runs AHRS/attitude PID/allocation at500Hz with 500Hz dt clamp (not legacy8.33ms minimum). Nullspace decay .001 permitted once per command, avoidance solving every step. Preserve gyro derivative copy BEFORE AHRS whole-sample rejection >1080deg/s. Preserve Mahony body-to-world right rotation-vector integration, NED up [0,0,-1], shortest rotation-vector PID, integral freeze threshold .2, component clips100deg, PID output /10, sequential exact nullspace ordering/ties, normal reorder then spin. No auto-tune implementation required. The board-defined sensor transform happens in the hardware wrapper (`src/imu/bmi270_sensor.c`) before the pure sample: output axis X/Y/Z = `MANAFISH_IMU_SIGN_{X,Y,Z}` (+1 or -1) times raw sensor axis `MANAFISH_IMU_AXIS_{X,Y,Z}` (a permutation of 0..2), for accel and gyro alike. Defaults are upstream's axes 0,1,2 with signs [+1,-1,-1]; a board header overrides them, and `boards/manafish_mainboard.h` keeps the defaults until bring-up B13 measures the mainboard. Frame FRD: level reads accel about (0, 0, -9.81). Safety/health/leases/raw override remain integration owner, not pure module.

Core1 owns SPI and pure state; core0 owns USB and physical outputs. Bounded cross-core FIFO preserves all state-changing commands. Settings ACK after core1 application. Core0 applies independent 200ms host and 10ms controller-output gates. A 1ms hardware timer also detects a stalled core0: PWM is set to neutral, while DShot GPIO is forced low until normal code can safely resume neutral frames. A 1s hardware watchdog resets a fully stalled system. Forced-low DShot is signal inhibition, not proof of an ESC accepting a neutral frame; actual ESC failsafe response remains a powered-hardware gate. Neither generated output nor pressure renews host lease. Upload pauses control authority and preserves existing neutral/startup/recovery sequences.
