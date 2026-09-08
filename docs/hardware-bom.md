# Hardware BOM / lab pull list (Day-0 inventory, not a purchase order)

Tick "preferred" if the lab has it, else use fallback. Anything in neither column → order same-day (lead time, not cost, is the risk).

| Subsystem | Preferred | Fallback |
|---|---|---|
| Planner brain | Raspberry Pi 4/5 + fresh SD + USB mic + camera | (no autonomous nav without it → teleop-only build) |
| Reflex brain | ESP32 DevKit x2 (1 spare) | Arduino Mega + Wi-Fi via Pi |
| Drive | Encoder gear motors (N20/JGA25) + TB6612 | TT motors + L298N (no PID/odometry) |
| Chassis | Aluminium kit + 3D-printed deck | Acrylic kit + cardboard deck |
| Camera | Pi Camera v2/v3 or USB webcam | phone-as-webcam (last resort) |
| Nav markers | Printed matte ArUco ~120-150 mm on stands | (markers ARE the nav plan) |
| Obstacle | HC-SR04 (or ToF VL53L0X) | HC-SR04 |
| Auth | RC522 + >=8 MIFARE tags | same |
| Dispenser | 3D-printed magazine + MG90S metal-gear servo | PET-bottle tube + SG90 |
| Cold box | Insulated box + 5V solenoid latch + DS18B20 | Plastic box + SG90 latch + DS18B20 |
| Sound | USB microphone (Pi) | MAX9814 -> ESP32 ADC |
| Power | 2S/3S LiPo + 2x 5V/3A UBEC + charger + LiPo bag | 18650 x2 + buck |
| Safety | Panel-mount E-stop in motor path | big rocker in motor path |
| Course | Matte marker prints + stands + tape measure | same |
| Spares (Day 12) | spare ESP32, servo, motor, fuses, printed markers, charged batteries, USB cables | - |
