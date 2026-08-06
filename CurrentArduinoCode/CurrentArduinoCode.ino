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

// ============================================================
//  Camera Focus + Trigger  (Sony ILX-LR1)
// ============================================================
//  Molex Micro-Fit control terminal:
//    pin 4 FOCUS    - input to camera, pull to GND to focus (S1)
//    pin 5 TRIGGER  - input to camera, pull to GND to shoot (S2); FOCUS must be low first
//
//  FOCUS/TRIGGER idle state inside the camera is 3.15 V via 31k. Drive them
//  OPEN-DRAIN only: OUTPUT-LOW to assert, INPUT (high-Z) to release. Never drive HIGH.
//
//  Camera settings that must be right for AF to work each shot:
//    - AF/MF switch on the lens set to AF
//    - Focus Mode = Continuous AF (AF-C)
//    - AF w/ Shutter = On   (so the FOCUS line actually commands AF)
//    - Pre-AF = Off         (so only the FOCUS line drives focus)
// ------------------------------------------------------------

const int cameraFocusPin   = 5;  // FOCUS   (connector pin 4)
const int cameraTriggerPin = 6;  // TRIGGER (connector pin 5)

const unsigned long cameraFocusLeadMs   = 500; // AF convergence time
const unsigned long cameraTriggerHoldMs = 60;  // S2 pulse width (spec min 4 ms)
const unsigned long cameraFocusTailMs   = 20;  // keep focus asserted briefly past the shot

// *** REFOCUS FIX ***
// Guaranteed high-Z (S1 released) time before every acquisition, so the camera
// registers a real focus release and re-arms AF each cycle. Raise to 400-500 if
// it still won't refocus.
const unsigned long cameraReleaseGapMs  = 300;

// Timestamp (ms) of the last genuine FOCUS release. Refocus gap is measured from here.
unsigned long lastFocusReleaseMs = 0;

void cameraFocusBegin() {
  pinMode(cameraFocusPin, OUTPUT);
  digitalWrite(cameraFocusPin, LOW);   // S1 asserted
}

void cameraFocusEnd() {
  pinMode(cameraFocusPin, INPUT);      // high-Z: S1 released
  lastFocusReleaseMs = millis();       // start the refocus gap clock from HERE
}

// Runs one full FOCUS -> TRIGGER -> release cycle.
void cameraTriggerCycle(unsigned long holdMs      = cameraTriggerHoldMs,
                        unsigned long focusLeadMs = cameraFocusLeadMs) {
  // 1) Ensure FOCUS has been released long enough since the last shot so AF re-arms.
  //    FOCUS is already high-Z here, so this is genuine S1-released time.
  while ((unsigned long)(millis() - lastFocusReleaseMs) < cameraReleaseGapMs) {
    /* wait: let the camera fully release S1 */
  }

  // 2) Assert FOCUS and give AF time to converge.
  cameraFocusBegin();
  delay(focusLeadMs);

  // 3) Fire TRIGGER.
  pinMode(cameraTriggerPin, OUTPUT);
  digitalWrite(cameraTriggerPin, LOW);   // S2 asserted
  delay(holdMs);
  pinMode(cameraTriggerPin, INPUT);      // S2 released

  // 4) Hold focus a moment past the shot, then release (stamps lastFocusReleaseMs).
  delay(cameraFocusTailMs);
  cameraFocusEnd();
}

// Discard any bytes that queued up on this client while we were busy capturing.
// This stops a backlog from building when the host fires triggers faster than a
// capture takes, so pressing "Stop Loop" on the host takes effect promptly instead
// of the Arduino draining a pile of already-buffered TRIGGER commands.
// NOTE: this is a blunt instrument - it also discards any non-trigger commands
// (lamp/strobe/status) that happened to arrive during the capture window.
void drainClient(EthernetClient &client) {
  while (client.connected() && client.available()) {
    client.read();
  }
}

// ---------- Forward decl ----------
void rs485_send_line(const String &s);
void rs485_poll(EthernetClient *client);

// ---------- TCP helpers ----------
void sendLine(EthernetClient &c, const char *s) {
  c.write((const uint8_t *)s, strlen(s));
  c.write('\n');
}

// Forward any line beginning with '~' or '$' over RS-485.
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

  // ====== Trigger commands (FOCUS precedes TRIGGER; refocus gap enforced) ======
  if (cmd.equalsIgnoreCase("TRIGGER")) {
    cameraTriggerCycle();
    drainClient(client);   // discard anything queued during the capture
    sendLine(client, "OK TRIGGERED");
    return;
  }
  if (cmd.startsWith("TRIGGER_MS")) {
    // Syntax: TRIGGER_MS <hold_ms> [focus_lead_ms]
    // hold_ms above ~100 buys nothing; focus_lead_ms is the AF convergence window.
    int sep = cmd.indexOf(' ');
    if (sep > 0) {
      String args = cmd.substring(sep + 1);
      args.trim();
      long ms = args.toInt();                 // first number = trigger hold
      long focusMs = -1;                       // optional second number = focus lead
      int sep2 = args.indexOf(' ');
      if (sep2 > 0) {
        focusMs = args.substring(sep2 + 1).toInt();
      }
      if (ms > 0 && ms <= 10000) {
        if (focusMs >= 0 && focusMs <= 5000) {
          cameraTriggerCycle((unsigned long)ms, (unsigned long)focusMs);
        } else {
          cameraTriggerCycle((unsigned long)ms);  // default focus lead
        }
        drainClient(client);   // discard anything queued during the capture
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
  Serial.println(F("          TRIGGER | TRIGGER_MS <ms> [focus_ms]  (drains backlog each capture)"));

  // Camera FOCUS/TRIGGER idle high-Z (open-drain released).
  pinMode(cameraFocusPin, INPUT);
  pinMode(cameraTriggerPin, INPUT);

  // Prime the refocus gap so the first shot isn't forced to wait.
  lastFocusReleaseMs = millis() - cameraReleaseGapMs;
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
