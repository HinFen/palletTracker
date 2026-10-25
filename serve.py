"""Runs the tracker with a proper server (waitress) instead of Flask's test server.
Start it with:  python serve.py      (or double-click start-tracker.bat on Windows)"""
import os

from tracker import create_app
from tracker.network import computer_name, local_addresses

app = create_app()

if __name__ == "__main__":
    try:
        from waitress import serve
    except ImportError:
        raise SystemExit("The waitress package is missing. Run:  pip install -r requirements.txt")
    host = os.environ.get("TRACKER_HOST", "0.0.0.0")  # 0.0.0.0 lets phones on the wifi connect
    port = int(os.environ.get("TRACKER_PORT", "5000"))
    print(f"Tracker is running. Open http://localhost:{port} on this computer.")
    addresses = local_addresses()
    if addresses:
        print("Phones and other computers on the same network can use:")
        for ip in addresses:
            print(f"    http://{ip}:{port}")
    print(f"Or try the computer's name: http://{computer_name()}:{port}")
    print("If the address stops working later, it may have changed. This window and the Connect page in the app show the current one.")
    print("Leave this window open. Press Ctrl+C to stop.")
    serve(app, host=host, port=port, threads=8)
