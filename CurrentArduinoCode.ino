#include <SPI.h>
#include <UIPEthernet.h>
#include <SoftwareSerial.h>

// ---------- RS-485 pins ----------
#define DE 3
#define RE 4
#define RS485_RX 8  // RO
#define RS485_TX 9  // DI
SoftwareSerial RS485Serial(RS485_RX, RS485_TX);  // RX, TX

// ---------- Ethernet (ENC28J60) ----------
const uint8_t ENC28J60_CS = 10;  // D10 = CS (SPI uses D11/D12/D13)
byte mac[] = { 0xDE, 0xAD, 0xBE, 0xEF, 0xFE, 0xED };
IPAddress ip(192, 168, 2, 70);
IPAddress dnsServer(192, 168, 2, 1);
IPAddress gateway(192, 168, 2, 1);
IPAddress subnet(255, 255, 255, 0);

const uint16_t PORT = 9000;
EthernetServer server(PORT);

// ---------- State ----------
uint8_t strobeIntensity = 0;  // 0..100
uint8_t lampIntensity   = 0;  // 0..100
bool lampOn = false;

// ====== Camera Focus + Trigger ======
const int cameraFocusPin   = 5; // FOCUS line (free)
const int cameraTriggerPin = 6; // TRIGGER line

// Pull FOCUS low, then TRIGGER low, hold for press_us using micros(), then release TRIGGER then FOCUS (to HIGH-Z).
void cameraTrigger_us(unsigned long press_us) {
  // Engage focus + trigger
  pinMode(cameraFocusPin, OUTPUT);
  digitalWrite(cameraFocusPin, LOW);

  pinMode(cameraTriggerPin, OUTPUT);
  digitalWrite(cameraTriggerPin, LOW);

  // Measure hold time *after* both are low
  unsigned long t0 = micros();
  while ((unsigned long)(micros() - t0) < press_us) {
    // busy-wait; press duration accurate to a few µs (AVR ISR jitter aside)
  }

  // Release trigger then focus (open switch behavior)
  pinMode(cameraTriggerPin, INPUT); // high-Z
  pinMode(cameraFocusPin,  INPUT);  // high-Z
}

// Millisecond wrapper (maintains your existing API)
void cameraTrigger(unsigned long press_ms = 1000) {
  cameraTrigger_us(press_ms * 1000UL);
}

// ---------- Forward decl ----------
void rs485_send_line(const String &s);
void rs485_poll(EthernetClient *client);

// ---------- TCP helpers ----------
void sendLine(EthernetClient &c, const char *s) {
  c.write((const uint8_t *)s, strlen(s));
  c.write('\n');
}

// Forward any line beginning with '~' over RS-485.
bool maybe_forward_rs485(const String &cmd, EthernetClient &client) {
  if (cmd.length() && (cmd.charAt(0) == '~' || cmd.charAt(0) == '$')) {
    rs485_send_line(cmd);
    sendLine(client, "OK FORWARDED");
    return true;
  }
  return false;
}

// ===== RS-485 MONITOR =====
void rs485_poll(EthernetClient *client) {
  static char    buf[160];
  static uint8_t idx = 0;
  static uint32_t lastByteMs = 0;
  const uint16_t idleFlushMs = 30;  // flush partial line after 30ms idle

  while (RS485Serial.available()) {
    int b = RS485Serial.read();
    if (b < 0) break;
    lastByteMs = millis();

    char ch = (char)b;

    if (ch == '\r' || ch == '\n') {
      if (idx > 0) {
        buf[idx] = '\0';
        if (client && client->connected()) {
          String out = String("RS485: ") + buf;
          sendLine(*client, out.c_str());
        }
        idx = 0;
      }
      continue;
    }

    if (idx < sizeof(buf) - 1) {
      buf[idx++] = ch;
    } else {
      buf[idx] = '\0';
      if (client && client->connected()) {
        String out = String("RS485: ") + buf;
        sendLine(*client, out.c_str());
      }
      idx = 0;
    }
  }

  if (idx > 0 && (millis() - lastByteMs) > idleFlushMs) {
    buf[idx] = '\0';
    if (client && client->connected()) {
      String out = String("RS485: ") + buf;
      sendLine(*client, out.c_str());
    }
    idx = 0;
  }
}

void handleCommand(const String &line, EthernetClient &client) {
  String cmd = line;
  cmd.trim();
  if (cmd.length() == 0) return;

  if (maybe_forward_rs485(cmd, client)) return;

  // ====== Trigger commands (FOCUS+TRIGGER with micros) ======
  if (cmd.equalsIgnoreCase("TRIGGER")) {
    cameraTrigger(1000); // 1s
    sendLine(client, "OK TRIGGERED");
    return;
  }
  if (cmd.startsWith("TRIGGER_MS")) {
    int sep = cmd.indexOf(' ');
    if (sep > 0) {
      long ms = cmd.substring(sep + 1).toInt();
      if (ms > 0 && ms <= 10000) {
        cameraTrigger_us((unsigned long)ms * 1000UL);  // micros-accurate hold
        sendLine(client, "OK TRIGGERED");
      } else {
        sendLine(client, "ERR TRIGGER_MS OUT OF RANGE (1..10000)");
      }
    } else {
      sendLine(client, "ERR TRIGGER_MS NEEDS VALUE");
    }
    return;
  }

  // ----- Other commands (unchanged) -----
  if (cmd.equalsIgnoreCase("LAMP OFF")) {
    String data = "~device set lamp:000|SUBC24991";
    rs485_send_line(data);
    sendLine(client, "OK LAMP OFF");
    return;
  }

  if (cmd.startsWith("STROBE_INTENSITY")) {
    int sep = cmd.indexOf(' ');
    if (sep > 0) {
      int v = cmd.substring(sep + 1).toInt();
      if (v >= 0 && v <= 100) {
        char buf[50];
        sprintf(buf, "~device set strobe:%03d|SUBC24991", v);
        rs485_send_line(String(buf));
        strobeIntensity = (uint8_t)v;
        sendLine(client, "OK STROBE_INTENSITY");
      } else {
        sendLine(client, "ERR STROBE_INTENSITY OUT OF RANGE (0-100)");
      }
    } else {
      sendLine(client, "ERR STROBE_INTENSITY NEEDS VALUE");
    }
    return;
  }

  if (cmd.startsWith("LAMP_INTENSITY")) {
    int sep = cmd.indexOf(' ');
    if (sep > 0) {
      int v = cmd.substring(sep + 1).toInt();
      if (v >= 0 && v <= 100) {
        char buf[50];
        sprintf(buf, "~device set lamp:%03d|SUBC24991", v);
        rs485_send_line(String(buf));
        lampIntensity = (uint8_t)v;
        sendLine(client, "OK LAMP_INTENSITY");
      } else {
        sendLine(client, "ERR LAMP_INTENSITY OUT OF RANGE (0-100)");
      }
    } else {
      sendLine(client, "ERR LAMP_INTENSITY NEEDS VALUE");
    }
    return;
  }

  if (cmd.equalsIgnoreCase("STATUS")) {
    String data = "~comms print status|SUBC24991";
    rs485_send_line(data);
    sendLine(client, "OK STATUS");
    return;
  }

  sendLine(client, "UNKNOWN CMD");
}

// ---------- RS-485 helpers ----------
void rs485_send_line(const String &s) {
  digitalWrite(RE, HIGH);
  digitalWrite(DE, HIGH);
  delayMicroseconds(5);
  RS485Serial.print(s);
  RS485Serial.print("\r\n");
  RS485Serial.flush();
  delayMicroseconds(5);
  digitalWrite(DE, LOW);
  digitalWrite(RE, LOW);
}

void setup() {
  Serial.begin(9600);

  // RS-485 setup
  RS485Serial.begin(9600);
  pinMode(DE, OUTPUT);
  pinMode(RE, OUTPUT);
  digitalWrite(DE, LOW);
  digitalWrite(RE, LOW);

  // Ethernet setup
  pinMode(ENC28J60_CS, OUTPUT);
  digitalWrite(ENC28J60_CS, HIGH);
  Ethernet.init(ENC28J60_CS);
  Ethernet.begin(mac, ip, dnsServer, gateway, subnet);
  server.begin();

  Serial.print(F("IP: "));      Serial.println(Ethernet.localIP());
  Serial.print(F("TCP server listening on port ")); Serial.println(PORT);
  Serial.println(F("Commands: ~... | LAMP OFF | STROBE_INTENSITY <0..100> | LAMP_INTENSITY <0..100> | STATUS"));
  Serial.println(F("          TRIGGER | TRIGGER_MS <ms> (FOCUS precedes TRIGGER; micros-accurate hold)"));

  // Ensure camera lines idle high-Z
  pinMode(cameraFocusPin, INPUT);
  pinMode(cameraTriggerPin, INPUT);
}

void loop() {
  rs485_poll(nullptr);

  EthernetClient client = server.available();
  if (!client) return;

  String line = "";
  while (client.connected()) {
    while (client.available()) {
      char ch = client.read();
      if (ch == '\n') {
        handleCommand(line, client);
        line = "";
      } else if (ch != '\r') {
        if (line.length() < 120) line += ch;
      }
    }
    rs485_poll(&client);
  }
  client.stop();
}
