#!/usr/bin/env python3
"""
readpin.py — Sony ILX exposure trigger + EKF position logger
Logs position/orientation at each camera exposure for photogrammetry.

Hardware:
  - BlueOS / ArduSub MAVLink on udp 14551
  - Water Linked A50 DVL (via ArduSub EKF)
  - Sony ILX trigger on GPIO pin 14 (falling edge = exposure)

Output:
  logs/YYYY-MM-DD_HH-MM-SS.csv — Metashape/ODM compatible camera reference format
"""

import math
import os
import time
import csv
import threading
from datetime import datetime
from pymavlink import mavutil
import RPi.GPIO as GPIO

# --- Config ---
MAVLINK_CONNECTION  = 'udpin:0.0.0.0:14552'
GPIO_PIN            = 14
BOUNCE_MS           = 200       # debounce time in milliseconds
MAVLINK_STREAM_HZ   = 20        # request stream rate from ArduSub

# --- Logging setup ---
os.makedirs('logs', exist_ok=True)
start_time = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
log_path   = f'logs/{start_time}.csv'

def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    line = f'[{timestamp}] {message}'
    print(line)

# --- Shared EKF state (updated by MAVLink thread) ---
state = {
    'lat':      None,
    'lon':      None,
    'alt':      None,
    'roll':     None,
    'pitch':    None,
    'yaw':      None,
    'altitude': None,   # metres off seabed from A50 via RANGEFINDER
}
state_lock   = threading.Lock()
ekf_ready    = threading.Event()

# --- Image counter + CSV writer (accessed from GPIO callback) ---
image_index  = 0
csv_file     = None
csv_writer   = None
csv_lock     = threading.Lock()


# ---------------------------------------------------------------------------
# MAVLink background thread — keeps state cache fresh, non-blocking for GPIO
# ---------------------------------------------------------------------------
def mavlink_thread(mav):
    """Continuously drain MAVLink messages and update shared state cache."""
    while True:
        msg = mav.recv_match(
            type=['GLOBAL_POSITION_INT', 'ATTITUDE', 'RANGEFINDER'],
            blocking=True,
            timeout=1
        )
        if msg is None:
            continue

        t = msg.get_type()
        with state_lock:
            if t == 'GLOBAL_POSITION_INT':
                state['lat'] = msg.lat / 1e7
                state['lon'] = msg.lon / 1e7
                state['alt'] = msg.relative_alt / 1000.0   # mm → metres
            elif t == 'ATTITUDE':
                state['roll']  = math.degrees(msg.roll)
                state['pitch'] = math.degrees(msg.pitch)
                state['yaw']   = math.degrees(msg.yaw)
            elif t == 'RANGEFINDER':
                state['altitude'] = msg.distance            # metres off seabed

            # Signal ready once we have position + attitude
            if all(state[k] is not None for k in ['lat', 'lon', 'alt', 'roll', 'pitch', 'yaw']):
                ekf_ready.set()


# ---------------------------------------------------------------------------
# GPIO interrupt callback — fires on falling edge (exposure detected)
# ---------------------------------------------------------------------------
def exposure_detected(channel):
    global image_index

    with state_lock:
        snap = dict(state)  # snapshot current state atomically

    with csv_lock:
        idx = image_index
        image_index += 1

    image_name = f"frame_{idx:06d}"
    ready = all(snap[k] is not None for k in ['lat', 'lon', 'alt', 'roll', 'pitch', 'yaw'])

    if ready:
        altitude = snap['altitude'] if snap['altitude'] is not None else 0.0
        row = [
            image_name,
            f"{snap['lat']:.8f}",
            f"{snap['lon']:.8f}",
            f"{snap['alt']:.4f}",
            f"{snap['roll']:.4f}",
            f"{snap['pitch']:.4f}",
            f"{snap['yaw']:.4f}",
            "0.1",      # horizontal accuracy (DVL ~0.1m)
            "0.1",      # vertical accuracy
        ]
        with csv_lock:
            csv_writer.writerow(row)
            csv_file.flush()

        log(
            f"EXPOSED {image_name} | "
            f"lat={snap['lat']:.6f} lon={snap['lon']:.6f} alt={snap['alt']:.3f}m | "
            f"roll={snap['roll']:.2f} pitch={snap['pitch']:.2f} yaw={snap['yaw']:.2f} | "
            f"seabed_alt={altitude:.3f}m"
        )
    else:
        # Still log the row with whatever we have so image count stays in sync
        row = [image_name, '', '', '', '', '', '', '', '']
        with csv_lock:
            csv_writer.writerow(row)
            csv_file.flush()
        log(f"EXPOSED {image_name} | WARNING: EKF state not ready — position not logged")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global csv_file, csv_writer

    # --- GPIO setup ---
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(GPIO_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # --- MAVLink connection ---
    log(f"Connecting to MAVLink on {MAVLINK_CONNECTION} ...")
    mav = mavutil.mavlink_connection(MAVLINK_CONNECTION)
    mav.wait_heartbeat()
    log(f"MAVLink connected — system {mav.target_system} component {mav.target_component}")

    mav.mav.request_data_stream_send(
        mav.target_system,
        mav.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL,
        MAVLINK_STREAM_HZ, 1
    )

    # --- Start MAVLink background thread ---
    t = threading.Thread(target=mavlink_thread, args=(mav,), daemon=True)
    t.start()

    # --- Wait for first valid EKF state before arming GPIO ---
    log("Waiting for EKF state ...")
    ekf_ready.wait(timeout=15)
    if not ekf_ready.is_set():
        log("WARNING: EKF state not received after 15s — continuing anyway")
    else:
        log("EKF state ready")

    # --- Open CSV ---
    csv_file   = open(log_path, 'w', newline='')
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow([
        'image name',
        'latitude [decimal degrees]',
        'longitude [decimal degrees]',
        'altitude [meter]',
        'roll [degrees]',
        'pitch [degrees]',
        'yaw [degrees]',
        'accuracy horizontal [meter]',
        'accuracy vertical [meter]',
    ])
    csv_file.flush()

    # --- Arm GPIO interrupt ---
    GPIO.add_event_detect(GPIO_PIN, GPIO.FALLING, callback=exposure_detected, bouncetime=BOUNCE_MS)

    log(f"Monitoring started — logging to {log_path}")
    print(f"Logging to {log_path}")
    print("Waiting for exposures... (Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("Monitoring stopped")
        print("\nStopping...")
    finally:
        GPIO.cleanup()
        if csv_file:
            csv_file.close()


if __name__ == '__main__':
    main()