import os
import sys
import subprocess
import socket
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import datetime
import time
import re
import math
import json
import urllib.request
import urllib.error
from pathlib import Path

# --- Pillow for JPEG rendering ---
from PIL import Image, ImageTk
from io import BytesIO

# --- pymavlink for direct seabed altitude probing ---
try:
    from pymavlink import mavutil
except ImportError:
    mavutil = None

# --- Paramiko for Pi SSH ---
try:
    import paramiko
except ImportError:
    paramiko = None

# Camera focus lead time before the trigger pulse starts (ms).
# This is sent to the Arduino with each TRIGGER_MS command, so the app value
# is the single source of truth. With a 1000 ms loop interval and 100 ms hold,
# 750 ms gives the AF the longest practical window while leaving ~150 ms margin.
FOCUS_LEAD_MS = 750

def get_app_dir() -> Path:
    """
    Return the folder beside the executable (frozen) or this script (dev).
    Use this for files that live next to the exe, not bundled inside it.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def resource_path(rel_path: str) -> str:
    """Return absolute path to resource, works for dev and PyInstaller."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, rel_path)
    return str(get_app_dir() / rel_path)


# -------------------- Configuration --------------------
# Master switch for the BlueROV heading / drive / depth controls.
# False = the Heading/Drive and Depth panels are hidden and no BlueROV
# API calls (yaw, velocity, depth, status polling) are made.
BLUEROV_CONTROLS_ENABLED = False

DEFAULT_HOST = "192.168.2.70"
DEFAULT_PORT = 9000
MANUAL_FILENAME = "StrobeCameraManual.pdf"  # put this PDF next to main.py (or bundle with PyInstaller)

# App icon files (bundle aongside main.py / exe)
APP_ICON_ICO = "app.ico"      # primary on Windows
APP_ICON_PNG = "cris_logo.png"      # optional fallback for non-Windows (if available)

# Sony SDK paths — absolute so the app works when double-clicked from Explorer
_RELEASE_DIR = get_app_dir() / "Release"
DEFAULT_IMAGE_DIR = str(_RELEASE_DIR)
CAMERA_APP_EXE = str(_RELEASE_DIR / "RemoteCli.exe")

# Stop flag file for the external camera app
STOP_FILE_PATH = str(_RELEASE_DIR / "stop.txt")

# Live View settings
LIVEVIEW_PATH = str(_RELEASE_DIR / "LiveView000000.JPG")
LIVEVIEW_REFRESH_MS = 50  # ~20 checks/sec; repaints only on mtime change
LIVEVIEW_TARGET_W = 1024
LIVEVIEW_TARGET_H = 680
LIVEVIEW_ASPECT = LIVEVIEW_TARGET_W / LIVEVIEW_TARGET_H

# MAVLink connection for direct seabed altitude probing
MAVLINK_CONNECTION = 'udpin:0.0.0.0:14552'
MAVLINK_STREAM_HZ = 20

# Raspberry Pi SSH settings for exposure logger
PI_HOST = "192.168.2.2"
PI_USER = "pi"
PI_PASS = "raspberry"
PI_SCRIPT_CMD = "cd ~/navigator_logger && venv/bin/python -u readpin.py"

# BlueROV control software HTTP API (topside Flask server; heading + drive + depth live there)
BLUEROV_API_HOST = "127.0.0.1"
BLUEROV_API_PORT = 5000
BLUEROV_YAW_ABS_URL = f"http://{BLUEROV_API_HOST}:{BLUEROV_API_PORT}/api/yaw_abs"
BLUEROV_SET_VELOCITY_URL = f"http://{BLUEROV_API_HOST}:{BLUEROV_API_PORT}/api/set_velocity"
BLUEROV_DEPTH_STEP_URL = f"http://{BLUEROV_API_HOST}:{BLUEROV_API_PORT}/api/depth_step"
BLUEROV_STOP_DEPTH_URL = f"http://{BLUEROV_API_HOST}:{BLUEROV_API_PORT}/api/stop_depth"
BLUEROV_CLEAR_DEPTH_HOLD_URL = f"http://{BLUEROV_API_HOST}:{BLUEROV_API_PORT}/api/clear_depth_hold"
BLUEROV_STATUS_URL = f"http://{BLUEROV_API_HOST}:{BLUEROV_API_PORT}/api/status"
HEADING_SEND_THROTTLE_MS = 120   # matches the BlueROV web UI's own drag throttle
DEFAULT_DRIVE_SPEED_MPS = 0.5    # default constant forward speed (m/s)
DEFAULT_DEPTH_STEP_M = 0.5       # default depth nudge per tap (m)
DEPTH_STATUS_POLL_MS = 1000      # how often to poll current depth for the readout

# Match DSCXXXXX.JPG and capture the number as group(1)
IMAGE_PATTERN = re.compile(r"^DSC(\d+)\.(jpg)$", re.IGNORECASE)
# ------------------------------------------------------


def open_file_with_default_app(path: str):
    """Open a file with the OS default application."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def post_json(url: str, payload: dict, timeout: float = 2.0):
    """POST a JSON body to url and return the parsed response (stdlib only)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    try:
        return json.loads(body)
    except Exception:
        return body


def get_json(url: str, timeout: float = 2.0):
    """GET url and return the parsed JSON response (stdlib only)."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body)


class HeadingDial(tk.Canvas):
    """Circular heading wheel: click/drag to pick a heading (0-359 deg)."""

    def __init__(self, master, size=170, on_change=None, **kwargs):
        kwargs.setdefault("bg", "#101010")
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, width=size, height=size, **kwargs)
        self.size = size
        self.radius = size / 2 - 16
        self.center = (size / 2, size / 2)
        self.heading = 0.0
        self.on_change = on_change

        self.bind("<Button-1>", self._on_pointer)
        self.bind("<B1-Motion>", self._on_pointer)

        self._draw()

    def _on_pointer(self, event):
        cx, cy = self.center
        deg = (math.degrees(math.atan2(event.y - cy, event.x - cx)) + 90) % 360
        self.set_heading(deg, notify=True)

    def set_heading(self, deg, notify=False):
        self.heading = float(deg) % 360
        self._draw()
        if notify and self.on_change:
            self.on_change(self.heading)

    def _draw(self):
        self.delete("all")
        cx, cy = self.center
        r = self.radius

        self.create_oval(cx - r, cy - r, cx + r, cy + r,
                          outline="#555555", width=2, fill="#0d0d0d")

        for deg in range(0, 360, 30):
            rad = math.radians(deg - 90)
            x1 = cx + (r - 8) * math.cos(rad)
            y1 = cy + (r - 8) * math.sin(rad)
            x2 = cx + r * math.cos(rad)
            y2 = cy + r * math.sin(rad)
            self.create_line(x1, y1, x2, y2, fill="#555555")

        for deg, label in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
            rad = math.radians(deg - 90)
            lx = cx + (r + 12) * math.cos(rad)
            ly = cy + (r + 12) * math.sin(rad)
            self.create_text(lx, ly, text=label, fill="#d4f1a0",
                              font=("Segoe UI", 10, "bold"))

        rad = math.radians(self.heading - 90)
        nx = cx + (r - 6) * math.cos(rad)
        ny = cy + (r - 6) * math.sin(rad)
        self.create_line(cx, cy, nx, ny, fill="#e53935", width=3, arrow="last")
        self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill="#e53935", outline="")


class TcpClient:
    def __init__(self, on_line):
        self.sock = None
        self.alive = False
        self.rx_thread = None
        self.on_line = on_line

    def connect(self, host, port):
        self.close()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((host, port))
        s.settimeout(None)  # blocking recv
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass
        self.sock = s
        self.alive = True
        self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True, name="TCP-RX")
        self.rx_thread.start()

    def _rx_loop(self):
        buf = b""
        try:
            while self.alive:
                data = self.sock.recv(1024)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        self.on_line(line.decode(errors="ignore").strip())
                    except Exception:
                        pass
        except Exception as e:
            self.on_line(f"[RX ERROR] {e}")
        finally:
            self.alive = False
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.on_line("[Disconnected]")

    def send_line(self, text: str):
        """Send EXACT text as typed; ensures a single trailing newline."""
        if not self.sock:
            raise RuntimeError("Not connected")
        if text.endswith("\n"):
            data = text.encode()
        else:
            data = (text + "\n").encode()
        self.sock.sendall(data)

    def close(self):
        self.alive = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Strobe / Lamp / Camera Controller (TCP) Aquorea Mk3 / Sony ILX")
        self.geometry("1100x800")

        # Keep a reference to any PhotoImage used for icons to prevent GC
        self._icon_img = None

        # Apply icon to the main window
        self._apply_icon(self)

        # --- Create logs folder and session log file beside the executable/script ---
        logs_dir = get_app_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.run_log_path = logs_dir / f"session_log_{ts}.txt"

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # Connection setup (kept internal; fields hidden per original)
        self.ip_var = tk.StringVar(value=DEFAULT_HOST)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))

        ttk.Separator(root, orient="horizontal").pack(fill="x", pady=4)

        # -------------------- Sliders --------------------
        sliders = ttk.LabelFrame(root, text="Intensities")
        sliders.pack(fill="x", pady=10)

        srow = ttk.Frame(sliders); srow.pack(fill="x", pady=6)
        ttk.Label(srow, text="Strobe intensity").pack(side="left")
        self.strobe_scale = ttk.Scale(
            srow, from_=0, to=100, orient="horizontal",
            command=lambda v: self._update_val(self.lbl_strobe, v)
        )
        self.strobe_scale.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_strobe = ttk.Label(srow, width=4, anchor="e", text="0"); self.lbl_strobe.pack(side="left")
        self.strobe_scale.bind(
            "<ButtonRelease-1>",
            lambda e: self.send_cmd(f"STROBE_INTENSITY {int(float(self.strobe_scale.get()))}")
        )

        lrow = ttk.Frame(sliders); lrow.pack(fill="x", pady=6)
        ttk.Label(lrow, text="Lamp intensity").pack(side="left")
        self.lamp_scale = ttk.Scale(
            lrow, from_=0, to=100, orient="horizontal",
            command=lambda v: self._update_val(self.lbl_lamp, v)
        )
        self.lamp_scale.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_lamp = ttk.Label(lrow, width=4, anchor="e", text="0"); self.lbl_lamp.pack(side="left")
        self.lamp_scale.bind(
            "<ButtonRelease-1>",
            lambda e: self.send_cmd(f"LAMP_INTENSITY {int(float(self.lamp_scale.get()))}")
        )

        # -------------------- Camera Trigger controls --------------------
        trig_ctrl = ttk.LabelFrame(root, text="Camera / Controller")
        trig_ctrl.pack(fill="x", pady=10)

        ttk.Button(trig_ctrl, text="Single Trigger",
                   command=lambda: self.send_cmd("TRIGGER")).pack(side="left", padx=5)

        self.trig_time_var = tk.StringVar(value="100")       # trigger hold (ms)
        self.trig_interval_var = tk.StringVar(value="1000")  # loop interval (ms)
        self.focus_lead_var = tk.StringVar(value=str(FOCUS_LEAD_MS))  # focus lead (ms)

        self.loop_running = False
        self.loop_job = None

        # --- ACK-gated trigger loop state ---
        # The loop sends one TRIGGER_MS, then waits for the Arduino's
        # "OK TRIGGERED" reply before sending the next. This keeps at most one
        # command in flight, so nothing backlogs in the socket buffer and
        # pressing Stop takes effect immediately.
        self._trigger_inflight = False
        self._trigger_sent_time = 0.0
        self._trigger_watchdog_job = None
        self._trigger_timeout_ms = 2000   # max wait for an ACK before moving on

        def toggle_loop():
            if not self.loop_running:
                self.start_loop()
            else:
                self.stop_loop()

        self.loop_btn = tk.Button(trig_ctrl, text="Start Loop", command=toggle_loop, width=16)
        self.loop_btn.pack(side="left", padx=10)
        ttk.Label(trig_ctrl, text="Interval (ms):").pack(side="left")
        ttk.Entry(trig_ctrl, textvariable=self.trig_interval_var, width=8).pack(side="left")
        ttk.Label(trig_ctrl, text="Focus lead (ms):").pack(side="left", padx=(10, 0))
        ttk.Entry(trig_ctrl, textvariable=self.focus_lead_var, width=6).pack(side="left")


        ttk.Button(trig_ctrl, text="Lamp OFF",
                   command=lambda: self.send_cmd("LAMP OFF")).pack(side="left", padx=6)

        ttk.Button(trig_ctrl, text="Open Manual (PDF)",
                   command=self.open_manual).pack(side="left", padx=6)

        self.btn_gui = tk.Button(trig_ctrl, text="Show Camera GUI",
                                 command=self.restart_camera_with_gui, width=22)
        self.btn_gui.pack(side="left", padx=6)

        # BlueROV state defaults — always initialised so the rest of the app
        # (e.g. on_close) is safe whether or not the controls are enabled.
        self._pending_heading = None
        self._heading_send_job = None
        self.driving_forward = False
        self._depth_status_job = None

        if BLUEROV_CONTROLS_ENABLED:
            # -------------------- Heading & Drive (BlueROV) --------------------
            heading_frame = ttk.LabelFrame(root, text="Heading / Drive (BlueROV)")
            heading_frame.pack(fill="x", pady=10)

            self.heading_dial = HeadingDial(heading_frame, size=150, on_change=self._on_dial_change)
            self.heading_dial.pack(side="left", padx=10, pady=8)

            heading_ctrl = ttk.Frame(heading_frame)
            heading_ctrl.pack(side="left", fill="x", expand=True, padx=10, pady=8)

            hrow = ttk.Frame(heading_ctrl); hrow.pack(fill="x", pady=4)
            ttk.Label(hrow, text="Heading (deg):").pack(side="left")
            self.heading_var = tk.StringVar(value="000")
            self.heading_readout = ttk.Label(hrow, textvariable=self.heading_var, width=5,
                                              font=("Segoe UI", 11, "bold"))
            self.heading_readout.pack(side="left", padx=(6, 12))

            self.heading_entry_var = tk.StringVar(value="0")
            ttk.Entry(hrow, textvariable=self.heading_entry_var, width=6).pack(side="left")
            ttk.Button(hrow, text="Set Heading",
                       command=self._apply_heading_entry).pack(side="left", padx=6)

            drow = ttk.Frame(heading_ctrl); drow.pack(fill="x", pady=(10, 4))
            ttk.Label(drow, text="Forward speed (m/s):").pack(side="left")
            self.drive_speed_var = tk.StringVar(value=str(DEFAULT_DRIVE_SPEED_MPS))
            ttk.Entry(drow, textvariable=self.drive_speed_var, width=6).pack(side="left", padx=(6, 12))

            self.drive_btn = tk.Button(drow, text="Drive Forward",
                                        command=self._toggle_drive_forward, width=16)
            self.drive_btn.pack(side="left")
            self._set_drive_button(False)

            # -------------------- Depth (BlueROV) --------------------
            depth_frame = ttk.LabelFrame(root, text="Depth (BlueROV)")
            depth_frame.pack(fill="x", pady=10)

            self.depth_readout_var = tk.StringVar(value="Depth: -- m")
            ttk.Label(depth_frame, textvariable=self.depth_readout_var, width=16,
                      font=("Segoe UI", 11, "bold")).pack(side="left", padx=10)

            depth_ctrl = ttk.Frame(depth_frame)
            depth_ctrl.pack(side="left", fill="x", expand=True, padx=10, pady=8)

            dtrow = ttk.Frame(depth_ctrl); dtrow.pack(fill="x", pady=4)
            ttk.Label(dtrow, text="Target depth (m):").pack(side="left")
            self.depth_target_var = tk.StringVar(value="0.0")
            ttk.Entry(dtrow, textvariable=self.depth_target_var, width=6).pack(side="left", padx=(6, 12))
            ttk.Button(dtrow, text="Set Depth", command=self._apply_depth_target).pack(side="left")

            dsrow = ttk.Frame(depth_ctrl); dsrow.pack(fill="x", pady=4)
            ttk.Label(dsrow, text="Step (m):").pack(side="left")
            self.depth_step_var = tk.StringVar(value=str(DEFAULT_DEPTH_STEP_M))
            ttk.Entry(dsrow, textvariable=self.depth_step_var, width=6).pack(side="left", padx=(6, 12))
            ttk.Button(dsrow, text="▲ Shallower", width=11,
                       command=lambda: self._nudge_depth(-1)).pack(side="left", padx=2)
            ttk.Button(dsrow, text="▼ Deeper", width=11,
                       command=lambda: self._nudge_depth(1)).pack(side="left", padx=2)
            ttk.Button(dsrow, text="Hold Current Depth",
                       command=self._hold_current_depth).pack(side="left", padx=(12, 4))
            ttk.Button(dsrow, text="Release Depth Hold",
                       command=self._release_depth_hold).pack(side="left", padx=4)

            self._schedule_depth_status_poll()

        # -------------------- Pi Exposure Logger --------------------
        pi_frame = ttk.LabelFrame(root, text="Pi Exposure Logger")
        pi_frame.pack(fill="x", pady=6)

        pi_btn_row = ttk.Frame(pi_frame)
        pi_btn_row.pack(fill="x", padx=6, pady=4)

        self.pi_btn = tk.Button(pi_btn_row, text="Start Pi Logger",
                                command=self.toggle_pi_logger, width=18,
                                bg="#b71c1c", fg="white", activebackground="#b71c1c")
        self.pi_btn.pack(side="left")

        pi_out_frame = ttk.Frame(pi_frame)
        pi_out_frame.pack(fill="x", padx=6, pady=(0, 6))
        pi_scroll = ttk.Scrollbar(pi_out_frame)
        pi_scroll.pack(side="right", fill="y")
        self.pi_output = tk.Text(
            pi_out_frame, height=7, bg="#1a1a1a", fg="#d4f1a0",
            font=("Consolas", 9), state="disabled", wrap="word",
            yscrollcommand=pi_scroll.set
        )
        self.pi_output.pack(side="left", fill="x", expand=True)
        pi_scroll.config(command=self.pi_output.yview)

        self.pi_dvl_label = ttk.Label(pi_frame, text="Seabed alt: -- m")
        self.pi_dvl_label.pack(anchor="w", padx=8, pady=(0, 6))

        # Pi logger state
        self._pi_running = False
        self._pi_ssh = None
        self._pi_thread = None

        # Direct MAVLink seabed altitude state
        self._mav_thread = None
        self._mav_running = False
        self._seabed_alt = None
        self._seabed_alt_lock = threading.Lock()

        # -------------------- Live View panel --------------------
        live_frame = ttk.LabelFrame(root, text=f"Live View ({LIVEVIEW_TARGET_W}×{LIVEVIEW_TARGET_H} aspect)")
        live_frame.pack(fill="both", expand=True, pady=8)

        live_frame.rowconfigure(0, weight=1)   # image row expands
        live_frame.rowconfigure(1, weight=0)   # controls row fixed
        live_frame.rowconfigure(2, weight=0)   # altimeter row
        live_frame.columnconfigure(0, weight=1)

        self.live_frame = live_frame
        self.live_label = tk.Label(live_frame, anchor="center", bg="#202020")
        self.live_label.grid(row=0, column=0, sticky="nsew")

        # Buttons under live view
        controls = ttk.Frame(live_frame)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for i in range(4):
            controls.columnconfigure(i, weight=0)
        controls.columnconfigure(4, weight=1)  # spacer

        self.btn_arduino = tk.Button(
            controls, text="Arduino: Retry Connect",
            command=self.retry_arduino_connect, width=22
        )
        self.btn_arduino.grid(row=0, column=0, padx=6, pady=4, sticky="w")

        ttk.Label(controls, text="").grid(row=0, column=4, sticky="ew")  # spacer

        # Internal liveview state
        self._liveview_job = None
        self._liveview_tk = None
        self._last_liveview_mtime = None
        self._last_render_size = (0, 0)

        # camera process tracking
        self.camera_proc = None
        self._camera_lock = threading.Lock()
        self.camera_mode = None  # "headless", "gui", or None
        self._camera_status_job = None
        self._camera_reader_thread = None

        # Latest Image window state
        self.latest_win = None
        self._latest_label = None
        self._latest_job = None
        self._latest_tk = None
        self._latest_img_pil = None
        self._last_latest_name = None
        self._last_latest_mtime = None
        self._latest_poll_ms = 300

        # Kick off periodic refresh & handle resize events
        self.start_liveview()
        self.live_frame.bind("<Configure>", self._on_liveview_resize)

        self.client = TcpClient(self.on_line_received)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Camera app is no longer auto-started; it launches only when the user
        # presses "Live View (Headless)" or "Show Camera GUI".

        # Start periodic camera status poll to color/disable buttons
        self._schedule_camera_status_poll()

        # --- Auto-connect to Arduino on boot (non-blocking) ---
        self.try_autoconnect_arduino()
        self._schedule_arduino_status_poll()

        # --- Start direct MAVLink seabed-altitude probe ---
        self._start_mavlink_probe()

        # Let the user know where logs are going
        self.append_log(f"[INFO] Writing logs to: {os.path.abspath(self.run_log_path)}")

        # Initialize GUI button colors/states
        self._refresh_camera_buttons()
        self._refresh_latest_button()

    # ---------- Icon helper (applies to any window) ----------
    def _apply_icon(self, window: tk.Misc):
        """
        Apply the application icon to the given Tk/Toplevel window.
        Prefers .ico on Windows; falls back to PNG with iconphoto elsewhere.
        """
        # Try ICO on Windows
        try:
            ico_path = resource_path(APP_ICON_ICO)
            if sys.platform.startswith("win") and os.path.exists(ico_path):
                window.iconbitmap(ico_path)
                return
        except Exception:
            pass

        # Fallback: PNG via iconphoto (works cross-platform)
        try:
            png_path = resource_path(APP_ICON_PNG)
            if os.path.exists(png_path):
                self._icon_img = tk.PhotoImage(file=png_path)
                window.iconphoto(True, self._icon_img)
        except Exception:
            pass  # No icon available; ignore

    # ---------- Manual open ----------
    def open_manual(self):
        path = resource_path(MANUAL_FILENAME)
        if not os.path.exists(path):
            messagebox.showerror("Manual not found", f"Couldn't find:\n{path}")
            return
        try:
            open_file_with_default_app(path)
        except Exception as e:
            messagebox.showerror("Error opening manual", str(e))

    # ---------- Camera app process control ----------
    def start_camera(self, headless=True):
        """
        Start RemoteCli.exe.
        headless=True  -> no args (live view only)
        headless=False -> arg '1' (show camera GUI)
        """
        with self._camera_lock:
            try:
                args = [CAMERA_APP_EXE] if headless else [CAMERA_APP_EXE, "1"]
                proc = subprocess.Popen(
                    args,
                    cwd=os.path.dirname(CAMERA_APP_EXE),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1  # line-buffered (best-effort on Windows)
                )
                self.camera_proc = proc
                self.camera_mode = "headless" if headless else "gui"
                self.append_log(f"[RemoteCLI] Launched: {' '.join(args)}")
                self._start_camera_reader(proc)
            except Exception as e:
                messagebox.showerror("Launch failed", f"Could not start camera app:\n{e}")
                self.append_log(f"[RemoteCLI] Launch failed: {e}")
                self.camera_proc = None
                self.camera_mode = None
        self._refresh_camera_buttons()

    def _start_camera_reader(self, proc: subprocess.Popen):
        def _reader():
            try:
                if proc.stdout is None:
                    return
                for line in proc.stdout:
                    line = line.rstrip("\r\n")
                    if line:
                        self.append_log(f"[RemoteCLI] {line}")
                rest = proc.stdout.read()
                if rest:
                    for ln in rest.splitlines():
                        self.append_log(f"[RemoteCLI] {ln}")
            except Exception as e:
                self.append_log(f"[RemoteCLI] [reader error] {e}")
            finally:
                if proc.poll() is not None and self.camera_proc is proc:
                    self.camera_mode = None
                    self._refresh_camera_buttons()

        self._camera_reader_thread = threading.Thread(target=_reader, daemon=True, name="RemoteCLI-Reader")
        self._camera_reader_thread.start()

    def _start_mavlink_probe(self):
        if mavutil is None:
            self._update_seabed_alt_label(None)
            self.append_log("[MAVLink] pymavlink not installed; direct seabed altitude disabled")
            return
        if self._mav_running:
            return
        self._mav_running = True
        self._mav_thread = threading.Thread(
            target=self._mavlink_probe_loop,
            daemon=True,
            name="MAVLink-Probe"
        )
        self._mav_thread.start()

    def _stop_mavlink_probe(self):
        self._mav_running = False
        if self._mav_thread is not None and self._mav_thread.is_alive():
            self._mav_thread.join(timeout=1.0)
        self._mav_thread = None

    def _mavlink_probe_loop(self):
        conn = None
        try:
            self.append_log(f"[MAVLink] Connecting to {MAVLINK_CONNECTION}")
            conn = mavutil.mavlink_connection(MAVLINK_CONNECTION)
            heartbeat = conn.wait_heartbeat(timeout=10)
            if heartbeat is None:
                raise RuntimeError("No MAVLink heartbeat received")
            self.append_log(f"[MAVLink] Heartbeat from sys={conn.target_system} comp={conn.target_component}")
            try:
                conn.mav.request_data_stream_send(
                    conn.target_system,
                    conn.target_component,
                    mavutil.mavlink.MAV_DATA_STREAM_ALL,
                    MAVLINK_STREAM_HZ,
                    1
                )
            except Exception:
                pass

            last_seen = time.time()
            while self._mav_running:
                msg = conn.recv_match(type="RANGEFINDER", blocking=True, timeout=2)
                if not self._mav_running:
                    break
                if msg is None:
                    if time.time() - last_seen > 5.0:
                        self._update_seabed_alt_label(None)
                    continue
                last_seen = time.time()
                if hasattr(msg, "distance"):
                    distance = float(msg.distance)
                    with self._seabed_alt_lock:
                        self._seabed_alt = distance
                    self._update_seabed_alt_label(distance)
        except Exception as e:
            self.append_log(f"[MAVLink] Error: {e}")
            self._update_seabed_alt_label(None)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._mav_running = False

    def _update_seabed_alt_label(self, distance):
        def ui():
            if distance is None:
                self.pi_dvl_label.config(text="Seabed alt: -- m")
            else:
                self.pi_dvl_label.config(text=f"Seabed alt: {distance:.3f} m")
        self.after(0, ui)

    def _write_stop_file(self):
        try:
            with open(STOP_FILE_PATH, "w") as f:
                f.write("stop")
        except Exception:
            pass

    def _remove_stop_file(self):
        try:
            if os.path.exists(STOP_FILE_PATH):
                os.remove(STOP_FILE_PATH)
        except Exception:
            pass

    def restart_camera_headless(self):
        """Stop current instance via stop.txt, then start headless (no args)."""
        def worker():
            self._write_stop_file()
            deadline = time.time() + 2.0
            with self._camera_lock:
                proc = self.camera_proc
            if proc is not None:
                while time.time() < deadline:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
            self._remove_stop_file()
            self.start_camera(headless=True)
        threading.Thread(target=worker, daemon=True, name="RestartHeadless").start()

    def restart_camera_with_gui(self):
        """Stop current instance via stop.txt, then start with arg=1 (GUI)."""
        def worker():
            self._write_stop_file()
            deadline = time.time() + 2.0
            with self._camera_lock:
                proc = self.camera_proc
            if proc is not None:
                while time.time() < deadline:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
            self._remove_stop_file()
            self.start_camera(headless=False)
        threading.Thread(target=worker, daemon=True, name="RestartGUI").start()

    def _schedule_camera_status_poll(self):
        self._poll_camera_status()
        self._camera_status_job = self.after(500, self._schedule_camera_status_poll)

    def _poll_camera_status(self):
        with self._camera_lock:
            proc = self.camera_proc
        running = (proc is not None and proc.poll() is None)
        if not running:
            self.camera_mode = None
        self._refresh_camera_buttons()

    def _refresh_camera_buttons(self):
        def set_btn(btn, active: bool):
            # Green if active (and disabled), red if inactive (and enabled)
            if active:
                try:
                    btn.config(state="disabled", bg="#2e7d32", fg="white", activebackground="#2e7d32")
                except Exception:
                    btn.config(state="disabled")
            else:
                try:
                    btn.config(state="normal", bg="#b71c1c", fg="white", activebackground="#b71c1c")
                except Exception:
                    btn.config(state="normal")

        mode = self.camera_mode  # "headless", "gui", or None
        set_btn(self.btn_gui,      active=(mode == "gui"))

    # ---------- Latest Image popup ----------
    def toggle_latest_window(self):
        if self.latest_win and tk.Toplevel.winfo_exists(self.latest_win):
            self._close_latest_window()
        else:
            self._open_latest_window()

    def _open_latest_window(self):
        if self.latest_win and tk.Toplevel.winfo_exists(self.latest_win):
            return
        self.latest_win = tk.Toplevel(self)
        self.latest_win.title("Latest Image")
        self.latest_win.geometry("900x600")
        self.latest_win.protocol("WM_DELETE_WINDOW", self._close_latest_window)

        # Apply icon to this Toplevel as well
        self._apply_icon(self.latest_win)

        container = ttk.Frame(self.latest_win)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self._latest_label = tk.Label(container, anchor="center", bg="#101010")
        self._latest_label.grid(row=0, column=0, sticky="nsew")

        self.latest_win.bind("<Configure>", self._on_latest_resize)

        # reset state
        self._latest_img_pil = None
        self._latest_tk = None
        self._last_latest_name = None
        self._last_latest_mtime = None

        self._refresh_latest_button()
        self._schedule_latest_poll()

    def _close_latest_window(self):
        try:
            if self._latest_job is not None:
                self.after_cancel(self._latest_job)
        except Exception:
            pass
        self._latest_job = None

        try:
            if self.latest_win and tk.Toplevel.winfo_exists(self.latest_win):
                self.latest_win.destroy()
        except Exception:
            pass
        self.latest_win = None
        self._latest_label = None
        self._latest_tk = None
        self._latest_img_pil = None
        self._last_latest_name = None
        self._last_latest_mtime = None
        self._refresh_latest_button()

    def _refresh_latest_button(self):
        # The "Show Latest Image" button has been removed; nothing to update.
        return

    def _schedule_latest_poll(self):
        self._poll_latest_once()
        self._latest_job = self.after(self._latest_poll_ms, self._schedule_latest_poll)

    def _poll_latest_once(self):
        if not (self.latest_win and tk.Toplevel.winfo_exists(self.latest_win)):
            return
        folder = Path(DEFAULT_IMAGE_DIR)
        try:
            newest = self._find_newest_image(folder)
        except Exception:
            newest = None
        if newest is None:
            if self._latest_label:
                self._latest_label.config(text=f"No DSC*.JPG found in:\n{folder}")
            return

        name, path, mtime = newest
        if (name != self._last_latest_name) or (mtime != self._last_latest_mtime):
            try:
                with open(path, "rb") as f:
                    data = f.read()
                img = Image.open(BytesIO(data))
                img.load()
                self._latest_img_pil = img
                self._last_latest_name = name
                self._last_latest_mtime = mtime
                self._render_latest_cached()
            except Exception:
                # ignore transient read errors while file is being written
                pass
        else:
            # re-render if window size changed
            self._render_latest_cached()

    def _find_newest_image(self, folder: Path):
        """Return (name, full_path, mtime) of the highest-numbered DSCXXXXX.JPG, or None."""
        if not folder.exists():
            return None
        newest = None
        best_num = -1
        for p in folder.iterdir():
            if not p.is_file():
                continue
            m = IMAGE_PATTERN.match(p.name)
            if not m:
                continue
            try:
                num = int(m.group(1))
            except Exception:
                continue
            if num > best_num:
                try:
                    st = p.stat()
                    newest = (p.name, str(p), st.st_mtime)
                    best_num = num
                except Exception:
                    continue
        return newest

    def _on_latest_resize(self, _evt=None):
        self._render_latest_cached()

    def _render_latest_cached(self):
        if not (self.latest_win and tk.Toplevel.winfo_exists(self.latest_win)):
            return
        if self._latest_img_pil is None or self._latest_label is None:
            return
        try:
            w = max(1, self._latest_label.winfo_width())
            h = max(1, self._latest_label.winfo_height())
            iw, ih = self._latest_img_pil.size
            if iw <= 0 or ih <= 0:
                return
            scale = min(w / iw, h / ih)
            tw = max(1, int(iw * scale))
            th = max(1, int(ih * scale))
            img = self._latest_img_pil.resize((tw, th), Image.BILINEAR)
            self._latest_tk = ImageTk.PhotoImage(img)
            self._latest_label.config(image=self._latest_tk, text="")
        except Exception:
            pass

    # ---------- Arduino/TCP auto-connect & status ----------
    def try_autoconnect_arduino(self):
        """Attempt to connect to the Arduino on startup (non-blocking)."""
        def worker():
            host = self.ip_var.get().strip()
            try:
                port = int(self.port_var.get())
            except Exception:
                return
            try:
                self.client.connect(host, port)
            except Exception:
                pass  # ignore on boot; user can retry with the button
        threading.Thread(target=worker, daemon=True, name="Arduino-Autoconnect").start()

    def retry_arduino_connect(self):
        """Manual retry from the red Arduino button."""
        def worker():
            host = self.ip_var.get().strip()
            try:
                port = int(self.port_var.get())
            except Exception:
                messagebox.showerror("Invalid Port", "Port must be an integer.")
                return
            try:
                self.client.connect(host, port)
                self.append_log(f"[Connected to {host}:{port}]")
            except Exception as e:
                messagebox.showwarning("Connect failed", str(e))
        threading.Thread(target=worker, daemon=True, name="Arduino-Retry").start()

    def _schedule_arduino_status_poll(self):
        self._refresh_arduino_button()
        self.after(500, self._schedule_arduino_status_poll)

    def _arduino_connected(self) -> bool:
        return bool(self.client and self.client.sock and self.client.alive)

    def _refresh_arduino_button(self):
        connected = self._arduino_connected()
        if connected:
            text = "Arduino: Connected"
            state = "disabled"
            bg = "#2e7d32"
        else:
            text = "Arduino: Retry Connect"
            state = "normal"
            bg = "#b71c1c"
        try:
            self.btn_arduino.config(text=text, state=state, bg=bg, fg="white", activebackground=bg)
        except Exception:
            self.btn_arduino.config(text=text, state=state)


    # ---------- UI helpers ----------
    def _update_val(self, label, v):
        try:
            label.config(text=str(int(float(v))))
        except Exception:
            pass

    # ---------- FILE LOGGING (used for all logs incl. RemoteCLI output) ----------
    def _write_line_to_file(self, line: str):
        try:
            with open(self.run_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        try:
            print(line)
        except Exception:
            pass

    def append_log(self, text):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._write_line_to_file(f"{ts} {text}")

    # ---------- Incoming TCP lines ----------
    def on_line_received(self, line):
        def ui():
            self.append_log(f"<< {line}")
            # ACK-gated loop: the Arduino replies "OK TRIGGERED" when a capture
            # cycle finishes. That reply is what releases the next trigger.
            if self._trigger_inflight and line.startswith("OK TRIGGERED"):
                self._on_trigger_ack()
            payload = self._extract_rx_payload(line)
            if payload:
                self.append_rx(payload)
        self.after(0, ui)

    def _extract_rx_payload(self, line: str):
        if line.startswith("RS485: "):
            return line[7:]
        tag = "[RS485<-] "
        if line.startswith(tag):
            return line[len(tag):]
        return None

    def append_rx(self, text):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._write_line_to_file(f"{ts} [RX] {text}")

    # ---------- Continuous trigger loop (ACK-gated) ----------
    def start_loop(self):
        try:
            hold_ms = int(self.trig_time_var.get())
            interval_ms = int(self.trig_interval_var.get())
            focus_ms = int(self.focus_lead_var.get())
            if hold_ms <= 0 or interval_ms <= 0 or focus_ms < 0:
                raise ValueError
        except Exception:
            messagebox.showerror("Invalid Input",
                                 "Hold (ms), Interval (ms) and Focus lead (ms) must be valid integers.")
            return

        self.loop_running = True
        self._trigger_inflight = False
        self._set_loop_button(True)
        self.append_log(f"[LOOP] Started: hold={hold_ms} ms, focus lead={focus_ms} ms, "
                        f"interval={interval_ms} ms (min spacing; waits for ACK each shot)")
        self._loop_tick()

    def stop_loop(self):
        self.loop_running = False
        self._trigger_inflight = False
        self._set_loop_button(False)
        for attr in ("loop_job", "_trigger_watchdog_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        self.append_log("[LOOP] Stopped")

    def _loop_tick(self):
        if not self.loop_running:
            return
        if self._trigger_inflight:
            return  # previous capture not yet acknowledged; don't stack another
        try:
            hold_ms = int(self.trig_time_var.get())
            focus_ms = int(self.focus_lead_var.get())
            if hold_ms <= 0 or focus_ms < 0:
                raise ValueError
        except Exception:
            self.append_log("[LOOP] Invalid inputs; stopping loop.")
            self.stop_loop()
            return

        try:
            self.client.send_line(f"TRIGGER_MS {hold_ms} {focus_ms}")
            self.append_log(f">> TRIGGER_MS {hold_ms} {focus_ms}")
            self._trigger_inflight = True
            self._trigger_sent_time = time.time()
            self._trigger_watchdog_job = self.after(self._trigger_timeout_ms, self._trigger_watchdog)
        except Exception as e:
            messagebox.showwarning("Send failed", str(e))
            self.stop_loop()

    def _on_trigger_ack(self):
        """Called when the Arduino confirms a capture. Schedules the next one."""
        if not self._trigger_inflight:
            return
        self._trigger_inflight = False
        if self._trigger_watchdog_job is not None:
            try:
                self.after_cancel(self._trigger_watchdog_job)
            except Exception:
                pass
            self._trigger_watchdog_job = None
        if not self.loop_running:
            return
        try:
            interval_ms = int(self.trig_interval_var.get())
        except Exception:
            interval_ms = 1000
        # interval acts as a MINIMUM spacing: if the capture took longer than the
        # interval, fire the next immediately; otherwise wait out the remainder.
        elapsed_ms = (time.time() - self._trigger_sent_time) * 1000.0
        delay = max(0, int(interval_ms - elapsed_ms))
        self.loop_job = self.after(delay, self._loop_tick)

    def _trigger_watchdog(self):
        """Safety net: if an ACK never arrives, don't freeze the loop forever."""
        self._trigger_watchdog_job = None
        if not self.loop_running:
            return
        self.append_log("[LOOP] No ACK within timeout; sending next anyway.")
        self._trigger_inflight = False
        self.loop_job = self.after(0, self._loop_tick)

    def _set_loop_button(self, running: bool):
        if running:
            try:
                self.loop_btn.config(text="Stop Loop", bg="green", activebackground="green", fg="white")
            except Exception:
                self.loop_btn.config(text="Stop Loop")
        else:
            try:
                # reset to default colors
                self.loop_btn.config(text="Start Loop", bg=self.cget("bg"), activebackground=self.cget("bg"), fg="black")
            except Exception:
                self.loop_btn.config(text="Start Loop")

    # ---------- Live View helpers ----------
    def start_liveview(self):
        if self._liveview_job is not None:
            return
        self._update_liveview()

    def _on_liveview_resize(self, _event=None):
        if self._liveview_tk is not None:
            self._render_cached_to_size()

    def _compute_target_box(self):
        fw = max(1, self.live_frame.winfo_width())
        fh = max(1, self.live_frame.winfo_height())
        frame_aspect = fw / fh
        if frame_aspect > LIVEVIEW_ASPECT:
            target_h = fh - 8
            target_w = int(target_h * LIVEVIEW_ASPECT)
        else:
            target_w = fw - 8
            target_h = int(target_w / LIVEVIEW_ASPECT)
        target_w = max(1, target_w)
        target_h = max(1, target_h)
        return (target_w, target_h)

    def _render_cached_to_size(self):
        try:
            target_w, target_h = self._compute_target_box()
            if target_w == 0 or target_h == 0:
                return
            if getattr(self, "_last_image_pil", None) is None:
                return
            img = self._last_image_pil.resize((target_w, target_h), Image.BILINEAR)  # "paint straight"
            self._liveview_tk = ImageTk.PhotoImage(img)
            self.live_label.config(image=self._liveview_tk, text="")
            self._last_render_size = (target_w, target_h)
        except Exception:
            pass

    def _update_liveview(self):
        try:
            st = os.stat(LIVEVIEW_PATH)
            mtime = st.st_mtime
            need_reload = (self._last_liveview_mtime != mtime)
            if need_reload:
                with open(LIVEVIEW_PATH, "rb") as f:
                    data = f.read()
                img = Image.open(BytesIO(data))
                img.load()
                self._last_image_pil = img
                self._last_liveview_mtime = mtime
                self._render_cached_to_size()
            else:
                target_w, target_h = self._compute_target_box()
                if (target_w, target_h) != self._last_render_size:
                    self._render_cached_to_size()
        except FileNotFoundError:
            self.live_label.config(text=f"Waiting for live view:\n{LIVEVIEW_PATH}", image="")
            self._liveview_tk = None
            self._last_image_pil = None
        except Exception:
            pass
        finally:
            self._liveview_job = self.after(LIVEVIEW_REFRESH_MS, self._update_liveview)

    # ---------- Sending ----------
    def send_cmd(self, s):
        try:
            self.client.send_line(s)
            self.append_log(f">> {s}")
        except Exception as e:
            messagebox.showwarning("Send failed", str(e))

    # ---------- BlueROV heading / drive (via topside Flask API) ----------
    def _on_dial_change(self, heading_deg):
        self.heading_var.set(f"{heading_deg:03.0f}")
        self.heading_entry_var.set(f"{heading_deg:.0f}")
        self._pending_heading = heading_deg
        if self._heading_send_job is None:
            self._heading_send_job = self.after(HEADING_SEND_THROTTLE_MS, self._flush_heading_send)

    def _flush_heading_send(self):
        self._heading_send_job = None
        heading = self._pending_heading
        if heading is not None:
            self._pending_heading = None
            self._send_bluerov_yaw(heading)

    def _apply_heading_entry(self):
        try:
            heading = float(self.heading_entry_var.get()) % 360
        except Exception:
            messagebox.showerror("Invalid Input", "Heading must be a number (degrees).")
            return
        self.heading_dial.set_heading(heading)
        self.heading_var.set(f"{heading:03.0f}")
        self._send_bluerov_yaw(heading)

    def _send_bluerov_yaw(self, heading_deg):
        if not BLUEROV_CONTROLS_ENABLED:
            return
        def worker():
            try:
                post_json(BLUEROV_YAW_ABS_URL, {"yaw": float(heading_deg)})
                self.after(0, lambda: self.append_log(f">> BlueROV YAW_ABS {heading_deg:.1f}"))
            except Exception as e:
                err = str(e)
                self.after(0, lambda: self.append_log(f"[BlueROV] yaw send failed: {err}"))
        threading.Thread(target=worker, daemon=True, name="BlueROV-Yaw").start()

    def _toggle_drive_forward(self):
        if not self.driving_forward:
            self.start_drive_forward()
        else:
            self.stop_drive_forward()

    def start_drive_forward(self):
        try:
            speed = float(self.drive_speed_var.get())
        except Exception:
            messagebox.showerror("Invalid Input", "Forward speed (m/s) must be a number.")
            return
        if speed <= 0:
            messagebox.showerror("Invalid Input", "Forward speed (m/s) must be positive.")
            return
        self.driving_forward = True
        self._set_drive_button(True)
        self.append_log(f"[DRIVE] Forward started at {speed:.2f} m/s")
        self._send_bluerov_velocity(speed, hold=True)

    def stop_drive_forward(self):
        self.driving_forward = False
        self._set_drive_button(False)
        self.append_log("[DRIVE] Forward stopped")
        self._send_bluerov_velocity(0.0, hold=False)

    def _send_bluerov_velocity(self, vx, hold):
        if not BLUEROV_CONTROLS_ENABLED:
            return
        def worker():
            try:
                post_json(BLUEROV_SET_VELOCITY_URL,
                          {"vx": float(vx), "vy": 0.0, "vz": 0.0, "hold": bool(hold)})
            except Exception as e:
                err = str(e)
                self.after(0, lambda: self.append_log(f"[BlueROV] velocity send failed: {err}"))
        threading.Thread(target=worker, daemon=True, name="BlueROV-Velocity").start()

    def _set_drive_button(self, active: bool):
        if active:
            try:
                self.drive_btn.config(text="Stop (Driving Forward)", bg="#2e7d32",
                                       fg="white", activebackground="#2e7d32")
            except Exception:
                self.drive_btn.config(text="Stop (Driving Forward)")
        else:
            try:
                self.drive_btn.config(text="Drive Forward", bg="#b71c1c",
                                       fg="white", activebackground="#b71c1c")
            except Exception:
                self.drive_btn.config(text="Drive Forward")

    # ---------- BlueROV depth (via topside Flask API) ----------
    # NOTE: /api/depth_step uses NED convention: dz > 0 = deeper, dz < 0 = shallower.
    def _nudge_depth(self, sign):
        try:
            step = float(self.depth_step_var.get())
        except Exception:
            messagebox.showerror("Invalid Input", "Depth step (m) must be a number.")
            return
        if step <= 0:
            messagebox.showerror("Invalid Input", "Depth step (m) must be positive.")
            return
        dz = sign * step
        self.append_log(f"[DEPTH] Step {'deeper' if sign > 0 else 'shallower'} by {step:.2f} m")
        self._send_bluerov_depth_step(dz)

    def _apply_depth_target(self):
        try:
            target = float(self.depth_target_var.get())
        except Exception:
            messagebox.showerror("Invalid Input", "Target depth (m) must be a number.")
            return

        def worker():
            try:
                status = get_json(BLUEROV_STATUS_URL)
                current = float(status.get("local_z", 0.0))
            except Exception as e:
                err = str(e)
                self.after(0, lambda: self.append_log(f"[BlueROV] depth status read failed: {err}"))
                return
            dz = target - current
            self.after(0, lambda: self.append_log(
                f"[DEPTH] Set target {target:.2f} m (current {current:.2f} m, dz {dz:+.2f} m)"))
            self._send_bluerov_depth_step(dz)
        threading.Thread(target=worker, daemon=True, name="BlueROV-DepthTarget").start()

    def _send_bluerov_depth_step(self, dz):
        if not BLUEROV_CONTROLS_ENABLED:
            return
        def worker():
            try:
                resp = post_json(BLUEROV_DEPTH_STEP_URL, {"dz": float(dz)})
                if isinstance(resp, dict) and not resp.get("lock", True):
                    self.after(0, lambda: self.append_log(
                        "[BlueROV] depth_step accepted but not locked "
                        "(vehicle may need to be in GUIDED mode)"))
            except Exception as e:
                err = str(e)
                self.after(0, lambda: self.append_log(f"[BlueROV] depth step failed: {err}"))
        threading.Thread(target=worker, daemon=True, name="BlueROV-DepthStep").start()

    def _hold_current_depth(self):
        if not BLUEROV_CONTROLS_ENABLED:
            return
        def worker():
            try:
                post_json(BLUEROV_STOP_DEPTH_URL, {})
                self.after(0, lambda: self.append_log("[DEPTH] Holding current depth"))
            except Exception as e:
                err = str(e)
                self.after(0, lambda: self.append_log(f"[BlueROV] hold depth failed: {err}"))
        threading.Thread(target=worker, daemon=True, name="BlueROV-DepthHold").start()

    def _release_depth_hold(self):
        if not BLUEROV_CONTROLS_ENABLED:
            return
        def worker():
            try:
                post_json(BLUEROV_CLEAR_DEPTH_HOLD_URL, {})
                self.after(0, lambda: self.append_log("[DEPTH] Depth hold released"))
            except Exception as e:
                err = str(e)
                self.after(0, lambda: self.append_log(f"[BlueROV] release depth hold failed: {err}"))
        threading.Thread(target=worker, daemon=True, name="BlueROV-DepthRelease").start()

    def _schedule_depth_status_poll(self):
        if not BLUEROV_CONTROLS_ENABLED:
            return
        self._poll_depth_status_once()
        self._depth_status_job = self.after(DEPTH_STATUS_POLL_MS, self._schedule_depth_status_poll)

    def _poll_depth_status_once(self):
        def worker():
            try:
                status = get_json(BLUEROV_STATUS_URL, timeout=1.5)
                depth = float(status.get("local_z", 0.0))
                self.after(0, lambda: self.depth_readout_var.set(f"Depth: {depth:.2f} m"))
            except Exception:
                self.after(0, lambda: self.depth_readout_var.set("Depth: -- m (unreachable)"))
        threading.Thread(target=worker, daemon=True, name="BlueROV-DepthPoll").start()

    # ---------- Pi Exposure Logger ----------
    def toggle_pi_logger(self):
        if not self._pi_running:
            self.start_pi_logger()
        else:
            self.stop_pi_logger()

    def start_pi_logger(self):
        if paramiko is None:
            messagebox.showerror(
                "Missing library",
                "paramiko is not installed.\nRun:  pip install paramiko"
            )
            return
        self._pi_running = True
        self._refresh_pi_button()
        self._append_pi(f"Connecting to {PI_USER}@{PI_HOST}…\n")
        self._pi_thread = threading.Thread(
            target=self._pi_worker,
            args=(PI_HOST, PI_USER, PI_PASS),
            daemon=True,
            name="Pi-Logger"
        )
        self._pi_thread.start()

    def _pi_worker(self, host, user, password):
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                host, username=user,
                password=password if password else None,
                timeout=10
            )
            self._pi_ssh = ssh
            self._append_pi("Connected. Running readpin.py…\n")

            _stdin, stdout, stderr = ssh.exec_command(PI_SCRIPT_CMD, get_pty=True)

            for line in iter(stdout.readline, ""):
                if not self._pi_running:
                    break
                if line:
                    self._append_pi(line)

            exit_code = stdout.channel.recv_exit_status()
            self._append_pi(f"Script exited (code {exit_code}).\n")
            ssh.close()
        except Exception as e:
            self._append_pi(f"Error: {e}\n")
        finally:
            self._pi_ssh = None
            self._pi_running = False
            self.after(0, self._refresh_pi_button)

    def stop_pi_logger(self):
        self._pi_running = False
        if self._pi_ssh:
            try:
                self._pi_ssh.close()
            except Exception:
                pass
        self._refresh_pi_button()

    def _append_pi(self, text):
        def ui():
            self.pi_output.config(state="normal")
            self.pi_output.insert("end", text)
            self.pi_output.see("end")
            self.pi_output.config(state="disabled")

        self.after(0, ui)
        self.append_log(f"[Pi] {text.rstrip()}")

    def _refresh_pi_button(self):
        if self._pi_running:
            try:
                self.pi_btn.config(
                    text="Stop Pi Logger", state="normal",
                    bg="#2e7d32", fg="white", activebackground="#2e7d32"
                )
            except Exception:
                self.pi_btn.config(text="Stop Pi Logger")
        else:
            try:
                self.pi_btn.config(
                    text="Start Pi Logger", state="normal",
                    bg="#b71c1c", fg="white", activebackground="#b71c1c"
                )
            except Exception:
                self.pi_btn.config(text="Start Pi Logger")

    # ---------- Close ----------
    def on_close(self):
        # Signal external executable to stop
        try:
            with open(STOP_FILE_PATH, "w") as f:
                f.write("stop")
        except Exception:
            pass

        # Stop camera status poll
        if self._camera_status_job is not None:
            try:
                self.after_cancel(self._camera_status_job)
            except Exception:
                pass
            self._camera_status_job = None

        # Stop Live View loop
        if self._liveview_job is not None:
            try:
                self.after_cancel(self._liveview_job)
            except Exception:
                pass
            self._liveview_job = None

        # Stop Latest Image poll
        if self._latest_job is not None:
            try:
                self.after_cancel(self._latest_job)
            except Exception:
                pass
            self._latest_job = None
        if self.latest_win and tk.Toplevel.winfo_exists(self.latest_win):
            try:
                self.latest_win.destroy()
            except Exception:
                pass

        # Stop continuous trigger loop if running
        if getattr(self, "loop_running", False):
            self.stop_loop()

        # Safety: make sure BlueROV isn't left driving forward on exit
        if getattr(self, "driving_forward", False):
            self.driving_forward = False
            try:
                post_json(BLUEROV_SET_VELOCITY_URL,
                          {"vx": 0.0, "vy": 0.0, "vz": 0.0, "hold": False}, timeout=1.0)
            except Exception:
                pass

        # Stop BlueROV depth status polling
        if getattr(self, "_depth_status_job", None) is not None:
            try:
                self.after_cancel(self._depth_status_job)
            except Exception:
                pass
            self._depth_status_job = None

        # Stop MAVLink seabed-altitude probe
        self._stop_mavlink_probe()

        # Stop Pi logger
        self.stop_pi_logger()

        # Close TCP client and exit
        self.client.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()