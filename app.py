#!/usr/bin/env python3

import json
import queue
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from pynput import keyboard as pynput_keyboard

#paths
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
MODELS_DIR  = BASE_DIR / "resources" / "models"

#default config
DEFAULT_CONFIG = {
    "keys": {
        "left":   "Escape",
        "middle": "z",
        "right":  "x"
    },
    "camera": {
        "position": {"x": 0, "y": 6.8, "z": -5},
        "target":   {"x": 0, "y": 0, "z": 0},
        "fov": 35
    },
    "model": {
        "position": {"x": 0, "y": 0, "z": 0},
        "rotation": {"x": 0, "y": 0, "z": 0},
        "scale": 1.0
    }
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
                elif isinstance(v, dict):
                    for kk, vv in v.items():
                        if kk not in cfg[k]:
                            cfg[k][kk] = vv
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

config = load_config()

#sse subscriber registry
_subscribers = []
_subscribers_lock = threading.Lock()

def _broadcast(event):
    msg = f"data: {json.dumps(event)}\n\n"
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)

#global keyboard listener
_key_map = {}
_key_map_lock = threading.Lock()
_pressed = set()

_SPECIAL = {}

def _init_special():
    global _SPECIAL
    _SPECIAL = {
        pynput_keyboard.Key.esc:        "Escape",
        pynput_keyboard.Key.space:      " ",
        pynput_keyboard.Key.enter:      "Enter",
        pynput_keyboard.Key.tab:        "Tab",
        pynput_keyboard.Key.backspace:  "Backspace",
        pynput_keyboard.Key.delete:     "Delete",
        pynput_keyboard.Key.up:         "ArrowUp",
        pynput_keyboard.Key.down:       "ArrowDown",
        pynput_keyboard.Key.left:       "ArrowLeft",
        pynput_keyboard.Key.right:      "ArrowRight",
        pynput_keyboard.Key.shift:      "Shift",
        pynput_keyboard.Key.shift_r:    "Shift",
        pynput_keyboard.Key.ctrl:       "Control",
        pynput_keyboard.Key.ctrl_r:     "Control",
        pynput_keyboard.Key.alt:        "Alt",
        pynput_keyboard.Key.alt_r:      "Alt",
        pynput_keyboard.Key.cmd:        "Meta",
        pynput_keyboard.Key.f1:  "F1",  pynput_keyboard.Key.f2:  "F2",
        pynput_keyboard.Key.f3:  "F3",  pynput_keyboard.Key.f4:  "F4",
        pynput_keyboard.Key.f5:  "F5",  pynput_keyboard.Key.f6:  "F6",
        pynput_keyboard.Key.f7:  "F7",  pynput_keyboard.Key.f8:  "F8",
        pynput_keyboard.Key.f9:  "F9",  pynput_keyboard.Key.f10: "F10",
        pynput_keyboard.Key.f11: "F11", pynput_keyboard.Key.f12: "F12",
    }

def _norm(s):
    return s.lower() if len(s) == 1 else s

def _pynput_to_str(key):
    if isinstance(key, pynput_keyboard.KeyCode):
        return key.char.lower() if key.char else ""
    return _SPECIAL.get(key, "")

def _rebuild_key_map():
    k = config.get("keys", {})
    with _key_map_lock:
        _key_map.clear()
        for slot, cfg_key in [("left",   k.get("left",   "Escape")),
                               ("middle", k.get("middle", "z")),
                               ("right",  k.get("right",  "x"))]:
            _key_map[_norm(cfg_key)] = slot

def _on_press(key):
    nk = _pynput_to_str(key)
    if not nk:
        return
    with _key_map_lock:
        slot = _key_map.get(_norm(nk))
    if slot and nk not in _pressed:
        _pressed.add(nk)
        _broadcast({"type": "keydown", "slot": slot})

def _on_release(key):
    nk = _pynput_to_str(key)
    if not nk:
        return
    with _key_map_lock:
        slot = _key_map.get(_norm(nk))
    if slot and nk in _pressed:
        _pressed.discard(nk)
        _broadcast({"type": "keyup", "slot": slot})

def _start_listener():
    listener = pynput_keyboard.Listener(on_press=_on_press, on_release=_on_release)
    listener.daemon = True
    listener.start()

#flask
app = Flask(__name__, template_folder="templates", static_folder="static")

@app.route("/")
def index():
    return render_template("control.html")

@app.route("/overlay")
def overlay():
    return render_template("overlay.html")

@app.route("/models/<path:filename>")
def serve_model(filename):
    return send_from_directory(MODELS_DIR, filename)

#sse endpoint
@app.route("/api/keys/stream")
def key_stream():
    q = queue.Queue(maxsize=64)
    with _subscribers_lock:
        _subscribers.append(q)

    def generate():
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

#cfg api
@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(config)

@app.route("/api/config", methods=["POST"])
def set_config():
    global config
    data = request.json
    config.update(data)
    save_config(config)
    _rebuild_key_map()
    return jsonify({"ok": True})

@app.route("/api/config/camera", methods=["POST"])
def set_camera():
    global config
    config["camera"] = request.json
    save_config(config)
    return jsonify({"ok": True})

@app.route("/api/config/model", methods=["POST"])
def set_model():
    global config
    config["model"] = request.json
    save_config(config)
    return jsonify({"ok": True})

@app.route("/api/config/keys", methods=["POST"])
def set_keys():
    global config
    config["keys"] = request.json
    save_config(config)
    _rebuild_key_map()
    return jsonify({"ok": True})

#entry point
if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════╗")
    print("║    Sayodevice O3C v1  –  Handcam Overlay         ║")
    print("╠══════════════════════════════════════════════════╣")
    print("║  Control Panel → http://localhost:7027           ║")
    print("║  OBS Overlay   → http://localhost:7027/overlay   ║")
    print("╚══════════════════════════════════════════════════╝")

    _init_special()
    _rebuild_key_map()
    _start_listener()

    def open_browser():
        time.sleep(0.8)
        webbrowser.open("http://localhost:7027")

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=7027, debug=False, threaded=True)
