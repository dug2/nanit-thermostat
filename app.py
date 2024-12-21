from flask import Flask, render_template, jsonify, request
from datetime import datetime
import json
import RPi.GPIO as GPIO
from apscheduler.schedulers.background import BackgroundScheduler
import threading
import time
import paho.mqtt.client as mqtt
import os
import sys
import fcntl

app = Flask(__name__)

# Default configurations - these can be overridden by config.json
DEFAULT_CONFIG = {
    "relay_pin": 17,
    "mqtt_broker": "localhost",
    "mqtt_port": 1883,
    "sensors": {
        "sensor1": {
            "name": "Room 1",
            "mqtt_topic": "sensors/room1/temperature",
            "threshold": 66.0
        },
        "sensor2": {
            "name": "Room 2",
            "mqtt_topic": "sensors/room2/temperature",
            "threshold": 66.0
        }
    },
    "cycle_duration_minutes": 30
}

CONFIG_FILE = 'config.json'
LOCK_FILE = '/tmp/boiler_control.lock'

# Global variables
config = DEFAULT_CONFIG.copy()
heating_cycle_thread = None
last_temps = {sensor_id: None for sensor_id in DEFAULT_CONFIG['sensors'].keys()}
cycle_trigger_source = None

# Initialize GPIO
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(DEFAULT_CONFIG['relay_pin'], GPIO.OUT)
GPIO.output(DEFAULT_CONFIG['relay_pin'], GPIO.LOW)  # Start with relay OFF

def c_to_f(celsius):
    return (celsius * 9/5) + 32

def f_to_c(fahrenheit):
    return (fahrenheit - 32) * 5/9

def load_config():
    """Load configuration from file, falling back to defaults if needed"""
    global config
    try:
        with open(CONFIG_FILE, 'r') as f:
            loaded_config = json.load(f)
            # Deep merge with defaults
            config = DEFAULT_CONFIG.copy()
            config.update(loaded_config)
    except FileNotFoundError:
        save_config()

def save_config():
    """Save current configuration to file"""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def heating_cycle(trigger_source="manual"):
    """Run one heating cycle"""
    global cycle_trigger_source
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] ====== STARTING HEATING CYCLE ======")
    print(f"[{timestamp}] Duration: {config['cycle_duration_minutes']} minutes")
    print(f"[{timestamp}] Triggered by: {trigger_source}")
    
    cycle_trigger_source = trigger_source
    GPIO.output(config['relay_pin'], GPIO.HIGH)  # Turn ON
    
    time.sleep(config['cycle_duration_minutes'] * 60)
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    GPIO.output(config['relay_pin'], GPIO.LOW)  # Turn OFF
    cycle_trigger_source = None
    print(f"[{timestamp}] ====== HEATING CYCLE COMPLETED ======")

def start_heating_cycle(trigger_source):
    """Start a heating cycle if none is running"""
    global heating_cycle_thread
    
    if heating_cycle_thread is None or not heating_cycle_thread.is_alive():
        heating_cycle_thread = threading.Thread(target=heating_cycle, args=(trigger_source,))
        heating_cycle_thread.daemon = True
        heating_cycle_thread.start()
        return True
    return False

def stop_heating_cycle():
    """Stop the current heating cycle if one is running"""
    global heating_cycle_thread, cycle_trigger_source
    if heating_cycle_thread and heating_cycle_thread.is_alive():
        GPIO.output(config['relay_pin'], GPIO.LOW)  # Turn OFF
        heating_cycle_thread = None
        cycle_trigger_source = None
        print("Manually stopped heating cycle")
    return True

def check_temperature(sensor_id, temperature_c):
    temperature_f = c_to_f(temperature_c)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sensor_config = config['sensors'][sensor_id]
    print(f"[{timestamp}] {sensor_config['name']}: {temperature_f:.1f}°F (threshold: {sensor_config['threshold']}°F)")
    
    if temperature_f < sensor_config['threshold']:
        if start_heating_cycle(f"Sensor {sensor_config['name']}"):
            print(f"[{timestamp}] Started heating cycle - {sensor_config['name']} below threshold")
        else:
            print(f"[{timestamp}] Heating cycle already running")
    else:
        print(f"[{timestamp}] Temperature above threshold - no action needed")

# MQTT callbacks
def on_connect(client, userdata, flags, rc):
    print(f"MQTT Connection result: {rc}")
    if rc == 0:
        for sensor_id, sensor_config in config['sensors'].items():
            topic = sensor_config['mqtt_topic']
            print(f"Subscribing to {topic}")
            client.subscribe(topic)

def on_message(client, userdata, msg):
    global last_temps
    try:
        temperature_c = float(msg.payload.decode())
        # Find sensor ID by matching topic
        sensor_id = next(
            (sid for sid, cfg in config['sensors'].items() 
             if cfg['mqtt_topic'] == msg.topic),
            None
        )
        if sensor_id:
            last_temps[sensor_id] = temperature_c
            check_temperature(sensor_id, temperature_c)
    except ValueError as e:
        print(f"Error processing temperature: {e}")

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/status', methods=['GET'])
def get_status():
    cycle_running = heating_cycle_thread is not None and heating_cycle_thread.is_alive()
    return jsonify({
        'temperatures': {
            sensor_id: {
                'name': sensor_config['name'],
                'temperature': c_to_f(last_temps[sensor_id]) if last_temps[sensor_id] is not None else None,
                'threshold': sensor_config['threshold']
            }
            for sensor_id, sensor_config in config['sensors'].items()
        },
        'cycle_running': cycle_running,
        'cycle_duration_minutes': config['cycle_duration_minutes'],
        'cycle_trigger_source': cycle_trigger_source
    })

@app.route('/config', methods=['POST'])
def update_config():
    data = request.json
    if 'cycle_duration_minutes' in data:
        config['cycle_duration_minutes'] = int(data['cycle_duration_minutes'])
    if 'thresholds' in data:
        for sensor_id, threshold in data['thresholds'].items():
            if sensor_id in config['sensors']:
                config['sensors'][sensor_id]['threshold'] = float(threshold)
    save_config()
    return jsonify({'success': True})

@app.route('/manual', methods=['POST'])
def manual_control():
    action = request.json.get('action')
    if action == 'start':
        if start_heating_cycle("manual"):
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Cycle already running'})
    elif action == 'stop':
        success = stop_heating_cycle()
        return jsonify({'success': success})
    return jsonify({'success': False, 'error': 'Invalid action'})

if __name__ == '__main__':
    fp = open(LOCK_FILE, 'w')
    try:
        fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Another instance is running")
        sys.exit(1)

    load_config()
    
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    try:
        mqtt_client.connect(config['mqtt_broker'], config['mqtt_port'], 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")

    app.run(host='0.0.0.0', port=5000, debug=False)
