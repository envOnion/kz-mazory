import os
import sys
import threading
import time
import requests
from django.apps import AppConfig
from django.conf import settings

def start_waha_watchdog():
    def watchdog_loop():
        waha_url = getattr(settings, 'WAHA_API_URL', 'http://waha:3000')
        api_key = getattr(settings, 'WAHA_API_KEY', 'mazory-waha-key-2026')
        headers = {'X-Api-Key': api_key}
        time.sleep(3)
        while True:
            try:
                res = requests.get(f"{waha_url}/api/sessions/default", headers=headers, timeout=5)
                if res.status_code == 200:
                    default_session = res.json()
                    status = default_session.get('status')
                    if status in ('FAILED', 'STOPPED'):
                        print(f"\n🔄 [WAHA AUTO-HEALER] Session 'default' is {status}. Auto-restarting so QR is always active...", flush=True)
                        requests.post(f"{waha_url}/api/sessions/default/restart", headers=headers, timeout=5)
                elif res.status_code == 404:
                    print(f"\n🚀 [WAHA AUTO-HEALER] Session 'default' not found. Creating...", flush=True)
                    requests.post(f"{waha_url}/api/sessions", json={"name": "default"}, headers=headers, timeout=5)
            except Exception as e:
                pass
            time.sleep(5)

    t = threading.Thread(target=watchdog_loop, daemon=True, name="WahaWatchdog")
    t.start()

class ApiConfig(AppConfig):
    name = 'api'

    def ready(self):
        print(f"DEBUG: ApiConfig.ready() called, RUN_MAIN={os.environ.get('RUN_MAIN')}, argv={sys.argv}", flush=True)
        # Run only once in main process of runserver or qcluster
        if os.environ.get('RUN_MAIN') == 'true' or 'qcluster' in sys.argv or 'runserver' in sys.argv:
            start_waha_watchdog()

