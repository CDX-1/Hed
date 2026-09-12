// MPU-6050 motion source for headtrack.py.
//
// Streams raw 16-bit accelerometer and gyroscope counts over USB serial as CSV:
//
//     ax,ay,az,gx,gy,gz,micros
//
// Nothing is fused here. Gyro bias calibration, the Mahony filter and the axis
// remap all live on the host, so remounting the board on the headphones is a
// command-line flag rather than a reflash. Lines starting with '#' are info the
// host reads at startup (scale factors) - it ignores anything it cannot parse,
// which covers the ESP32 bootloader chatter after a reset.

#include <Wire.h>

const uint8_t MPU_ADDR = 0x68;   // AD0 tied to GND
const int SDA_PIN = 33;
const int SCL_PIN = 32;

// Registers
const uint8_t REG_SMPLRT_DIV   = 0x19;
const uint8_t REG_CONFIG       = 0x1A;
const uint8_t REG_GYRO_CONFIG  = 0x1B;
const uint8_t REG_ACCEL_CONFIG = 0x1C;
const uint8_t REG_ACCEL_CONFIG2 = 0x1D;   // 6500/9250 only
const uint8_t REG_ACCEL_XOUT_H = 0x3B;
const uint8_t REG_PWR_MGMT_1   = 0x6B;
const uint8_t REG_WHO_AM_I     = 0x75;

// +/-2 g keeps the best resolution: a head never pulls much more than 1 g on
// top of gravity. +/-500 deg/s is the one that matters - a quick head turn
// clips the 250 deg/s default and the yaw integration eats the error.
const uint8_t ACCEL_FS_SEL = 0;   // 0:2g  1:4g  2:8g  3:16g
const uint8_t GYRO_FS_SEL  = 1;   // 0:250 1:500 2:1000 3:2000 deg/s
const uint16_t ACCEL_G[]   = {2, 4, 8, 16};
const uint16_t GYRO_DPS[]  = {250, 500, 1000, 2000};

const uint16_t RATE_HZ = 100;     // gyro output is 1 kHz with the DLPF on

uint32_t nextSample = 0;
uint8_t buf[14];
bool deviceOk = false;
uint32_t nextProbe = 0;
uint32_t nextHeader = 0;
const char *partLabel = "unknown";
int whoami = -1;

void writeReg(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(value);
  Wire.endTransmission(true);
}

int readReg(uint8_t reg) {   // -1 when nothing answers on the bus
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -1;
  if (Wire.requestFrom((int)MPU_ADDR, 1, true) != 1) return -1;
  return Wire.read();
}

// WHO_AM_I is not a "is this the right part" check so much as a "did anything
// answer" one. Boards sold as MPU-6050 are often an MPU-6500 or a 9250, which
// report something else here and drive the registers this sketch uses in
// exactly the same way - so an unfamiliar value is worth naming, not refusing.
const char *partName(int who) {
  switch (who) {
    case 0x68: case 0x69: return "MPU-6050";
    case 0x70: return "MPU-6500";
    case 0x71: return "MPU-9250";
    case 0x73: return "MPU-9255";
    case 0x75: return "MPU-6555";
    case 0xAC: return "ICM-20608";
    case 0xAF: return "ICM-20602";
    default:   return "unknown";
  }
}

// The host may attach at any point - it does not reset us on connect, and
// pyserial throws away whatever is already buffered when it opens the port. So
// a header printed once at power-up is a header the host will usually miss:
// repeat it on request ('?') and every few seconds regardless.
void printHeader() {
  Serial.print("#mpu part=");        Serial.print(partLabel);
  Serial.print(" whoami=0x");        Serial.print(whoami, HEX);
  Serial.print(" rate=");            Serial.print(RATE_HZ);
  Serial.print(" accel_fs=");        Serial.print(ACCEL_G[ACCEL_FS_SEL]);
  Serial.print(" gyro_fs=");         Serial.println(GYRO_DPS[GYRO_FS_SEL]);
  Serial.println("#fields ax,ay,az,gx,gy,gz,micros");
  nextHeader = millis() + 5000;
}

void setup() {
  Serial.begin(115200);
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(400000);           // 100 kHz cannot keep up with 100 Hz bursts

  delay(50);
  int who = readReg(REG_WHO_AM_I);
  if (who < 0) {
    Serial.println("#error nothing answered at I2C address 0x68");
    return;                        // loop() keeps retrying
  }
  const char *part = partName(who);
  deviceOk = true;

  // Clock off the gyro X PLL rather than the internal 8 MHz oscillator: the
  // oscillator drifts with temperature, and every integrated angle drifts with
  // it. This also clears the sleep bit the MPU powers up with.
  writeReg(REG_PWR_MGMT_1, 0x01);
  delay(10);
  writeReg(REG_CONFIG, 0x03);      // DLPF: 44 Hz gyro (and accel, on a 6050)
  writeReg(REG_GYRO_CONFIG, GYRO_FS_SEL << 3);
  writeReg(REG_ACCEL_CONFIG, ACCEL_FS_SEL << 3);
  // On the 6500 and its relatives CONFIG only filters the gyro; the
  // accelerometer has its own filter in a register the 6050 does not have. Skip
  // this on a real 6050 - 0x1D is undocumented there.
  if (who == 0x70 || who == 0x71 || who == 0x73 || who == 0x75) {
    writeReg(REG_ACCEL_CONFIG2, 0x03);   // 44.8 Hz
  }
  writeReg(REG_SMPLRT_DIV, (1000 / RATE_HZ) - 1);
  delay(50);

  partLabel = part;
  whoami = who;
  printHeader();

  nextSample = micros();
}

void loop() {
  // Nothing on the bus at boot: re-probe once a second, so plugging the sensor
  // in (or reseating a wire) recovers without a reset.
  if (!deviceOk) {
    if (millis() >= nextProbe) {
      nextProbe = millis() + 1000;
      if (readReg(REG_WHO_AM_I) >= 0) setup();
    }
    return;
  }

  while (Serial.available()) {
    if (Serial.read() == '?') printHeader();
  }
  if (millis() >= nextHeader) printHeader();

  uint32_t now = micros();
  if ((int32_t)(now - nextSample) < 0) return;
  nextSample += 1000000UL / RATE_HZ;
  // If something stalled us for longer than a whole period, don't try to catch
  // up with a burst - the host times its integration off these stamps.
  if ((int32_t)(micros() - nextSample) > 0) nextSample = micros();

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_XOUT_H);
  if (Wire.endTransmission(false) != 0) return;
  if (Wire.requestFrom((int)MPU_ADDR, 14, true) != 14) return;
  for (int i = 0; i < 14; i++) buf[i] = Wire.read();

  int16_t ax = (buf[0]  << 8) | buf[1];
  int16_t ay = (buf[2]  << 8) | buf[3];
  int16_t az = (buf[4]  << 8) | buf[5];
  // buf[6..7] is temperature, skipped
  int16_t gx = (buf[8]  << 8) | buf[9];
  int16_t gy = (buf[10] << 8) | buf[11];
  int16_t gz = (buf[12] << 8) | buf[13];

  char line[64];
  int n = snprintf(line, sizeof(line), "%d,%d,%d,%d,%d,%d,%lu\n",
                   ax, ay, az, gx, gy, gz, (unsigned long)now);
  Serial.write((const uint8_t *)line, n);
}
