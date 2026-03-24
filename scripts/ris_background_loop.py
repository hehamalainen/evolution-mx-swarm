import time
import subprocess
from datetime import datetime

# Cycle sleep time: 5 minutes (to avoid rate limits)
CYCLE_DELAY = 300 

def start_surveillance_loop():
    print(f"[{datetime.now()}] 🛡️ RIS Passive Surveillance Loop Started.")
    print(f"[{datetime.now()}] 🛡️ Cycle Interval: {CYCLE_DELAY / 60} minutes.")
    
    while True:
        try:
            print(f"\n[{datetime.now()}] 🌊 PASSIVE COLLECTION CYCLE TRIGGERED")
            
            # Run Collection
            subprocess.run(["python3", "scripts/ris_collector.py"], check=True)
            
            # Run Analysis
            print(f"[{datetime.now()}] 🧠 TRIGGERING ANALYSIS UPDATE")
            subprocess.run(["python3", "scripts/ris_analyzer.py"], check=True)
            
            print(f"[{datetime.now()}] 🛡️ Cycle Complete. Sleeping for {CYCLE_DELAY / 60} minutes...\n")
            time.sleep(CYCLE_DELAY)
            
        except subprocess.CalledProcessError as e:
            print(f"[{datetime.now()}] ❌ Error during execution: {e}")
            time.sleep(60) # Short wait before retry
        except KeyboardInterrupt:
            print(f"[{datetime.now()}] 🛑 Surveillance Loop Terminated.")
            break

if __name__ == "__main__":
    start_surveillance_loop()
