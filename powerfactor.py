"""
POWER FACTOR CORRECTION SYSTEM - Python Backend
Fetches power data from URL endpoint and serves to Web Dashboard
"""

import os
import time
import json
from datetime import datetime
import threading
import requests
from flask import Flask, jsonify, send_from_directory, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from collections import deque
import warnings
warnings.filterwarnings('ignore')

# URL to fetch real-time power data from (Arduino/ESP32 cloud endpoint)
DATA_URL = os.environ.get('DATA_URL', "https://smartmeter-isps.onrender.com/api/data")
FETCH_INTERVAL = 3  # seconds between fetches

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ============================================
# DATA CLASS FOR POWER FACTOR CORRECTION
# ============================================
class PFCMonitor:
    def __init__(self):
        self.current_data = {
            'voltage': 230.0,
            'current': 0.0,
            'power_factor': 0.82,
            'target_pf': 0.95,
            'real_power': 0.0,
            'apparent_power': 0.0,
            'reactive_power': 0.0,
            'energy_wh': 0.0,
            'energy_kwh': 0.0,
            'relay_state': False,
            'cap1_state': False,
            'cap2_state': False,
            'cap3_state': False,
            'total_capacitance': 0,
            'pf_correction_active': False,
            'load_status': 'NONE',
            'pf_quality': 'POOR',
            'health_status': 'Healthy',
            'timestamp': ''
        }
        
        self.data_history = deque(maxlen=100)
        self.alerts = deque(maxlen=50)
        self.recommendations = deque(maxlen=20)
        self.running = True
        self.last_alert_time = {}
        self.last_data_time = time.time()
        
    def update_from_dict(self, d):
        """Process a data dict (from ESP32 POST or other source)"""
        try:
            voltage = float(d.get('voltage', d.get('Voltage', 230)))
            current = float(d.get('current', d.get('Current', 0)))
            real_power = float(d.get('active_power', d.get('real_power', d.get('realPower', 0))))
            reactive_power = float(d.get('reactive_power', d.get('reactivePower', d.get('Reactive_Power', 0))))
            power_factor = float(d.get('pf', d.get('power_factor', d.get('powerFactor', 0.82))))

            def to_bool(v):
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.lower() in ('1', 'true', 'yes', 'on')
                return bool(v)

            relay_step = d.get('relay_status', -1)
            if isinstance(relay_step, int) and 0 <= relay_step <= 6:
                step_map = {
                    0: (0, 0, 0),
                    1: (0, 1, 0),
                    2: (0, 0, 1),
                    3: (0, 1, 1),
                    4: (1, 0, 0),
                    5: (1, 1, 0),
                    6: (1, 1, 1),
                }
                cap1, cap2, cap3 = step_map[relay_step]
            else:
                r1 = d.get('relay1', d.get('Relay1', d.get('cap1', False)))
                r2 = d.get('relay2', d.get('Relay2', d.get('cap2', False)))
                r3 = d.get('relay3', d.get('Relay3', d.get('cap3', False)))
                cap1 = to_bool(r1)
                cap2 = to_bool(r2)
                cap3 = to_bool(r3)

            # Capacitance: relay1=8uF, relay2=3uF, relay3=3uF
            cap_vals = {'cap1': 8, 'cap2': 3, 'cap3': 3}
            total_cap = (cap_vals['cap1'] if cap1 else 0) + (cap_vals['cap2'] if cap2 else 0) + (cap_vals['cap3'] if cap3 else 0)

            energy_wh = float(d.get('energy_wh', d.get('energy', d.get('Energy', 0))))
            apparent_power = real_power / power_factor if power_factor > 0 else voltage * current

            self.current_data['voltage'] = voltage
            self.current_data['current'] = current
            self.current_data['power_factor'] = power_factor
            self.current_data['real_power'] = real_power
            self.current_data['reactive_power'] = reactive_power
            self.current_data['apparent_power'] = apparent_power
            self.current_data['energy_wh'] = energy_wh
            self.current_data['energy_kwh'] = energy_wh / 1000 if energy_wh else 0
            self.current_data['cap1_state'] = bool(cap1)
            self.current_data['cap2_state'] = bool(cap2)
            self.current_data['cap3_state'] = bool(cap3)
            self.current_data['total_capacitance'] = total_cap
            self.current_data['relay_state'] = bool(cap1 or cap2 or cap3)
            self.current_data['pf_correction_active'] = bool(cap1 or cap2 or cap3)

            if current > 30:
                self.current_data['load_status'] = 'HEAVY'
            elif current > 5:
                self.current_data['load_status'] = 'LIGHT'
            else:
                self.current_data['load_status'] = 'NONE'

            if power_factor >= 0.95:
                self.current_data['pf_quality'] = 'EXCELLENT'
                self.current_data['health_status'] = 'Excellent'
            elif power_factor >= 0.90:
                self.current_data['pf_quality'] = 'GOOD'
                self.current_data['health_status'] = 'Good'
            elif power_factor >= 0.85:
                self.current_data['pf_quality'] = 'ACCEPTABLE'
                self.current_data['health_status'] = 'Monitor'
            else:
                self.current_data['pf_quality'] = 'POOR'
                self.current_data['health_status'] = 'Warning'

            self.current_data['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.data_history.append(self.current_data.copy())
            self.generate_recommendations()
            self.check_alerts()
            socketio.emit('data_update', self.current_data)
            self.last_data_time = time.time()
            return True

        except Exception as e:
            print(f"Process error: {e}")

        return False
    
    def generate_recommendations(self):
        """Generate recommendations based on PF and load"""
        recommendations = []
        pf = self.current_data.get('power_factor', 0.82)
        current = self.current_data.get('current', 0)
        relay = self.current_data.get('relay_state', False)
        target = self.current_data.get('target_pf', 0.95)
        cap_total = self.current_data.get('total_capacitance', 0)
        
        # PF-based recommendations
        if pf < target - 0.10:
            recommendations.append({
                'type': 'critical',
                'message': f'⚠️ VERY LOW POWER FACTOR: {pf:.3f} - Target is {target:.2f}',
                'actions': [
                    'PF correction capacitors should be connected',
                    'Check if relays are functioning properly',
                    f'Need approximately {(target - pf) * 40:.0f}μF more capacitance',
                    'Verify load is inductive (motors, transformers)'
                ],
                'priority': 'HIGH'
            })
        elif pf < target - 0.05:
            recommendations.append({
                'type': 'warning',
                'message': f'📉 Power Factor {pf:.3f} - Below optimal range',
                'actions': [
                    'Monitor PF trend closely',
                    'Consider adding more capacitor stages',
                    'Verify capacitor bank operation',
                    'Check load balancing'
                ],
                'priority': 'MEDIUM'
            })
        elif pf >= target - 0.02:
            recommendations.append({
                'type': 'normal',
                'message': f'✅ EXCELLENT POWER FACTOR: {pf:.3f} - Target achieved!',
                'actions': [
                    'Maintain current operation',
                    'Continue regular monitoring',
                    f'Optimal capacitance: {cap_total:.0f}μF',
                    'Schedule periodic checks'
                ],
                'priority': 'LOW'
            })
        
        # Load-based recommendations
        if current > 40:
            recommendations.append({
                'type': 'warning',
                'message': f'🔴 HIGH LOAD CURRENT: {current:.1f}A',
                'actions': [
                    'Reduce non-critical loads',
                    'Monitor temperature rise',
                    'Check for overload conditions',
                    'Consider load shedding'
                ],
                'priority': 'HIGH'
            })
        elif current > 25:
            recommendations.append({
                'type': 'monitor',
                'message': f'⚠️ ELEVATED LOAD: {current:.1f}A',
                'actions': [
                    'Monitor load trends',
                    'Plan for load distribution',
                    'Check cooling system',
                    'Schedule preventive maintenance'
                ],
                'priority': 'MEDIUM'
            })
        
        # Capacitor status recommendation
        if pf < target - 0.05 and cap_total < 14:
            recommendations.append({
                'type': 'warning',
                'message': f'💡 Insufficient capacitance: {cap_total:.0f}μF connected',
                'actions': [
                    f'Add {(target - pf) * 40:.0f}μF more capacitance',
                    'Check if all capacitors are working',
                    'Consider upgrading capacitor bank',
                    'Verify relay connections'
                ],
                'priority': 'HIGH'
            })
        
        # Energy saving recommendation
        if pf < 0.85 and current > 10:
            loss_percent = (1 - pf) * 100
            recommendations.append({
                'type': 'performance',
                'message': f'💰 Energy loss due to low PF: {loss_percent:.1f}%',
                'actions': [
                    f'Potential savings: Improve PF to save {loss_percent * 0.5:.1f}% on losses',
                    'ROI for capacitor investment: Usually 6-12 months',
                    'Contact electrical contractor for PF correction audit'
                ],
                'priority': 'MEDIUM'
            })
        
        self.recommendations = deque(recommendations, maxlen=20)
        socketio.emit('recommendations_update', {'recommendations': list(self.recommendations)})
    
    def check_alerts(self):
        """Check and generate alerts"""
        alerts = []
        current_time = time.time()
        pf = self.current_data.get('power_factor', 0.82)
        current = self.current_data.get('current', 0)
        target = self.current_data.get('target_pf', 0.95)
        
        # Low PF alert
        if pf < target - 0.12:
            if 'low_pf' not in self.last_alert_time or current_time - self.last_alert_time['low_pf'] > 120:
                alerts.append({
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                    'type': 'critical',
                    'message': f'⚠️ CRITICAL LOW POWER FACTOR: {pf:.3f} - Target: {target:.2f} - Immediate correction needed!'
                })
                self.last_alert_time['low_pf'] = current_time
        elif pf < target - 0.07:
            if 'low_pf_warning' not in self.last_alert_time or current_time - self.last_alert_time['low_pf_warning'] > 300:
                alerts.append({
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                    'type': 'warning',
                    'message': f'⚠️ LOW POWER FACTOR: {pf:.3f} - Consider adding more capacitance'
                })
                self.last_alert_time['low_pf_warning'] = current_time
        
        # High current alert
        if current > 45:
            if 'high_current' not in self.last_alert_time or current_time - self.last_alert_time['high_current'] > 60:
                alerts.append({
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                    'type': 'critical',
                    'message': f'🔴 OVERLOAD: Current {current:.1f}A exceeds safe limit!'
                })
                self.last_alert_time['high_current'] = current_time
        elif current > 35:
            if 'high_current_warning' not in self.last_alert_time or current_time - self.last_alert_time['high_current_warning'] > 300:
                alerts.append({
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                    'type': 'warning',
                    'message': f'⚠️ HIGH LOAD: Current {current:.1f}A - Monitor closely'
                })
                self.last_alert_time['high_current_warning'] = current_time
        
        # PF achievement alert
        if pf >= target - 0.02 and self.current_data.get('relay_state', False):
            if 'pf_target' not in self.last_alert_time or current_time - self.last_alert_time['pf_target'] > 600:
                alerts.append({
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                    'type': 'info',
                    'message': f'✅ TARGET POWER FACTOR ACHIEVED: {pf:.3f} - System working optimally!'
                })
                self.last_alert_time['pf_target'] = current_time
        
        for alert in alerts:
            self.alerts.append(alert)
            socketio.emit('new_alert', alert)

# ============================================
# FLASK API ENDPOINTS
# ============================================

monitor = PFCMonitor()

# HTML Dashboard
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Power Factor Correction Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Inter', sans-serif;
            background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
            color: #eee;
            overflow-x: hidden;
        }
        
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        
        ::-webkit-scrollbar-track {
            background: #2a2a3e;
            border-radius: 10px;
        }
        
        ::-webkit-scrollbar-thumb {
            background: #667eea;
            border-radius: 10px;
        }
        
        .header {
            background: rgba(0, 0, 0, 0.8);
            backdrop-filter: blur(10px);
            box-shadow: 0 2px 20px rgba(0,0,0,0.3);
            padding: 1rem 2rem;
            position: sticky;
            top: 0;
            z-index: 1000;
            border-bottom: 1px solid rgba(102, 126, 234, 0.3);
        }
        
        .header-content {
            max-width: 1400px;
            margin: 0 auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 1rem;
        }
        
        .logo h1 {
            font-size: 1.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .logo p {
            font-size: 0.75rem;
            color: #888;
        }
        
        .status-badge {
            display: flex;
            gap: 1rem;
            align-items: center;
        }
        
        .badge {
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 600;
        }
        
        .badge.online {
            background: #10b981;
            color: white;
        }
        
        .badge.warning {
            background: #f59e0b;
            color: black;
        }
        
        .badge.critical {
            background: #ef4444;
            color: white;
        }
        
        .badge.excellent {
            background: #10b981;
            color: white;
        }
        
        .badge.good {
            background: #3b82f6;
            color: white;
        }
        
        .badge.acceptable {
            background: #f59e0b;
            color: black;
        }
        
        .badge.poor {
            background: #ef4444;
            color: white;
        }
        
        .dashboard-container {
            max-width: 1400px;
            margin: 2rem auto;
            padding: 0 2rem;
        }
        
        .pf-card {
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border-radius: 20px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            border: 1px solid rgba(102, 126, 234, 0.3);
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
            text-align: center;
        }
        
        .pf-gauge {
            position: relative;
            width: 200px;
            height: 200px;
            margin: 0 auto;
        }
        
        .pf-value {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            font-size: 2.5rem;
            font-weight: 800;
            text-align: center;
        }
        
        .pf-label {
            font-size: 0.85rem;
            color: #888;
            margin-top: 0.5rem;
        }
        
        canvas {
            max-width: 100%;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }
        
        .section-title {
            font-size: 1.2rem;
            font-weight: 700;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            color: #ccc;
        }
        
        .section-title i {
            font-size: 1.3rem;
            color: #667eea;
        }
        
        .stat-card {
            background: #1e293b;
            border-radius: 20px;
            padding: 1.5rem;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            transition: transform 0.3s, box-shadow 0.3s;
            border: 1px solid rgba(102, 126, 234, 0.2);
        }
        
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 40px rgba(0,0,0,0.3);
            border-color: rgba(102, 126, 234, 0.5);
        }
        
        .stat-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }
        
        .stat-header i {
            font-size: 2rem;
            color: #667eea;
        }
        
        .stat-title {
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #888;
        }
        
        .stat-value {
            font-size: 2rem;
            font-weight: 800;
            color: #fff;
            margin: 0.5rem 0;
        }
        
        .stat-unit {
            font-size: 0.85rem;
            color: #888;
        }
        
        .relay-indicator {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 8px;
            animation: pulse 1.5s infinite;
        }
        
        .relay-on {
            background: #ef4444;
            box-shadow: 0 0 10px #ef4444;
        }
        
        .relay-off {
            background: #6b7280;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .two-column {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1.5rem;
            margin-bottom: 1.5rem;
        }
        
        .chart-container {
            background: #1e293b;
            border-radius: 20px;
            padding: 1.5rem;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            border: 1px solid rgba(102, 126, 234, 0.2);
        }
        
        .chart-title {
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 1rem;
            color: #ccc;
        }
        
        .capacitor-bank {
            display: flex;
            justify-content: space-around;
            margin-top: 1rem;
        }
        
        .capacitor {
            text-align: center;
            padding: 1rem;
            background: #0f172a;
            border-radius: 15px;
            min-width: 100px;
        }
        
        .capacitor.active {
            border: 2px solid #10b981;
            background: rgba(16, 185, 129, 0.1);
        }
        
        .capacitor .capacitor-value {
            font-size: 1.2rem;
            font-weight: 700;
        }
        
        .capacitor .capacitor-status {
            font-size: 0.8rem;
            margin-top: 0.5rem;
        }
        
        .recommendations-container {
            background: #1e293b;
            border-radius: 20px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
            border: 1px solid rgba(102, 126, 234, 0.2);
        }
        
        .recommendation-card {
            background: #0f172a;
            border-radius: 15px;
            padding: 1rem;
            margin-bottom: 1rem;
            border-left: 4px solid;
        }
        
        .recommendation-card.critical { border-left-color: #dc2626; }
        .recommendation-card.warning { border-left-color: #f59e0b; }
        .recommendation-card.monitor { border-left-color: #3b82f6; }
        .recommendation-card.normal { border-left-color: #10b981; }
        .recommendation-card.performance { border-left-color: #8b5cf6; }
        
        .recommendation-title {
            font-weight: 700;
            margin-bottom: 0.5rem;
        }
        
        .recommendation-actions {
            list-style: none;
            margin-top: 0.5rem;
        }
        
        .recommendation-actions li {
            font-size: 0.85rem;
            color: #aaa;
            padding: 0.25rem 0;
            padding-left: 1.2rem;
            position: relative;
        }
        
        .recommendation-actions li:before {
            content: "→";
            position: absolute;
            left: 0;
            color: #667eea;
        }
        
        .priority-badge {
            display: inline-block;
            padding: 0.2rem 0.6rem;
            border-radius: 10px;
            font-size: 0.7rem;
            font-weight: 600;
            margin-left: 0.5rem;
        }
        
        .priority-high { background: #dc2626; color: white; }
        .priority-medium { background: #f59e0b; color: black; }
        .priority-low { background: #3b82f6; color: white; }
        
        .alerts-container {
            background: #1e293b;
            border-radius: 20px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
            border: 1px solid rgba(102, 126, 234, 0.2);
            max-height: 300px;
            overflow-y: auto;
        }
        
        .alert-item {
            background: #0f172a;
            border-radius: 10px;
            padding: 0.75rem;
            margin-bottom: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        
        .alert-icon {
            width: 32px;
            height: 32px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .alert-icon.critical { background: #dc2626; }
        .alert-icon.warning { background: #f59e0b; }
        .alert-icon.info { background: #3b82f6; }
        
        .alert-content {
            flex: 1;
        }
        
        .alert-message {
            font-size: 0.85rem;
            font-weight: 500;
        }
        
        .alert-time {
            font-size: 0.7rem;
            color: #666;
        }
        
        .data-table-container {
            background: #1e293b;
            border-radius: 20px;
            padding: 1.5rem;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            margin-bottom: 1.5rem;
            overflow-x: auto;
            border: 1px solid rgba(102, 126, 234, 0.2);
        }
        
        .data-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .data-table th,
        .data-table td {
            padding: 0.75rem;
            text-align: left;
            border-bottom: 1px solid #334155;
        }
        
        .data-table th {
            background: #0f172a;
            font-weight: 600;
            color: #ccc;
        }
        
        .data-table tr:hover {
            background: #334155;
        }
        
        @media (max-width: 768px) {
            .two-column {
                grid-template-columns: 1fr;
            }
            
            .dashboard-container {
                padding: 0 1rem;
            }
            
            .stats-grid {
                grid-template-columns: 1fr;
            }
        }
        
        .loading {
            text-align: center;
            padding: 2rem;
        }
        
        .spinner {
            border: 3px solid #334155;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <div class="logo">
                <h1>⚡ POWER FACTOR CORRECTION MONITOR v3.1</h1>
                <p>Real-time PF Monitoring & Automatic Correction | Target PF: 0.95</p>
            </div>
            <div class="status-badge">
                <div class="badge online" id="connectionStatus">
                    <i class="fas fa-circle"></i> Connected
                </div>
                <div class="badge" id="pfQualityBadge">
                    <i class="fas fa-chart-line"></i> Loading...
                </div>
            </div>
        </div>
    </div>
    
    <div class="dashboard-container">
        <!-- PF Gauge Card -->
        <div class="pf-card">
            <div class="pf-gauge">
                <canvas id="pfGauge" width="200" height="200"></canvas>
                <div class="pf-value">
                    <span id="pfValue">0.82</span>
                </div>
            </div>
            <div class="pf-label">Power Factor | Target: <span id="targetPf">0.95</span></div>
            <div style="margin-top: 1rem;" id="pfStatusText">
                <span class="relay-indicator" id="relayIndicator"></span>
                <span id="relayStatusText">Capacitors: OFF</span>
            </div>
        </div>
        
        <!-- Stats Grid -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">Voltage</span>
                    <i class="fas fa-bolt"></i>
                </div>
                <div class="stat-value" id="voltage">230.0</div>
                <div class="stat-unit">Volts (V)</div>
            </div>
            
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">Current</span>
                    <i class="fas fa-waveform"></i>
                </div>
                <div class="stat-value" id="current">0.00</div>
                <div class="stat-unit">Amperes (A)</div>
            </div>
            
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">Real Power</span>
                    <i class="fas fa-plug"></i>
                </div>
                <div class="stat-value" id="realPower">0.0</div>
                <div class="stat-unit">Watts (W)</div>
            </div>
            
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">Reactive Power</span>
                    <i class="fas fa-bolt"></i>
                </div>
                <div class="stat-value" id="reactivePower">0.0</div>
                <div class="stat-unit">Volt-Amperes Reactive (VAR)</div>
            </div>
            
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">Apparent Power</span>
                    <i class="fas fa-chart-line"></i>
                </div>
                <div class="stat-value" id="apparentPower">0.0</div>
                <div class="stat-unit">Volt-Amperes (VA)</div>
            </div>
        </div>
        
        <!-- Energy and Load Stats -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">Energy Consumed</span>
                    <i class="fas fa-charging-station"></i>
                </div>
                <div class="stat-value" id="energy">0.00</div>
                <div class="stat-unit" id="energyUnit">kWh</div>
            </div>
            
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">Load Status</span>
                    <i class="fas fa-weight-hanging"></i>
                </div>
                <div class="stat-value" id="loadStatus">NONE</div>
                <div class="stat-unit">Current Load Level</div>
            </div>
            
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">Total Capacitance</span>
                    <i class="fas fa-microchip"></i>
                </div>
                <div class="stat-value" id="totalCap">0</div>
                <div class="stat-unit">Microfarads (μF)</div>
            </div>
            
            <div class="stat-card">
                <div class="stat-header">
                    <span class="stat-title">PF Quality</span>
                    <i class="fas fa-star"></i>
                </div>
                <div class="stat-value" id="pfQuality">POOR</div>
                <div class="stat-unit">Power Factor Rating</div>
            </div>
        </div>
        
        <!-- Capacitor Bank Status -->
        <div class="chart-container">
            <div class="chart-title">
                <i class="fas fa-microchip"></i> Capacitor Bank Status
            </div>
            <div class="capacitor-bank" id="capacitorBank">
                <div class="capacitor" id="cap1">
                    <div class="capacitor-value">8 μF</div>
                    <div class="capacitor-status">OFF</div>
                </div>
                <div class="capacitor" id="cap2">
                    <div class="capacitor-value">3 μF</div>
                    <div class="capacitor-status">OFF</div>
                </div>
                <div class="capacitor" id="cap3">
                    <div class="capacitor-value">3 μF</div>
                    <div class="capacitor-status">OFF</div>
                </div>
            </div>
        </div>
        
        <!-- Charts -->
        <div class="two-column">
            <div class="chart-container">
                <div class="chart-title">
                    <i class="fas fa-chart-line"></i> Real-time Trends
                </div>
                <canvas id="trendChart"></canvas>
            </div>
            
            <div class="chart-container">
                <div class="chart-title">
                    <i class="fas fa-chart-pie"></i> Power Analysis
                </div>
                <canvas id="powerChart"></canvas>
            </div>
        </div>
        
        <!-- Recommendations Section -->
        <div class="recommendations-container">
            <div class="chart-title">
                <i class="fas fa-lightbulb"></i> SMART RECOMMENDATIONS
            </div>
            <div id="recommendationsList">
                <div class="loading"><div class="spinner"></div> Loading recommendations...</div>
            </div>
        </div>
        
        <!-- Alerts Section -->
        <div class="alerts-container">
            <div class="chart-title">
                <i class="fas fa-bell"></i> LIVE ALERTS
            </div>
            <div id="alertsList">
                <div style="text-align: center; color: #666;">No alerts</div>
            </div>
        </div>
        
        <!-- Data Table -->
        <div class="data-table-container">
            <div class="chart-title">
                <i class="fas fa-table"></i> Live Data Stream
            </div>
            <table class="data-table" id="dataTable">
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Voltage (V)</th>
                        <th>Current (A)</th>
                        <th>Power Factor</th>
                        <th>Real Power (W)</th>
                        <th>Capacitors</th>
                        <th>PF Quality</th>
                    </tr>
                </thead>
                <tbody id="tableBody">
                    <tr>
                        <td colspan="7" class="loading">
                            <div class="spinner"></div>
                            Waiting for data...
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
    
    <script>
        // Socket.IO connection
        const socket = io();
        
        // Chart variables
        let trendChart, powerChart, gaugeCanvas;
        
        // Initialize charts
        function initCharts() {
            // Trend Chart
            const ctx = document.getElementById('trendChart').getContext('2d');
            trendChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'Current (A)',
                        data: [],
                        borderColor: '#667eea',
                        backgroundColor: 'rgba(102, 126, 234, 0.1)',
                        tension: 0.4,
                        fill: true,
                        yAxisID: 'y'
                    }, {
                        label: 'Power Factor',
                        data: [],
                        borderColor: '#f59e0b',
                        backgroundColor: 'rgba(245, 158, 11, 0.1)',
                        tension: 0.4,
                        fill: true,
                        yAxisID: 'y1'
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {
                        legend: { position: 'top', labels: { color: '#ccc' } }
                    },
                    scales: {
                        y: {
                            title: { display: true, text: 'Current (A)', color: '#ccc' },
                            grid: { color: '#334155' },
                            ticks: { color: '#ccc' }
                        },
                        y1: {
                            position: 'right',
                            title: { display: true, text: 'Power Factor', color: '#ccc' },
                            grid: { drawOnChartArea: false },
                            ticks: { color: '#ccc', min: 0, max: 1 }
                        },
                        x: {
                            grid: { color: '#334155' },
                            ticks: { color: '#ccc' }
                        }
                    }
                }
            });
            
            // Power Chart (Pie)
            const powerCtx = document.getElementById('powerChart').getContext('2d');
            powerChart = new Chart(powerCtx, {
                type: 'doughnut',
                data: {
                    labels: ['Real Power (W)', 'Reactive Power (VAR)'],
                    datasets: [{
                        data: [0, 0],
                        backgroundColor: ['#10b981', '#ef4444'],
                        borderWidth: 0
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {
                        legend: { position: 'bottom', labels: { color: '#ccc' } }
                    }
                }
            });
            
            // PF Gauge
            gaugeCanvas = document.getElementById('pfGauge').getContext('2d');
            drawGauge(0.82);
        }
        
        function drawGauge(pf) {
            const ctx = gaugeCanvas;
            const width = 200;
            const height = 200;
            
            ctx.clearRect(0, 0, width, height);
            
            const startAngle = -Math.PI / 2;
            const endAngle = Math.PI / 2;
            
            // Background arc
            ctx.beginPath();
            ctx.arc(width/2, height/2, 80, startAngle, endAngle);
            ctx.strokeStyle = '#334155';
            ctx.lineWidth = 20;
            ctx.stroke();
            
            // Colored arc based on PF
            const angle = startAngle + (pf * Math.PI);
            ctx.beginPath();
            ctx.arc(width/2, height/2, 80, startAngle, angle);
            
            if (pf >= 0.95) ctx.strokeStyle = '#10b981';
            else if (pf >= 0.90) ctx.strokeStyle = '#3b82f6';
            else if (pf >= 0.85) ctx.strokeStyle = '#f59e0b';
            else ctx.strokeStyle = '#ef4444';
            
            ctx.lineWidth = 20;
            ctx.stroke();
            
            // Draw tick marks
            for (let i = 0; i <= 10; i++) {
                const tickPF = i / 10;
                const tickAngle = startAngle + (tickPF * Math.PI);
                const x1 = width/2 + 70 * Math.cos(tickAngle);
                const y1 = height/2 + 70 * Math.sin(tickAngle);
                const x2 = width/2 + 80 * Math.cos(tickAngle);
                const y2 = height/2 + 80 * Math.sin(tickAngle);
                
                ctx.beginPath();
                ctx.moveTo(x1, y1);
                ctx.lineTo(x2, y2);
                ctx.strokeStyle = '#666';
                ctx.lineWidth = 2;
                ctx.stroke();
            }
        }
        
        // Current data storage
        let currentData = {};
        
        // Socket event listeners
        socket.on('connect', function() {
            console.log('Connected to server');
            document.getElementById('connectionStatus').innerHTML = '<i class="fas fa-circle"></i> Connected';
            document.getElementById('connectionStatus').classList.add('online');
        });
        
        socket.on('disconnect', function() {
            console.log('Disconnected from server');
            document.getElementById('connectionStatus').innerHTML = '<i class="fas fa-circle"></i> Disconnected';
            document.getElementById('connectionStatus').classList.remove('online');
            document.getElementById('connectionStatus').classList.add('warning');
        });
        
        socket.on('data_update', function(data) {
            currentData = data;
            updateDashboard(data);
        });
        
        socket.on('recommendations_update', function(data) {
            updateRecommendations(data.recommendations);
        });
        
        socket.on('new_alert', function(alert) {
            addAlert(alert);
        });
        
        function updateDashboard(data) {
            // Update basic values
            document.getElementById('voltage').innerHTML = (data.voltage || 230).toFixed(1);
            document.getElementById('current').innerHTML = (data.current || 0).toFixed(2);
            document.getElementById('realPower').innerHTML = (data.real_power || 0).toFixed(1);
            document.getElementById('apparentPower').innerHTML = (data.apparent_power || 0).toFixed(1);
            document.getElementById('reactivePower').innerHTML = (data.reactive_power || 0).toFixed(1);
            document.getElementById('targetPf').innerHTML = (data.target_pf || 0.95).toFixed(3);
            document.getElementById('totalCap').innerHTML = (data.total_capacitance || 0).toFixed(0);
            
            // Update PF
            const pf = data.power_factor || 0.82;
            document.getElementById('pfValue').innerHTML = pf.toFixed(3);
            drawGauge(pf);
            
            // Update Energy
            const energy = data.energy_kwh || data.energy_wh / 1000 || 0;
            if (energy < 100) {
                document.getElementById('energy').innerHTML = energy.toFixed(2);
                document.getElementById('energyUnit').innerHTML = 'kWh';
            } else {
                document.getElementById('energy').innerHTML = (energy / 1000).toFixed(2);
                document.getElementById('energyUnit').innerHTML = 'MWh';
            }
            
            // Update Load Status
            document.getElementById('loadStatus').innerHTML = data.load_status || 'NONE';
            
            // Update PF Quality
            const pfQuality = data.pf_quality || 'POOR';
            document.getElementById('pfQuality').innerHTML = pfQuality;
            
            // Update PF Quality Badge
            const pfBadge = document.getElementById('pfQualityBadge');
            pfBadge.className = 'badge';
            if (pf >= 0.95) {
                pfBadge.classList.add('excellent');
                pfBadge.innerHTML = '<i class="fas fa-trophy"></i> EXCELLENT PF';
            } else if (pf >= 0.90) {
                pfBadge.classList.add('good');
                pfBadge.innerHTML = '<i class="fas fa-check"></i> GOOD PF';
            } else if (pf >= 0.85) {
                pfBadge.classList.add('acceptable');
                pfBadge.innerHTML = '<i class="fas fa-exclamation-triangle"></i> ACCEPTABLE PF';
            } else {
                pfBadge.classList.add('poor');
                pfBadge.innerHTML = '<i class="fas fa-exclamation-circle"></i> POOR PF';
            }
            
            // Update Capacitor Bank Display
            const cap1Active = data.cap1_state || false;
            const cap2Active = data.cap2_state || false;
            const cap3Active = data.cap3_state || false;
            
            const cap1Div = document.getElementById('cap1');
            const cap2Div = document.getElementById('cap2');
            const cap3Div = document.getElementById('cap3');
            
            if (cap1Active) {
                cap1Div.classList.add('active');
                cap1Div.querySelector('.capacitor-status').innerHTML = '✅ ON';
            } else {
                cap1Div.classList.remove('active');
                cap1Div.querySelector('.capacitor-status').innerHTML = '⚫ OFF';
            }
            
            if (cap2Active) {
                cap2Div.classList.add('active');
                cap2Div.querySelector('.capacitor-status').innerHTML = '✅ ON';
            } else {
                cap2Div.classList.remove('active');
                cap2Div.querySelector('.capacitor-status').innerHTML = '⚫ OFF';
            }
            
            if (cap3Active) {
                cap3Div.classList.add('active');
                cap3Div.querySelector('.capacitor-status').innerHTML = '✅ ON';
            } else {
                cap3Div.classList.remove('active');
                cap3Div.querySelector('.capacitor-status').innerHTML = '⚫ OFF';
            }
            
            // Update Relay Status
            const relayIndicator = document.getElementById('relayIndicator');
            const relayText = document.getElementById('relayStatusText');
            const anyActive = cap1Active || cap2Active || cap3Active;
            
            if (anyActive) {
                relayIndicator.className = 'relay-indicator relay-on';
                let capList = [];
                if (cap1Active) capList.push('8μF');
                if (cap2Active) capList.push('3μF');
                if (cap3Active) capList.push('3μF');
                relayText.innerHTML = `🔴 Capacitors ON: ${capList.join(' + ')} (${data.total_capacitance || 0}μF total)`;
            } else {
                relayIndicator.className = 'relay-indicator relay-off';
                relayText.innerHTML = '⚫ Capacitors OFF - No correction';
            }
            
            // Update PF Status Text color
            const pfStatusText = document.getElementById('pfStatusText');
            if (pf >= 0.95) pfStatusText.style.color = '#10b981';
            else if (pf >= 0.90) pfStatusText.style.color = '#3b82f6';
            else if (pf >= 0.85) pfStatusText.style.color = '#f59e0b';
            else pfStatusText.style.color = '#ef4444';
            
            // Update Power Chart
            const reactivePower = Math.max(0, (data.apparent_power || 0) - (data.real_power || 0));
            powerChart.data.datasets[0].data = [data.real_power || 0, reactivePower];
            powerChart.update();
            
            // Update Trend Chart
            updateTrendChart(data);
            
            // Update Table
            updateTable(data);
        }
        
        function updateTrendChart(data) {
            const now = new Date().toLocaleTimeString();
            
            if (trendChart.data.labels.length > 20) {
                trendChart.data.labels.shift();
                trendChart.data.datasets[0].data.shift();
                trendChart.data.datasets[1].data.shift();
            }
            
            trendChart.data.labels.push(now);
            trendChart.data.datasets[0].data.push(data.current || 0);
            trendChart.data.datasets[1].data.push(data.power_factor || 0.82);
            trendChart.update();
        }
        
        function updateTable(data) {
            const tableBody = document.getElementById('tableBody');
            const newRow = document.createElement('tr');
            
            const now = new Date().toLocaleTimeString();
            const pf = data.power_factor || 0.82;
            let pfColor = '#10b981';
            if (pf < 0.85) pfColor = '#ef4444';
            else if (pf < 0.90) pfColor = '#f59e0b';
            else if (pf < 0.95) pfColor = '#3b82f6';
            
            const capStatus = [];
            if (data.cap1_state) capStatus.push('8');
            if (data.cap2_state) capStatus.push('3');
            if (data.cap3_state) capStatus.push('3');
            const capText = capStatus.length > 0 ? capStatus.join('+') + 'μF' : 'OFF';
            
            newRow.innerHTML = `
                <td>${now}</td>
                <td>${(data.voltage || 230).toFixed(1)}</td>
                <td>${(data.current || 0).toFixed(2)}</td>
                <td style="color: ${pfColor}; font-weight: 600;">${pf.toFixed(3)}</td>
                <td>${(data.real_power || 0).toFixed(1)}</td>
                <td>${capText}</td>
                <td style="color: ${pfColor};">${data.pf_quality || 'POOR'}</td>
            `;
            
            tableBody.insertBefore(newRow, tableBody.firstChild);
            while (tableBody.children.length > 10) {
                tableBody.removeChild(tableBody.lastChild);
            }
        }
        
        function updateRecommendations(recommendations) {
            const container = document.getElementById('recommendationsList');
            if (!recommendations || recommendations.length === 0) {
                container.innerHTML = '<div style="text-align: center; color: #666;">No recommendations at this time</div>';
                return;
            }
            
            let html = '';
            for (let rec of recommendations) {
                const type = rec.type || 'normal';
                const priority = rec.priority || 'NORMAL';
                let priorityClass = '';
                if (priority === 'HIGH') priorityClass = 'priority-high';
                else if (priority === 'MEDIUM') priorityClass = 'priority-medium';
                else if (priority === 'LOW') priorityClass = 'priority-low';
                
                html += `
                    <div class="recommendation-card ${type}">
                        <div class="recommendation-title">
                            ${rec.message}
                            <span class="priority-badge ${priorityClass}">${priority}</span>
                        </div>
                        <ul class="recommendation-actions">
                            ${rec.actions.map(action => `<li>${action}</li>`).join('')}
                        </ul>
                    </div>
                `;
            }
            container.innerHTML = html;
        }
        
        function addAlert(alert) {
            const container = document.getElementById('alertsList');
            const alertDiv = document.createElement('div');
            alertDiv.className = 'alert-item';
            
            let iconClass = 'info';
            if (alert.type === 'critical') iconClass = 'critical';
            else if (alert.type === 'warning') iconClass = 'warning';
            
            alertDiv.innerHTML = `
                <div class="alert-icon ${iconClass}">
                    <i class="fas fa-${alert.type === 'critical' ? 'exclamation-triangle' : alert.type === 'warning' ? 'exclamation' : 'bell'}" style="color: white;"></i>
                </div>
                <div class="alert-content">
                    <div class="alert-message">${alert.message}</div>
                    <div class="alert-time">${alert.timestamp}</div>
                </div>
            `;
            
            container.insertBefore(alertDiv, container.firstChild);
            while (container.children.length > 10) {
                container.removeChild(container.lastChild);
            }
        }
        
        // Fetch initial data
        fetch('/api/current-data')
            .then(res => res.json())
            .then(data => {
                if (data.success && data.data) {
                    updateDashboard(data.data);
                }
            });
        
        fetch('/api/recommendations')
            .then(res => res.json())
            .then(data => {
                if (data.success && data.recommendations) {
                    updateRecommendations(data.recommendations);
                }
            });
        
        fetch('/api/alerts')
            .then(res => res.json())
            .then(data => {
                if (data.success && data.alerts) {
                    data.alerts.forEach(alert => addAlert(alert));
                }
            });
        
        // Initialize charts when page loads
        initCharts();
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/data', methods=['POST'])
def receive_data():
    d = request.get_json()
    if d:
        monitor.update_from_dict(d)
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'No data'}), 400

@app.route('/api/current-data')
def get_current_data():
    return jsonify({
        'success': True,
        'data': monitor.current_data
    })

@app.route('/api/history')
def get_history():
    limit = request.args.get('limit', 50, type=int)
    history_list = list(monitor.data_history)[-limit:]
    return jsonify({
        'success': True,
        'data': history_list
    })

@app.route('/api/alerts')
def get_alerts():
    return jsonify({
        'success': True,
        'alerts': list(monitor.alerts)
    })

@app.route('/api/recommendations')
def get_recommendations():
    return jsonify({
        'success': True,
        'recommendations': list(monitor.recommendations)
    })

# ============================================
# DATA FETCHER THREAD
# ============================================
def watchdog():
    """Start simulation if no data received from ESP32 for 30s"""
    time.sleep(10)
    while monitor.running:
        if time.time() - monitor.last_data_time > 30:
            print("\n⚠️ No data from ESP32 for 30s - starting simulation mode")
            simulate_data()
            break
        time.sleep(5)

def simulate_data():
    """Simulate data for testing without Arduino"""
    import random
    import math
    
    print("\n🎮 SIMULATION MODE ACTIVE")
    print("   Showing demo data for Power Factor Correction")
    print("   Configure DATA_URL for real data\n")
    
    angle = 0
    cap1 = cap2 = cap3 = False
    energy = 0
    
    while monitor.running:
        angle += 0.3
        
        # Simulate varying load
        current = 8 + 15 * abs(math.sin(angle))
        current += random.uniform(-0.5, 0.5)
        current = max(2, min(40, current))
        
        # Simulate PF based on capacitors
        base_pf = 0.78
        total_cap = 0
        if cap1: total_cap += 8
        if cap2: total_cap += 3
        if cap3: total_cap += 3
        
        power_factor = base_pf + (total_cap * 0.025)
        power_factor = min(0.99, max(0.72, power_factor))
        
        # Auto-toggle capacitors based on PF
        if power_factor < 0.82 and not cap1:
            cap1 = True
            print("🔄 SIM: Added 8μF capacitor")
        elif power_factor < 0.86 and cap1 and not cap2:
            cap2 = True
            print("🔄 SIM: Added 3μF capacitor")
        elif power_factor < 0.90 and cap2 and not cap3:
            cap3 = True
            print("🔄 SIM: Added 3μF capacitor")
        elif power_factor > 0.97 and cap3:
            cap3 = False
            print("🔄 SIM: Removed 3μF capacitor")
        elif power_factor > 0.96 and cap2:
            cap2 = False
            print("🔄 SIM: Removed 3μF capacitor")
        elif power_factor > 0.95 and cap1:
            cap1 = False
            print("🔄 SIM: Removed 8μF capacitor")
        
        voltage = 230 + random.uniform(-3, 3)
        real_power = voltage * current * power_factor
        apparent_power = voltage * current
        
        # Energy calculation
        energy += (real_power / 3600)
        if energy > 100000:
            energy = 0
        
        total_cap = (8 if cap1 else 0) + (3 if cap2 else 0) + (3 if cap3 else 0)
        
        reactive_power = math.sqrt(max(0, apparent_power**2 - real_power**2))

        # Update data
        monitor.current_data = {
            'voltage': round(voltage, 1),
            'current': round(current, 2),
            'power_factor': round(power_factor, 3),
            'target_pf': 0.95,
            'real_power': round(real_power, 1),
            'reactive_power': round(reactive_power, 1),
            'apparent_power': round(apparent_power, 1),
            'energy_wh': round(energy, 1),
            'energy_kwh': round(energy / 1000, 3),
            'cap1_state': cap1,
            'cap2_state': cap2,
            'cap3_state': cap3,
            'total_capacitance': total_cap,
            'relay_state': cap1 or cap2 or cap3,
            'pf_correction_active': cap1 or cap2 or cap3,
            'load_status': 'HEAVY' if current > 25 else 'LIGHT' if current > 5 else 'NONE',
            'pf_quality': 'EXCELLENT' if power_factor >= 0.95 else 'GOOD' if power_factor >= 0.90 else 'ACCEPTABLE' if power_factor >= 0.85 else 'POOR',
            'health_status': 'Excellent' if power_factor >= 0.95 else 'Good' if power_factor >= 0.90 else 'Monitor' if power_factor >= 0.85 else 'Warning',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Add to history
        monitor.data_history.append(monitor.current_data.copy())
        
        # Generate recommendations and alerts
        monitor.generate_recommendations()
        monitor.check_alerts()
        
        # Emit to WebSocket
        socketio.emit('data_update', monitor.current_data)
        
        cap_status = []
        if cap1: cap_status.append('8')
        if cap2: cap_status.append('3')
        if cap3: cap_status.append('3')
        caps = '+'.join(cap_status) if cap_status else 'OFF'
        
        print(f"📊 [SIM] PF={power_factor:.3f}/0.95 | I={current:.1f}A | "
              f"P={real_power:.0f}W | Caps={caps}({total_cap}μF) | {monitor.current_data['pf_quality']}")
        
        time.sleep(1)

# ============================================
# MAIN EXECUTION
# ============================================
if __name__ == '__main__':
    print("="*60)
    print("   POWER FACTOR CORRECTION MONITORING SYSTEM v3.1")
    print("="*60)
    print("Developer: SIMON")
    print("------------------------------------------")
    print("🌐 Web Dashboard: http://localhost:5000")
    print("📡 ESP32 POST endpoint: /api/data")
    print("🔄 Real-time updates via WebSocket")
    print("💡 PF Correction Target: 0.95")
    print("📊 Capacitor Bank: 8μF, 3μF, 3μF")
    print("="*60)
    print()
    
    # Start watchdog thread (falls back to simulation if no ESP32 data)
    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    watchdog_thread.start()
    
    port = int(os.environ.get('PORT', 5000))
    print("✅ System started!")
    print(f"📊 Web Dashboard: http://0.0.0.0:{port}")
    print(f"🔌 ESP32 should POST data to: http://0.0.0.0:{port}/api/data")
    print("\nPress Ctrl+C to stop\n")
    
    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)