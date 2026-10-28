"""
In-process access to the Sony camera through SonyCam.dll.

SonyCam.dll is built from the Sony_SDK repo (CMake target "SonyCam", C API in
app/SonyCamApi.h). It contains the same CameraDevice code as RemoteCli.exe,
but instead of running it as a separate program and scraping its console
output, the GUI calls the camera directly:

    cam = SonyCamera(lib_dir, on_log=print, on_image=print)
    cam.load()                       # load the DLL + init the Sony SDK
    cam.connect()                    # first camera found
    cam.set_save_folder("C:/images")
    cam.capture()                    # on_capture() fires once the camera has saved it
                                     # on_image(path) fires if it is also sent to the PC
    jpeg = cam.get_live_view()       # bytes, or None if no new frame

All methods block until the camera answers, so call them from a worker
thread, not the Tk main loop. The callbacks run on SDK threads: only hand
the data to the GUI thread (e.g. put it on a queue) from inside them.
"""
import contextlib
import ctypes
import os
import sys
import threading
from pathlib import Path

API_VERSION = 2

OK = 0
_ERRORS = {
    -1: "Sony SDK not initialised",
    -2: "no camera found",
    -3: "could not connect to the camera",
    -4: "camera not connected",
    -5: "buffer too small",
    -6: "camera command failed",
    -7: "invalid argument",
}
ERR_BUFFER = -5

# Settings understood by get_setting / set_setting
SETTING_ISO = 0
SETTING_SHUTTER_SPEED = 1
SETTING_APERTURE = 2
SETTING_EXPOSURE_MODE = 3
SETTING_FOCUS_MODE = 4

# Index order matches sonycam_set_store_destination
STORE_DESTINATIONS = ("PC", "Camera", "PC + Camera")

_TEXT_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_char_p)
_EVENT_CALLBACK = ctypes.CFUNCTYPE(None)
_NO_CALLBACK = _TEXT_CALLBACK()  # NULL function pointers
_NO_EVENT_CALLBACK = _EVENT_CALLBACK()

_LIVE_VIEW_START_BYTES = 2 * 1024 * 1024
_LIVE_VIEW_MAX_BYTES = 32 * 1024 * 1024

# chdir() is process wide; serialise the calls that need it.
_cwd_lock = threading.Lock()


class SonyCameraError(RuntimeError):
    def __init__(self, what, code):
        super().__init__(f"{what}: {_ERRORS.get(code, f'error {code}')}")
        self.code = code


def library_filename():
    if sys.platform.startswith("win"):
        return "SonyCam.dll"
    if sys.platform == "darwin":
        return "libSonyCam.dylib"
    return "libSonyCam.so"


@contextlib.contextmanager
def _working_dir(path):
    # The Sony SDK finds its CrAdapter plug-ins relative to where it runs,
    # just like RemoteCli.exe did when launched from the Release folder.
    with _cwd_lock:
        old = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(old)


class SonyCamera:
    def __init__(self, lib_dir, on_log=None, on_image=None, on_capture=None):
        """
        lib_dir:    folder holding SonyCam.dll, Cr_Core.dll and CrAdapter/.
        on_log:     called with each line the camera code logs.
        on_image:   called with the full path of each image saved on the PC.
        on_capture: called (no arguments) each time the camera has saved a
                    shot to its own memory, whatever the store destination.
        """
        self.lib_dir = Path(lib_dir)
        self.on_log = on_log
        self.on_image = on_image
        self.on_capture = on_capture
        self._lib = None
        self._dll_dir_handle = None
        self._lv_buf = None
        # ctypes callbacks must stay referenced while the DLL may call them
        self._log_cb = _TEXT_CALLBACK(self._handle_log)
        self._image_cb = _TEXT_CALLBACK(self._handle_image)
        self._capture_cb = _EVENT_CALLBACK(self._handle_capture)

    # ---------- callbacks (SDK threads) ----------
    def _handle_log(self, raw):
        if self.on_log is not None and raw:
            try:
                self.on_log(raw.decode("utf-8", errors="replace"))
            except Exception:
                pass

    def _handle_image(self, raw):
        if self.on_image is not None and raw:
            try:
                self.on_image(raw.decode("utf-8", errors="replace"))
            except Exception:
                pass

    def _handle_capture(self):
        if self.on_capture is not None:
            try:
                self.on_capture()
            except Exception:
                pass

    # ---------- setup ----------
    @property
    def loaded(self):
        return self._lib is not None

    @property
    def library_path(self):
        return self.lib_dir / library_filename()

    def load(self):
        """Load SonyCam.dll and initialise the Sony SDK (once per process)."""
        if self._lib is not None:
            return
        path = self.library_path
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found. Build the SonyCam target in the Sony_SDK repo "
                f"and copy it next to Cr_Core.dll."
            )
        if hasattr(os, "add_dll_directory"):
            # Let Windows find Cr_Core.dll / OpenCV DLLs beside SonyCam.dll
            self._dll_dir_handle = os.add_dll_directory(str(self.lib_dir))
        os.environ["PATH"] = str(self.lib_dir) + os.pathsep + os.environ.get("PATH", "")

        lib = ctypes.CDLL(str(path))
        self._declare(lib)
        version = lib.sonycam_api_version()
        if version != API_VERSION:
            raise RuntimeError(
                f"{path.name} API version {version} does not match this GUI "
                f"(expects {API_VERSION}); rebuild SonyCam."
            )
        lib.sonycam_set_log_callback(self._log_cb)
        lib.sonycam_set_download_callback(self._image_cb)
        lib.sonycam_set_capture_callback(self._capture_cb)
        with _working_dir(self.lib_dir):
            rc = lib.sonycam_init()
        if rc != OK:
            lib.sonycam_set_log_callback(_NO_CALLBACK)
            lib.sonycam_set_download_callback(_NO_CALLBACK)
            lib.sonycam_set_capture_callback(_NO_EVENT_CALLBACK)
            raise SonyCameraError("SDK init", rc)
        self._lib = lib

    @staticmethod
    def _declare(lib):
        c_int, c_char_p = ctypes.c_int, ctypes.c_char_p
        sigs = {
            "sonycam_api_version": ([], c_int),
            "sonycam_set_log_callback": ([_TEXT_CALLBACK], None),
            "sonycam_set_download_callback": ([_TEXT_CALLBACK], None),
            "sonycam_set_capture_callback": ([_EVENT_CALLBACK], None),
            "sonycam_init": ([], c_int),
            "sonycam_shutdown": ([], None),
            "sonycam_connect": ([c_int, c_int], c_int),
            "sonycam_disconnect": ([], None),
            "sonycam_is_connected": ([], c_int),
            "sonycam_get_model": ([c_char_p, c_int], c_int),
            "sonycam_set_save_info": ([c_char_p, c_char_p, c_int], c_int),
            "sonycam_set_store_destination": ([c_int], c_int),
            "sonycam_get_store_destination": ([], c_int),
            "sonycam_capture": ([], c_int),
            "sonycam_get_live_view": ([ctypes.POINTER(ctypes.c_ubyte), c_int], c_int),
            "sonycam_get_setting": (
                [c_int, c_char_p, c_int, c_char_p, c_int, ctypes.POINTER(c_int)], c_int),
            "sonycam_set_setting": ([c_int, c_int], c_int),
        }
        for name, (args, res) in sigs.items():
            fn = getattr(lib, name)
            fn.argtypes = args
            fn.restype = res

    def close(self):
        """Disconnect, release the SDK and stop all callbacks."""
        if self._lib is None:
            return
        lib, self._lib = self._lib, None
        try:
            lib.sonycam_shutdown()
        finally:
            lib.sonycam_set_log_callback(_NO_CALLBACK)
            lib.sonycam_set_download_callback(_NO_CALLBACK)
            lib.sonycam_set_capture_callback(_NO_EVENT_CALLBACK)

    def _api(self):
        if self._lib is None:
            raise SonyCameraError("camera", -1)
        return self._lib

    @staticmethod
    def _check(what, rc):
        if rc < 0:
            raise SonyCameraError(what, rc)
        return rc

    # ---------- connection ----------
    def connect(self, camera_index=0, timeout_ms=10000):
        lib = self._api()
        with _working_dir(self.lib_dir):
            self._check("connect", lib.sonycam_connect(camera_index, timeout_ms))

    def disconnect(self):
        if self._lib is not None:
            self._lib.sonycam_disconnect()

    def is_connected(self):
        return self._lib is not None and self._lib.sonycam_is_connected() == 1

    def model(self):
        buf = ctypes.create_string_buffer(256)
        self._check("model", self._api().sonycam_get_model(buf, len(buf)))
        return buf.value.decode("utf-8", errors="replace")

    # ---------- saving ----------
    def set_save_folder(self, folder, prefix="", start_no=-1):
        """Folder (created if missing) where the camera's PC copies are written."""
        self._check("set save folder", self._api().sonycam_set_save_info(
            str(folder).encode("utf-8"), prefix.encode("utf-8"), start_no))

    def set_store_destination(self, index):
        """0 = PC, 1 = memory card, 2 = PC + card (see STORE_DESTINATIONS)."""
        self._check("set store destination", self._api().sonycam_set_store_destination(index))

    def get_store_destination(self):
        return self._check("get store destination", self._api().sonycam_get_store_destination())

    # ---------- shooting ----------
    def capture(self):
        self._check("capture", self._api().sonycam_capture())

    def get_live_view(self):
        """Latest live view JPEG as bytes, or None if no new frame is ready."""
        lib = self._api()
        if self._lv_buf is None:
            self._lv_buf = (ctypes.c_ubyte * _LIVE_VIEW_START_BYTES)()
        while True:
            n = lib.sonycam_get_live_view(self._lv_buf, len(self._lv_buf))
            if n == ERR_BUFFER and len(self._lv_buf) < _LIVE_VIEW_MAX_BYTES:
                self._lv_buf = (ctypes.c_ubyte * (len(self._lv_buf) * 2))()
                continue
            self._check("live view", n)
            return ctypes.string_at(self._lv_buf, n) if n > 0 else None

    # ---------- settings ----------
    def get_setting(self, setting):
        """Return (current, options, writable) for one of the SETTING_* values."""
        current = ctypes.create_string_buffer(256)
        options = ctypes.create_string_buffer(64 * 1024)
        writable = ctypes.c_int(0)
        self._check("get setting", self._api().sonycam_get_setting(
            setting, current, len(current), options, len(options), ctypes.byref(writable)))
        opts = options.value.decode("utf-8", errors="replace")
        return (current.value.decode("utf-8", errors="replace"),
                opts.split("\n") if opts else [],
                bool(writable.value))

    def set_setting(self, setting, index):
        """Select options[index] from get_setting(setting)."""
        self._check("set setting", self._api().sonycam_set_setting(setting, index))
