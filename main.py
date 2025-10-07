import os
import sys
import subprocess
import socket
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import csv
import datetime
import time
import re
from pathlib import Path

# --- Pillow for JPEG rendering ---
from PIL import Image, ImageTk
from io import BytesIO

# --- Blue Robotics Ping1D (altimeter) ---
try:
    from brping import Ping1D
except Exception:
    Ping1D = None  # we'll show a helpful message if not installed

DEFAULT_HOST = "192.168.2.70"
DEFAULT_PORT = 9000
MANUAL_FILENAME = "Aquorea Mk3 Manual.pdf"  # put this PDF next to main.py

# >>> Set your Sony SDK image folder here (or use the Browse button in the UI)
DEFAULT_IMAGE_DIR = r"C:\Users\Luke Griffin\OneDrive\Desktop\Sony_SDK\build\Release"  # <-- change to your path (Windows example)

# how long we allow between an exposure and its matching image (seconds)
MATCH_TOLERANCE_SEC = 2.0

# Match DSCXXXXX.JPG and capture the number as group(1)
IMAGE_PATTERN = re.compile(r"^DSC(\d+)\.(jpg)$", re.IGNORECASE)

# >>> Path to your compiled Sony SDK C++ executable
CAMERA_APP_EXE = r"C:\Users\Luke Griffin\OneDrive\Desktop\Sony_SDK\build\Release\RemoteCli.exe"

# >>> Where to write stop.txt so your executable can see it (same folder as exe)
STOP_FILE_PATH = os.path.join(os.path.dirname(CAMERA_APP_EXE), "stop.txt")

# --- Live View settings ---
LIVEVIEW_PATH = r"C:\Users\Luke Griffin\OneDrive\Desktop\Sony_SDK\build\Release\LiveView000000.JPG"
LIVEVIEW_REFRESH_MS = 50  # ~20 checks/sec; repaints only on mtime change
LIVEVIEW_TARGET_W = 1024
LIVEVIEW_TARGET_H = 680
LIVEVIEW_ASPECT = LIVEVIEW_TARGET_W / LIVEVIEW_TARGET_H

# --- Altimeter (Ping1D) connection settings ---
PING_CONNECT_MODE = "udp"   # "udp" or "serial"
PING_SERIAL_PORT = "COM3"   # e.g. "COM3" on Windows or "/dev/ttyUSB0" on Linux
PING_SERIAL_BAUD = 115200
PING_UDP_HOST = "192.168.2.2"
PING_UDP_PORT = 9090
PING_REFRESH_MS = 100       # how often to poll distance (ms)


def resource_path(rel_path: str) -> str:
    """Return absolute path to resource, works for dev and PyInstaller."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, rel_path)
    return os.path.join(os.path.abspath("."), rel_path)

def open_file_with_default_app(path: str):
    """Open a file with the OS default application."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])

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
            try: self.sock.close()
            except: pass
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
            try: self.sock.shutdown(socket.SHUT_RDWR)
            except: pass
            try: self.sock.close()
            except: pass
        self.sock = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Strobe / Lamp Controller (TCP) Aquorea Mk3")
        self.geometry("1100x800")

        # --- set up a log file in place of text boxes ---
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.run_log_path = f"session_log_{ts}.txt"

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # Connection row (hidden controls kept as vars)
        row = ttk.Frame(root); row.pack(fill="x", pady=4)

        self.ip_var = tk.StringVar(value=DEFAULT_HOST)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))

        # (Hidden per request)
        # ttk.Label(row, text="IP:").pack(side="left")
        # ttk.Entry(row, textvariable=self.ip_var, width=18).pack(side="left", padx=5)
        # ttk.Label(row, text="Port:").pack(side="left")
        # ttk.Entry(row, textvariable=self.port_var, width=8).pack(side="left", padx=5)
        # ttk.Button(row, text="Connect", command=self.on_connect).pack(side="left", padx=6)
        # ttk.Button(row, text="Disconnect", command=self.on_disconnect).pack(side="left")
        # ttk.Button(row, text="Status",   command=lambda: self.send_cmd("STATUS")).pack(side="left", padx=5)

        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=10, pady=2)

        # Sliders
        sliders = ttk.LabelFrame(root, text="Intensities")
        sliders.pack(fill="x", pady=10)

        srow = ttk.Frame(sliders); srow.pack(fill="x", pady=6)
        ttk.Label(srow, text="Strobe intensity").pack(side="left")
        self.strobe_scale = ttk.Scale(srow, from_=0, to=100, orient="horizontal",
                                      command=lambda v: self._update_val(self.lbl_strobe, v))
        self.strobe_scale.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_strobe = ttk.Label(srow, width=4, anchor="e", text="0"); self.lbl_strobe.pack(side="left")
        self.strobe_scale.bind("<ButtonRelease-1>", lambda e: self.send_cmd(f"STROBE_INTENSITY {int(float(self.strobe_scale.get()))}"))

        lrow = ttk.Frame(sliders); lrow.pack(fill="x", pady=6)
        ttk.Label(lrow, text="Lamp intensity").pack(side="left")
        self.lamp_scale = ttk.Scale(lrow, from_=0, to=100, orient="horizontal",
                                    command=lambda v: self._update_val(self.lbl_lamp, v))
        self.lamp_scale.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_lamp = ttk.Label(lrow, width=4, anchor="e", text="0"); self.lbl_lamp.pack(side="left")
        self.lamp_scale.bind("<ButtonRelease-1>", lambda e: self.send_cmd(f"LAMP_INTENSITY {int(float(self.lamp_scale.get()))}"))

        # Camera Trigger controls
        trig_ctrl = ttk.LabelFrame(root, text="Camera Trigger")
        trig_ctrl.pack(fill="x", pady=10)
        ttk.Button(trig_ctrl, text="Single Trigger",
                   command=lambda: self.send_cmd("TRIGGER")).pack(side="left", padx=5)

        self.trig_time_var = tk.StringVar(value="5")      # default 5 ms
        self.trig_interval_var = tk.StringVar(value="1000")  # default 1000 ms
        ttk.Entry(trig_ctrl, textvariable=self.trig_interval_var, width=8).pack(side="left")

        self.loop_running = False
        self.loop_job = None

        def toggle_loop():
            if not self.loop_running:
                self.start_loop()
            else:
                self.stop_loop()

        self.loop_btn = tk.Button(trig_ctrl, text="Start Loop", command=toggle_loop)
        self.loop_btn.pack(side="left", padx=10)

        # --- Buttons beside Start Loop ---
        ttk.Button(trig_ctrl, text="Lamp OFF",
                   command=lambda: self.send_cmd("LAMP OFF")).pack(side="left", padx=6)

        ttk.Button(trig_ctrl, text="Open Manual (PDF)",
                   command=self.open_manual).pack(side="left", padx=6)

        self.btn_gui = tk.Button(trig_ctrl, text="Show Camera GUI",
                                 command=self.restart_camera_with_gui, width=22)
        self.btn_gui.pack(side="left", padx=6)

        # NEW: Show Latest Image (red/green toggle like GUI)
        self.btn_latest = tk.Button(trig_ctrl, text="Show Latest Image",
                                    command=self.toggle_latest_window, width=22)
        self.btn_latest.pack(side="left", padx=6)
        # ---------------------------------------------------

        # --- Live View panel (dominates remaining space) ---
        live_frame = ttk.LabelFrame(root, text="Live View (1024×680 aspect)")
        live_frame.pack(fill="both", expand=True, pady=8)

        live_frame.rowconfigure(0, weight=1)   # image row expands
        live_frame.rowconfigure(1, weight=0)   # controls row fixed
        live_frame.rowconfigure(2, weight=0)   # altimeter row
        live_frame.columnconfigure(0, weight=1)

        self.live_frame = live_frame
        self.live_label = tk.Label(live_frame, anchor="center", bg="#202020")
        self.live_label.grid(row=0, column=0, sticky="nsew")

        # --- Camera + Arduino control/status buttons under Live View ---
        controls = ttk.Frame(live_frame)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for i in range(4):
            controls.columnconfigure(i, weight=0)
        controls.columnconfigure(4, weight=1)  # spacer

        self.btn_headless = tk.Button(
            controls, text="Live View (Headless)",
            command=self.restart_camera_headless, width=22
        )
        self.btn_headless.grid(row=0, column=0, padx=6, pady=4, sticky="w")

        self.btn_arduino = tk.Button(
            controls, text="Arduino: Retry Connect",
            command=self.retry_arduino_connect, width=22
        )
        self.btn_arduino.grid(row=0, column=1, padx=6, pady=4, sticky="w")

        ttk.Label(controls, text="").grid(row=0, column=4, sticky="ew")  # spacer

        controls = ttk.Frame(live_frame)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for i in range(5):
            controls.columnconfigure(i, weight=0)
        controls.columnconfigure(5, weight=1)  # spacer moved right

        self.btn_headless = tk.Button(
            controls, text="Live View (Headless)",
            command=self.restart_camera_headless, width=22
        )
        self.btn_headless.grid(row=0, column=0, padx=6, pady=4, sticky="w")

        self.btn_arduino = tk.Button(
            controls, text="Arduino: Retry Connect",
            command=self.retry_arduino_connect, width=22
        )
        self.btn_arduino.grid(row=0, column=1, padx=6, pady=4, sticky="w")

        # Altimeter button
        self.btn_altimeter = tk.Button(
            controls, text="Altimeter: Retry Connect",
            command=self.retry_altimeter_connect, width=22
        )
        self.btn_altimeter.grid(row=0, column=2, padx=6, pady=4, sticky="w")

        ttk.Label(controls, text="").grid(row=0, column=5, sticky="ew")

        # --- Altimeter readout label ---
        self.alt_label = ttk.Label(live_frame, text="Altimeter: --.– m (––% confidence)")
        self.alt_label.grid(row=2, column=0, sticky="w", padx=8, pady=(6, 8))

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

        # NEW: Latest Image window state
        self.latest_win = None
        self._latest_label = None
        self._latest_job = None
        self._latest_tk = None
        self._latest_img_pil = None
        self._last_latest_name = None
        self._last_latest_mtime = None
        self._latest_poll_ms = 300  # adjust if needed

        # Kick off periodic refresh & handle resize events
        self.start_liveview()
        self.live_frame.bind("<Configure>", self._on_liveview_resize)

        self.client = TcpClient(self.on_line_received)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # exposure / CSV state
        self.polling_active = False
        self.csv_filename = None
        self.last_logged_count = None

        # image pairing state
        self.image_scan_running = False
        self.seen_images = set()
        self.pending_exposures = []
        self.pending_images = []
        self.run_start_time = None

        # Altimeter state
        self.ping = None
        self._ping_job = None
        self._ping_ok = False  # set True after we successfully read data

        # --- Auto-start the camera app on boot (headless: no arguments) ---
        self.start_camera(headless=True)

        # Start periodic camera status poll to color/disable buttons
        self._schedule_camera_status_poll()

        # --- Auto-connect to Arduino on boot (non-blocking) ---
        self.try_autoconnect_arduino()
        self._schedule_arduino_status_poll()

        # --- Start Altimeter on boot ---
        self.start_altimeter()

        # Let the user know where logs are going
        self.append_log(f"[INFO] Writing logs to: {os.path.abspath(self.run_log_path)}")

        # Initialize GUI button colors/states
        self._refresh_camera_buttons()
        self._refresh_latest_button()

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
                # Start process with stdout/stderr piped so we can log them
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

                # Start/replace reader thread
                self._start_camera_reader(proc)
            except Exception as e:
                messagebox.showerror("Launch failed", f"Could not start camera app:\n{e}")
                self.append_log(f"[RemoteCLI] Launch failed: {e}")
                self.camera_proc = None
                self.camera_mode = None
        self._refresh_camera_buttons()

    def _start_camera_reader(self, proc: subprocess.Popen):
        # Stop previous reader if any (it should end when proc exits, but be safe)
        try:
            if self._camera_reader_thread and self._camera_reader_thread.is_alive():
                # We can't forcibly kill the thread; it'll end once proc stdout closes
                pass
        except Exception:
            pass

        def _reader():
            try:
                if proc.stdout is None:
                    return
                # Iterate lines; handle cases where buffering is odd on Windows
                for line in proc.stdout:
                    line = line.rstrip("\r\n")
                    if line:
                        self.append_log(f"[RemoteCLI] {line}")
                # drain any remaining
                rest = proc.stdout.read()
                if rest:
                    for ln in rest.splitlines():
                        self.append_log(f"[RemoteCLI] {ln}")
            except Exception as e:
                self.append_log(f"[RemoteCLI] [reader error] {e}")
            finally:
                # Ensure mode reflects actual running state
                if proc.poll() is not None and self.camera_proc is proc:
                    self.camera_mode = None
                    self._refresh_camera_buttons()

        self._camera_reader_thread = threading.Thread(target=_reader, daemon=True, name="RemoteCLI-Reader")
        self._camera_reader_thread.start()

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
        set_btn(self.btn_headless, active=(mode == "headless"))
        set_btn(self.btn_gui,      active=(mode == "gui"))

    # ---------- NEW: Latest Image popup ----------
    def toggle_latest_window(self):
        if self.latest_win and tk.Toplevel.winfo_exists(self.latest_win):
            # close it
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

        container = ttk.Frame(self.latest_win)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self._latest_label = tk.Label(container, anchor="center", bg="#101010")
        self._latest_label.grid(row=0, column=0, sticky="nsew")

        # bind resize to re-render cached image to the new size
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
        self._latest_img_pil = None
        self._latest_tk = None
        self._last_latest_name = None
        self._last_latest_mtime = None
        self._refresh_latest_button()

    def _refresh_latest_button(self):
        open_ = bool(self.latest_win and tk.Toplevel.winfo_exists(self.latest_win))
        try:
            if open_:
                self.btn_latest.config(text="Latest Image: Open", state="disabled",
                                       bg="#2e7d32", fg="white", activebackground="#2e7d32")
            else:
                self.btn_latest.config(text="Show Latest Image", state="normal",
                                       bg="#b71c1c", fg="white", activebackground="#b71c1c")
        except Exception:
            # fallback if classic colors not supported
            self.btn_latest.config(state=("disabled" if open_ else "normal"))

    def _schedule_latest_poll(self):
        self._poll_latest_once()
        self._latest_job = self.after(self._latest_poll_ms, self._schedule_latest_poll)

    def _poll_latest_once(self):
        if not (self.latest_win and tk.Toplevel.winfo_exists(self.latest_win)):
            return
        folder = Path(DEFAULT_IMAGE_DIR)
        try:
            newest = self._find_newest_image(folder)
        except Exception as e:
            newest = None
            # Optional: could log error
        if newest is None:
            # show waiting text
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
            # compute target size inside the label
            w = max(1, self._latest_label.winfo_width())
            h = max(1, self._latest_label.winfo_height())
            # keep aspect ratio, fit inside label
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
    # -----------------------------------------------

    # ---------- Arduino auto-connect & status ----------
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

    def retry_altimeter_connect(self):
        """Manual retry from the Altimeter button; non-blocking."""
        try:
            self.btn_altimeter.config(state="disabled")
        except Exception:
            pass
        if getattr(self, "_ping_job", None) is not None:
            try:
                self.after_cancel(self._ping_job)
            except Exception:
                pass
            self._ping_job = None
        self.ping = None
        self._ping_ok = False
        self.alt_label.config(text="Altimeter: reconnecting…")
        self._refresh_altimeter_button()
        self.start_altimeter()

    def _refresh_altimeter_button(self):
        """Green+disabled when reading OK; red+clickable otherwise."""
        if Ping1D is None:
            text = "Altimeter: Install lib"
            state = "normal"
            bg = "#b71c1c"
        else:
            if self.ping and self._ping_ok:
                text = "Altimeter: Connected"
                state = "disabled"
                bg = "#2e7d32"
            else:
                text = "Altimeter: Retry Connect"
                state = "normal"
                bg = "#b71c1c"
        try:
            self.btn_altimeter.config(text=text, state=state, bg=bg, fg="white", activebackground=bg)
        except Exception:
            self.btn_altimeter.config(text=text, state=state)

    # ---------- UI helpers ----------
    def browse_image_dir(self):
        d = filedialog.askdirectory(initialdir=DEFAULT_IMAGE_DIR,
                                    title="Select image folder (Sony SDK output)")
        if d:
            self.image_dir_var = tk.StringVar(value=d)

    def _update_val(self, label, v):
        try:
            label.config(text=str(int(float(v))))
        except:
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

    def clear_log(self):
        pass

    def append_rx(self, text):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._write_line_to_file(f"{ts} [RX] {text}")

    def clear_rx(self):
        pass

    def _extract_rx_payload(self, line: str):
        if line.startswith("RS485: "):
            return line[7:]
        tag = "[RS485<-] "
        if line.startswith(tag):
            return line[len(tag):]
        return None

    # ---------- Incoming TCP lines ----------
    def on_line_received(self, line):
        def ui():
            self.append_log(f"<< {line}")
            payload = self._extract_rx_payload(line)
            if payload:
                self.append_rx(payload)

            if line.startswith("EXPOSURE_COUNT "):
                try:
                    val = int(line.split()[1])
                    if self.last_logged_count is None or val != self.last_logged_count:
                        self.last_logged_count = val
                        exp_ts = datetime.datetime.now()
                        self.pending_exposures.append((exp_ts, val))
                        if self.csv_filename and not Path(self.csv_filename).exists():
                            with open(self.csv_filename, "w", newline="") as f:
                                csv.writer(f).writerow(["ExposureTS","ExposureCount","ImageTS","ImageFile","Delta_ms"])
                        self.try_match_pairs()
                except Exception as e:
                    self.append_log(f"[CSV/Pair ERROR] {e}")
        self.after(0, ui)

    # ---------- Continuous trigger loop ----------
    def start_loop(self):
        try:
            hold_ms = int(self.trig_time_var.get())
            interval_ms = int(self.trig_interval_var.get())
            if hold_ms <= 0 or interval_ms <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror("Invalid Input",
                                 "Hold (ms) and Interval (ms) must be positive integers.")
            return

        if interval_ms < hold_ms:
            if not messagebox.askyesno(
                "Interval < Hold",
                f"Interval ({interval_ms} ms) is shorter than hold ({hold_ms} ms).\n"
                "This can queue triggers faster than the camera can finish.\n\n"
                "Start anyway?"
            ):
                return

        self.loop_running = True
        self._set_loop_button(True)
        self.append_log(f"[LOOP] Started: hold={hold_ms} ms, interval={interval_ms} ms")
        self._loop_tick()

    def stop_loop(self):
        self.loop_running = False
        self._set_loop_button(False)
        if self.loop_job is not None:
            try:
                self.after_cancel(self.loop_job)
            except Exception:
                pass
            self.loop_job = None
        self.append_log("[LOOP] Stopped")

    def _loop_tick(self):
        if not self.loop_running:
            return
        try:
            hold_ms = int(self.trig_time_var.get())
            interval_ms = int(self.trig_interval_var.get())
            if hold_ms <= 0 or interval_ms <= 0:
                raise ValueError
        except Exception:
            self.append_log("[LOOP] Invalid inputs; stopping loop.")
            self.stop_loop()
            return

        try:
            self.client.send_line(f"TRIGGER_MS {hold_ms}")
            self.append_log(f">> TRIGGER_MS {hold_ms}")
        except Exception as e:
            messagebox.showwarning("Send failed", str(e))
            self.stop_loop()
            return

        self.loop_job = self.after(interval_ms, self._loop_tick)

    def _set_loop_button(self, running: bool):
        if running:
            try:
                self.loop_btn.config(text="Stop Loop", bg="green", activebackground="green", fg="white")
            except Exception:
                self.loop_btn.config(text="Stop Loop")
        else:
            try:
                self.loop_btn.config(text="Start Loop", bg=self.cget("bg"), activebackground=self.cget("bg"), fg="black")
            except Exception:
                self.loop_btn.config(text="Start Loop")

    # ---------- Exposure controls ----------
    def start_exposure_count(self):
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.csv_filename = f"exposure_log_{ts}.csv"
        try:
            with open(self.csv_filename, "w", newline="") as f:
                csv.writer(f).writerow(["ExposureTS","ExposureCount","ImageTS","ImageFile","Delta_ms"])
            self.append_log(f"[CSV] Logging exposures to {self.csv_filename}")
        except Exception as e:
            self.append_log(f"[CSV ERROR] {e}")

        self.pending_exposures.clear()
        self.pending_images.clear()
        self.seen_images.clear()
        self.run_start_time = datetime.datetime.now()

        # Keep DEFAULT_IMAGE_DIR
        self.image_dir_var = tk.StringVar(value=DEFAULT_IMAGE_DIR)
        self.snapshot_existing_images()

        self.polling_active = True
        self.last_logged_count = None
        self.send_cmd("START_EXPOSURE_COUNT")
        self.poll_exposure_count()
        if not self.image_scan_running:
            self.image_scan_running = True
            self.after(300, self.scan_image_folder)

    def stop_exposure_count(self):
        self.send_cmd("STOP_EXPOSURE_COUNT")
        self.polling_active = False
        self.append_log("[CSV] Exposure logging stopped")

    def poll_exposure_count(self):
        if not self.polling_active:
            return
        try:
            self.client.send_line("GET_EXPOSURE_COUNT")
        except Exception:
            return
        self.after(500, self.poll_exposure_count)

    # ---------- Image monitoring & pairing ----------
    def snapshot_existing_images(self):
        folder = Path(self.image_dir_var.get())
        try:
            if not folder.exists():
                self.append_log(f"[IMG] Folder not found: {folder}")
                return
            for p in folder.iterdir():
                if p.is_file() and IMAGE_PATTERN.match(p.name):
                    self.seen_images.add(p.name)
        except Exception as e:
            self.append_log(f"[IMG SNAPSHOT ERROR] {e}")

    def scan_image_folder(self):
        folder = Path(self.image_dir_var.get())
        if not self.image_scan_running:
            return
        try:
            if folder.exists():
                for p in folder.iterdir():
                    if not p.is_file():
                        continue
                    name = p.name
                    if name in self.seen_images:
                        continue
                    if not IMAGE_PATTERN.match(name):
                        continue
                    try:
                        mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime)
                    except Exception:
                        continue
                    if self.run_start_time and mtime < self.run_start_time - datetime.timedelta(seconds=1):
                        self.seen_images.add(name)
                        continue
                    self.seen_images.add(name)
                    self.pending_images.append((mtime, name))
                    self.append_log(f"[IMG] New file: {name} @ {mtime.strftime('%H:%M:%S.%f')[:-3]}")
                self.try_match_pairs()
        except Exception as e:
            self.append_log(f"[IMG SCAN ERROR] {e}")
        self.after(300, self.scan_image_folder)

    def try_match_pairs(self):
        if not self.csv_filename:
            return
        if not self.pending_exposures or not self.pending_images:
            return

        self.pending_exposures.sort(key=lambda x: x[0])
        self.pending_images.sort(key=lambda x: x[0])

        matched_exposures = []
        matched_images_idx = set()

        for ei, (ets, ecount) in enumerate(self.pending_exposures):
            best_idx = None
            best_dt = None
            for ii, (its, fname) in enumerate(self.pending_images):
                if ii in matched_images_idx:
                    continue
                dt = abs((its - ets).total_seconds())
                if best_dt is None or dt < best_dt:
                    best_dt = dt
                    best_idx = ii
            if best_idx is not None and best_dt is not None and best_dt <= MATCH_TOLERANCE_SEC:
                matched_exposures.append(ei)
                matched_images_idx.add(best_idx)

        if matched_exposures:
            with open(self.csv_filename, "a", newline="") as f:
                writer = csv.writer(f)
                for ei in sorted(matched_exposures, reverse=True):
                    ets, ecount = self.pending_exposures[ei]
                    best_idx2 = None
                    best_dt2 = None
                    for ii, (its, fname) in enumerate(self.pending_images):
                        if ii not in matched_images_idx:
                            continue
                        dt = abs((its - ets).total_seconds())
                        if best_dt2 is None or dt < best_dt2:
                            best_dt2 = dt
                            best_idx2 = ii
                    if best_idx2 is None:
                        for ii, (its, fname) in enumerate(self.pending_images):
                            dt = abs((its - ets).total_seconds())
                            if best_dt2 is None or dt < best_dt2:
                                best_dt2 = dt
                                best_idx2 = ii
                    if best_idx2 is not None and best_dt2 is not None and best_dt2 <= MATCH_TOLERANCE_SEC:
                        its, fname = self.pending_images[best_idx2]
                        delta_ms = int(round((its - ets).total_seconds() * 1000.0))
                        writer.writerow([
                            ets.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                            ecount,
                            its.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                            fname,
                            delta_ms
                        ])
                        self.append_log(f"[PAIR] Exposure #{ecount} @ {ets.strftime('%H:%M:%S.%f')[:-3]}  <->  {fname} @ {its.strftime('%H:%M:%S.%f')[:-3]}  (Δ {delta_ms} ms)")
                        try:
                            self.pending_images.pop(best_idx2)
                        except Exception:
                            pass
                        self.pending_exposures.pop(ei)

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

    # ---------- Altimeter helpers ----------
    def start_altimeter(self):
        """Initialize Ping1D and start periodic distance polling."""
        self._ping_ok = False
        if Ping1D is None:
            self.alt_label.config(text="Altimeter: library not installed (pip install bluerobotics-ping)")
            self._refresh_altimeter_button()
            return
        try:
            p = Ping1D()
            if PING_CONNECT_MODE.lower() == "serial":
                p.connect_serial(PING_SERIAL_PORT, PING_SERIAL_BAUD)
            else:
                p.connect_udp(PING_UDP_HOST, int(PING_UDP_PORT))
            if p.initialize() is False:
                self.alt_label.config(text="Altimeter: failed to initialize")
                self.ping = None
                self._refresh_altimeter_button()
                return
            self.ping = p
            self.alt_label.config(text="Altimeter: connected, reading…")
            self._refresh_altimeter_button()
            self._schedule_ping_poll()
        except Exception as e:
            self.ping = None
            self.alt_label.config(text=f"Altimeter: error — {e}")
            self._refresh_altimeter_button()

    def _schedule_ping_poll(self):
        self._poll_ping_once()
        self._ping_job = self.after(PING_REFRESH_MS, self._schedule_ping_poll)

    def _poll_ping_once(self):
        if not self.ping:
            self._ping_ok = False
            self._refresh_altimeter_button()
            return
        try:
            data = self.ping.get_distance()
            if data:
                dist_m = data.get("distance", 0) / 1000.0  # mm -> m
                conf = data.get("confidence", 0)
                self.alt_label.config(text=f"Altimeter: {dist_m:.1f} m ({conf}% confidence)")
                self._ping_ok = True
            else:
                self.alt_label.config(text="Altimeter: no data")
                self._ping_ok = False
        except Exception:
            self._ping_ok = False
        finally:
            self._refresh_altimeter_button()

    # ---------- Hidden Connect / Disconnect (still programmatic) ----------
    def on_connect(self):
        host = self.ip_var.get().strip()
        try:
            port = int(self.port_var.get())
            self.client.connect(host, port)
            self.append_log(f"[Connected to {host}:{port}]")
        except Exception as e:
            messagebox.showerror("Connect failed", str(e))

    def on_disconnect(self):
        self.client.close()
        self.append_log("[Disconnected]")

    # ---------- Sending ----------
    def send_cmd(self, s):
        try:
            self.client.send_line(s)
            self.append_log(f">> {s}")
        except Exception as e:
            messagebox.showwarning("Send failed", str(e))

    def send_raw(self):
        try:
            text = self.cmd_var.get()
            self.client.send_line(text)
            self.append_log(f">> {text}")
        except Exception as e:
            messagebox.showwarning("Send failed", str(e))

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
        self.image_scan_running = False
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

        # Stop Altimeter polling
        if self._ping_job is not None:
            try:
                self.after_cancel(self._ping_job)
            except Exception:
                pass
            self._ping_job = None
        self.ping = None

        # Stop continuous trigger loop if running
        if getattr(self, "loop_running", False):
            self.stop_loop()

        # Close TCP client and exit
        self.client.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
