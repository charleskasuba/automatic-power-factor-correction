#include <WiFi.h>
#include <HTTPClient.h>
#include <PZEM004Tv30.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <cmath>

// --- WiFi & Server Configurations ---
const char* WIFI_SSID = "THE METHOD ZONE";
const char* WIFI_PASS = "Chabu321+";
const char* SERVER_URL = "https://smartmeter-isps.onrender.com/api/data";

// --- Hardware Pin Definitions ---
const int SEC_VOLTAGE_PIN = 32;    // ZMPT101B Analog Pin
const int SEC_CURRENT_PIN = 33;    // ACS712 Analog Pin
const int RELAY_8UF_PIN = 18;      // Relay 1 (8uF Capacitor)
const int RELAY_3UF_PIN = 19;      // Relay 2 (3uF Capacitor)
const int RELAY_3UF_B_PIN = 5;     // Relay 3 (3uF Capacitor)

// --- Target Settings ---
const float TARGET_PF = 0.95;       // Desired minimum power factor
const float MIN_POWER_WATT = 15.0;  // Minimum active power to initiate correction

// --- Sensor Instantiations ---
LiquidCrystal_I2C lcd(0x27, 20, 4); 

// Meter Module configured with RX=26, TX=25
PZEM004Tv30 pzemModule(Serial2, 26, 25);

// --- Global Variables ---
float voltage = 0, current = 0, active_power = 0, reactive_power = 0, pf = 0;
float analog_voltage = 0, analog_current = 0;
int activeStep = 0; // 0=None, 1=3uF, 2=3uF, 3=6uF, 4=8uF, 5=11uF, 6=14uF

// --- Timing Variables ---
unsigned long lastSend = 0;
unsigned long lastWiFiCheck = 0;
unsigned long lastDisplaySwitch = 0;
int displayPage = 0; 

// --- Function Prototypes ---
float readAnalogVoltage();
float readAnalogCurrent();
void adjustPowerFactor(float currentPF, float currentPower);
void setCapacitorStep(int step);

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n==============================================");
  Serial.println("SYSTEM SYSTEM - CORE ACTIVE");
  Serial.println("==============================================");

  // Configure Relay Pins
  pinMode(RELAY_8UF_PIN, OUTPUT);
  pinMode(RELAY_3UF_PIN, OUTPUT);
  pinMode(RELAY_3UF_B_PIN, OUTPUT);
  
  // Initialize Relays to OFF (Assuming Active LOW relay boards; use HIGH for OFF)
  digitalWrite(RELAY_8UF_PIN, HIGH); 
  digitalWrite(RELAY_3UF_PIN, HIGH);
  digitalWrite(RELAY_3UF_B_PIN, HIGH);

  pinMode(SEC_VOLTAGE_PIN, INPUT);
  pinMode(SEC_CURRENT_PIN, INPUT);

  Wire.begin(21, 22); 
  lcd.init();
  lcd.backlight();
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("System Initializing");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 15) {
    delay(500);
    attempts++;
  }
  
  lcd.clear();
}

void loop() {
  unsigned long now = millis();

  // --- WIFI RECOVERY ---
  if (now - lastWiFiCheck >= 10000) {
    lastWiFiCheck = now;
    if (WiFi.status() != WL_CONNECTED) {
      WiFi.reconnect();
    }
  }

  // --- DATA SAMPLING & API DELIVERY (Every 3 seconds) ---
  if (now - lastSend >= 3000) {
    lastSend = now;

    // 1. Read Core Metrics
    voltage = pzemModule.voltage();
    current = pzemModule.current();
    active_power = pzemModule.power();
    pf = pzemModule.pf();
    
    if (isnan(voltage)) { 
      voltage = 0; current = 0; active_power = 0; pf = 0; reactive_power = 0; 
    } else if (pf > 0.0 && pf < 1.0) {
      // Calculate Reactive Power dynamically: Q = P * tan(acos(PF))
      reactive_power = active_power * tan(acos(pf));
    } else {
      reactive_power = 0.0;
    }

    // 2. Read Monitor Metrics
    analog_voltage = readAnalogVoltage();
    analog_current = readAnalogCurrent();

    // 3. Run Automatic Power Factor Correction switching 
    adjustPowerFactor(pf, active_power);

    // --- PRINT REVISED DIAGNOSTIC REPORT ---
    Serial.println("\n--- SYSTEM MEASUREMENT REPORT ---");
    Serial.printf("Line Metrics:     V=%.1fV | I=%.2fA | P_Act=%.1fW | P_React=%.1fVAR | PF=%.2f\n", voltage, current, active_power, reactive_power, pf);
    Serial.printf("Monitor Line:     V=%.1fV | I=%.2fA\n", analog_voltage, analog_current);
    Serial.printf("Relay Status:     Step=%d | R1(8uF)=%s R2(3uF)=%s R3(3uF)=%s\n", activeStep, digitalRead(RELAY_8UF_PIN) == LOW ? "ON" : "OFF", digitalRead(RELAY_3UF_PIN) == LOW ? "ON" : "OFF", digitalRead(RELAY_3UF_B_PIN) == LOW ? "ON" : "OFF");
    Serial.println("-------------------------------------------------");

    // Send API Payload
    if (WiFi.status() == WL_CONNECTED) {
      // Determine individual relay states from activeStep
      bool relay1_on = (activeStep >= 4);
      bool relay2_on = (activeStep == 1 || activeStep == 3 || activeStep >= 5);
      bool relay3_on = (activeStep == 2 || activeStep == 3 || activeStep == 6);

      String json = "{\"voltage\":" + String(voltage, 1) + 
                    ",\"current\":" + String(current, 2) + 
                    ",\"active_power\":" + String(active_power, 1) + 
                    ",\"reactive_power\":" + String(reactive_power, 1) + 
                    ",\"pf\":" + String(pf, 2) + 
                    ",\"analog_voltage\":" + String(analog_voltage, 1) + 
                    ",\"analog_current\":" + String(analog_current, 2) + 
                    ",\"relay_status\":" + String(activeStep) + 
                    ",\"relay1\":" + (relay1_on ? "true" : "false") + 
                    ",\"relay2\":" + (relay2_on ? "true" : "false") + 
                    ",\"relay3\":" + (relay3_on ? "true" : "false") + "}";
      HTTPClient http;
      http.begin(SERVER_URL);
      http.addHeader("Content-Type", "application/json");
      http.POST(json);
      http.end();
    }
  }

  // --- CLEAN LCD ROTATION LOGIC (Every 5 seconds with precise formatting) ---
  if (now - lastDisplaySwitch >= 5000) {
    lastDisplaySwitch = now;
    char rowBuffer[21]; 

    switch (displayPage) {
      case 0: // PAGE 1: CORE POWER METRICS
        lcd.setCursor(0, 0); lcd.print("--- POWER METRICS --");
        snprintf(rowBuffer, sizeof(rowBuffer), "Voltage: %5.1f V   ", voltage); lcd.setCursor(0, 1); lcd.print(rowBuffer);
        snprintf(rowBuffer, sizeof(rowBuffer), "Current: %5.2f A   ", current); lcd.setCursor(0, 2); lcd.print(rowBuffer);
        snprintf(rowBuffer, sizeof(rowBuffer), "Active P: %4.1f W ", active_power); lcd.setCursor(0, 3); lcd.print(rowBuffer);
        displayPage = 1; 
        break;

      case 1: // PAGE 2: COMPLEMENTARY MONITOR LINE
        lcd.setCursor(0, 0); lcd.print("--- MONITOR LINE ---");
        snprintf(rowBuffer, sizeof(rowBuffer), "Voltage: %5.1f V   ", analog_voltage); lcd.setCursor(0, 1); lcd.print(rowBuffer);
        snprintf(rowBuffer, sizeof(rowBuffer), "Current: %5.2f A   ", analog_current); lcd.setCursor(0, 2); lcd.print(rowBuffer);
        snprintf(rowBuffer, sizeof(rowBuffer), "                    ");                lcd.setCursor(0, 3); lcd.print(rowBuffer);
        displayPage = 2;
        break;

      case 2: // PAGE 3: APFC MANAGEMENT STATUS
        lcd.setCursor(0, 0); lcd.print("--- APFC STATUS ----");
        snprintf(rowBuffer, sizeof(rowBuffer), "PF Meas:   %5.2f     ", pf);            lcd.setCursor(0, 1); lcd.print(rowBuffer);
        snprintf(rowBuffer, sizeof(rowBuffer), "Reactive: %5.1f VAR ", reactive_power); lcd.setCursor(0, 2); lcd.print(rowBuffer);
        
        const char* capLabel = "0uF          ";
        if(activeStep == 1)      capLabel = "3uF (R2)     ";
        else if(activeStep == 2) capLabel = "3uF (R3)     ";
        else if(activeStep == 3) capLabel = "6uF (R2+R3)  ";
        else if(activeStep == 4) capLabel = "8uF (R1)     ";
        else if(activeStep == 5) capLabel = "11uF (R1+R2) ";
        else if(activeStep == 6) capLabel = "14uF (All)   ";
        
        snprintf(rowBuffer, sizeof(rowBuffer), "Relay Status: %s", capLabel); lcd.setCursor(0, 3); lcd.print(rowBuffer);
        displayPage = 0;  
        break;
    }
  }
}

void adjustPowerFactor(float currentPF, float currentPower) {
  if (currentPower < MIN_POWER_WATT) {
    if (activeStep != 0) setCapacitorStep(0);
    return;
  }

  // If PF is lower than target, step up capacity smoothly
  if (currentPF < TARGET_PF && currentPF > 0.1) {
    if (activeStep < 6) {
      activeStep++;
      setCapacitorStep(activeStep);
      delay(600); 
    }
  }
}

void setCapacitorStep(int step) {
  activeStep = step;
  switch(step) {
    case 0: // 0 uF - all OFF
      digitalWrite(RELAY_8UF_PIN, HIGH); digitalWrite(RELAY_3UF_PIN, HIGH); digitalWrite(RELAY_3UF_B_PIN, HIGH);
      break;
    case 1: // 3 uF - relay2 only
      digitalWrite(RELAY_8UF_PIN, HIGH); digitalWrite(RELAY_3UF_PIN, LOW);  digitalWrite(RELAY_3UF_B_PIN, HIGH);
      break;
    case 2: // 3 uF - relay3 only
      digitalWrite(RELAY_8UF_PIN, HIGH); digitalWrite(RELAY_3UF_PIN, HIGH); digitalWrite(RELAY_3UF_B_PIN, LOW);
      break;
    case 3: // 6 uF - relay2 + relay3
      digitalWrite(RELAY_8UF_PIN, HIGH); digitalWrite(RELAY_3UF_PIN, LOW);  digitalWrite(RELAY_3UF_B_PIN, LOW);
      break;
    case 4: // 8 uF - relay1 only
      digitalWrite(RELAY_8UF_PIN, LOW);  digitalWrite(RELAY_3UF_PIN, HIGH); digitalWrite(RELAY_3UF_B_PIN, HIGH);
      break;
    case 5: // 11 uF - relay1 + relay2
      digitalWrite(RELAY_8UF_PIN, LOW);  digitalWrite(RELAY_3UF_PIN, LOW);  digitalWrite(RELAY_3UF_B_PIN, HIGH);
      break;
    case 6: // 14 uF - all relays
      digitalWrite(RELAY_8UF_PIN, LOW);  digitalWrite(RELAY_3UF_PIN, LOW);  digitalWrite(RELAY_3UF_B_PIN, LOW);
      break;
  }
}
}

float readAnalogVoltage() {
  int maxValue = 0, minValue = 4095;
  unsigned long start_time = millis();
  while (millis() - start_time < 50) {
    int readVal = analogRead(SEC_VOLTAGE_PIN);
    if (readVal > maxValue) maxValue = readVal;
    if (readVal < minValue) minValue = readVal;
  }
  float voltageValuePP = ((maxValue - minValue) * 3.3) / 4095.0;
  float rmsVoltage = (voltageValuePP / 2.0) * 0.707 * 18.5; 
  return (rmsVoltage < 1.0) ? 0.0 : rmsVoltage; 
}

float readAnalogCurrent() {
  int maxValue = 0, minValue = 4095;
  unsigned long start_time = millis();
  while (millis() - start_time < 50) {
    int readVal = analogRead(SEC_CURRENT_PIN);
    if (readVal > maxValue) maxValue = readVal;
    if (readVal < minValue) minValue = readVal;
  }
  float voltageValuePP = ((maxValue - minValue) * 3.3) / 4095.0;
  float rmsCurrent = ((voltageValuePP / 2.0) * 0.707) / 0.185;
  return (rmsCurrent < 0.08) ? 0.0 : rmsCurrent; 
}