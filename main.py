import threading
import os
from dashboard import app
from test import bot
from config import BOT_TOKEN, DASHBOARD_PORT

def run_flask():
    """Runs the Flask web dashboard on the port designated by Render or config."""
    # Render sets the PORT environment variable automatically
    port = int(os.getenv("PORT", DASHBOARD_PORT))
    
    print(f"[Web] Starting LSPD Web Dashboard on port {port}...")
    # debug=False is critical to prevent Flask's reloader from starting the bot twice or in separate threads.
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    # 1. Start the Flask dashboard in a background daemon thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # 2. Start the Discord bot in the main thread (blocks execution)
    print("[Bot] Starting LSPD Discord Bot...")
    bot.run(BOT_TOKEN)
