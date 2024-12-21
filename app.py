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

# Constants
RELAY_PIN = 17  # GPIO pin connected to relay
SCHEDULE_FILE = 'schedule.json'
CONFIG_FILE = 'config.json'
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "nanit/babies/56728ce1/temperature"
TEMP_THRESHOLD_F = 66.0  # Temperature threshold in Fahrenheit
DEFAULT_CYCLE_MINUTES = 30  # Default heating cycle duration
LOCK_FILE = '/tmp/boiler_control.lock'

# Global variables
heating_cycle_thread = None
last_temp = None
cycle_duration_minutes = DEFAULT_CYCLE_MINUTES

# Initialize GPIO
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(RELAY_PIN, GPIO.OUT)
GPIO.output(RELAY_PIN, GPIO.LOW)  # Start with relay OFF (LOW)

def c_to_f(celsius):
    return (celsius * 9/5) + 32

def f_to_c(fahrenheit):
    return (fahrenheit - 32) * 5/9

def load_config():
    global cycle_duration_minutes, TEMP_THRESHOLD_F
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            cycle_duration_minutes = config.get('cycle_duration_minutes', DEFAULT_CYCLE_MINUTES)
            TEMP_THRESHOLD_F = config.get('temp_threshold_f', 66.0)
    except FileNotFoundError:
        save_config()

def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump({
            'cycle_duration_minutes': cycle_duration_minutes,
            'temp_threshold_f': TEMP_THRESHOLD_F
        }, f)

def heating_cycle():
    """Run one heating cycle"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] ====== STARTING HEATING CYCLE ======")
    print(f"[{timestamp}] Duration: {cycle_duration_minutes} minutes")
    print(f"[{timestamp}] Temperature threshold: {TEMP_THRESHOLD_F}°F")
    
    GPIO.output(RELAY_PIN, GPIO.HIGH)  # Turn ON
    
    time.sleep(cycle_duration_minutes * 60)  # Convert minutes to seconds
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    GPIO.output(RELAY_PIN, GPIO.LOW)  # Turn OFF
    print(f"[{timestamp}] ====== HEATING CYCLE COMPLETED ======")

def check_temperature(temperature_c):
    """Start a heating cycle if temperature is below threshold and no cycle is running"""
    global heating_cycle_thread
    
    temperature_f = c_to_f(temperature_c)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] Temperature check: {temperature_f:.1f}°F (threshold: {TEMP_THRESHOLD_F}°F)")
    
    # If temperature is below threshold and no heating cycle is running
    if temperature_f < TEMP_THRESHOLD_F:
        if heating_cycle_thread is None or not heating_cycle_thread.is_alive():
            print(f"[{timestamp}] Temperature {temperature_f:.1f}°F is below threshold {TEMP_THRESHOLD_F}°F - starting new heating cycle")
            heating_cycle_thread = threading.Thread(target=heating_cycle)
            heating_cycle_thread.daemon = True
            heating_cycle_thread.start()
        else:
            print(f"[{timestamp}] Temperature {temperature_f:.1f}°F is below threshold but heating cycle is already running")
    else:
        print(f"[{timestamp}] Temperature {temperature_f:.1f}°F is above threshold - no action needed")

def stop_heating_cycle():
    """Stop the current heating cycle if one is running"""
    global heating_cycle_thread, cycle_trigger_source
    if heating_cycle_thread and heating_cycle_thread.is_alive():
        GPIO.output(RELAY_PIN, GPIO.LOW)  # Turn OFF
        heating_cycle_thread = None  # Clear the thread
        cycle_trigger_source = None  # Clear the trigger source
        print("Manually stopped heating cycle")
    return True

# MQTT callbacks
def on_connect(client, userdata, flags, rc):
    print(f"MQTT Connection result: {rc}")
    if rc == 0:
        print(f"Successfully subscribed to {MQTT_TOPIC}")
        client.subscribe(MQTT_TOPIC)
    else:
        print(f"Failed to connect to MQTT broker with code {rc}")

def on_message(client, userdata, msg):
    global last_temp
    print(f"MQTT message received: {msg.payload}")
    try:
        temperature_c = float(msg.payload.decode())
        temperature_f = c_to_f(temperature_c)
        last_temp = temperature_c
        print(f"Processed temperature: {temperature_f:.1f}°F")
        check_temperature(temperature_c)
    except ValueError as e:
        print(f"Error processing temperature: {e}")

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/status', methods=['GET'])
def get_status():
    cycle_running = heating_cycle_thread is not None and heating_cycle_thread.is_alive()
    return jsonify({
        'current_temperature': c_to_f(last_temp) if last_temp is not None else None,
        'threshold': TEMP_THRESHOLD_F,
        'cycle_running': cycle_running,
        'cycle_duration_minutes': cycle_duration_minutes
    })

@app.route('/config', methods=['POST'])
def update_config():
    global cycle_duration_minutes, TEMP_THRESHOLD_F
    data = request.json
    if 'cycle_duration_minutes' in data:
        cycle_duration_minutes = int(data['cycle_duration_minutes'])
    if 'temp_threshold_f' in data:
        TEMP_THRESHOLD_F = float(data['temp_threshold_f'])
    save_config()
    return jsonify({'success': True})

@app.route('/manual', methods=['POST'])
def manual_control():
    action = request.json.get('action')
    if action == 'start':
        global heating_cycle_thread
        if not heating_cycle_thread or not heating_cycle_thread.is_alive():
            heating_cycle_thread = threading.Thread(target=heating_cycle)
            heating_cycle_thread.daemon = True
            heating_cycle_thread.start()
            print("Manually started heating cycle")
        return jsonify({'success': True})
    elif action == 'stop':
        success = stop_heating_cycle()
        return jsonify({'success': success})
    return jsonify({'success': False, 'error': 'Invalid action'})

if __name__ == '__main__':
    # Try to get lock file
    fp = open(LOCK_FILE, 'w')
    try:
        fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Another instance is running")
        sys.exit(1)

    # Load configuration
    load_config()
    
    # Initialize MQTT client
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")

    app.run(host='0.0.0.0', port=5000, debug=False)
