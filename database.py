import sqlite3
import time

# ============================================================
#  DATABASE CONNECTION
# ============================================================
conn = sqlite3.connect('lspd_automated.db')
c = conn.cursor()

# ============================================================
#  TABLE CREATION
# ============================================================
c.execute('''CREATE TABLE IF NOT EXISTS activity_logs 
             (user_id INTEGER, log_type TEXT, amount REAL, timestamp REAL)''')
c.execute('''CREATE TABLE IF NOT EXISTS on_duty 
             (user_id INTEGER PRIMARY KEY, clock_in_time REAL)''')
c.execute('''CREATE TABLE IF NOT EXISTS roster 
             (user_id INTEGER PRIMARY KEY, rank TEXT, unit_number INTEGER, badge INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS blacklist 
             (user_id INTEGER PRIMARY KEY)''')
c.execute('''CREATE TABLE IF NOT EXISTS warnings 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, mod_id INTEGER, reason TEXT, timestamp REAL)''')
c.execute('''CREATE TABLE IF NOT EXISTS loa_requests 
             (user_id INTEGER PRIMARY KEY, start_date TEXT, end_date TEXT, reason TEXT, approved INTEGER DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS access_keys 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, key TEXT UNIQUE, created_at REAL, used INTEGER DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS security_logs 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, user_id INTEGER, user_name TEXT, details TEXT, timestamp REAL)''')
conn.commit()

# ============================================================
#  HELPER FUNCTIONS
# ============================================================
def get_user_stats(user_id, days):
    """Get a user's activity stats for the given number of days."""
    cutoff = time.time() - (days * 86400)
    stats = {"hours": 0.0, "robberies": 0, "arrests": 0, "fto": 0, "tickets": 0}
    for log_type in stats.keys():
        c.execute("SELECT SUM(amount) FROM activity_logs WHERE user_id = ? AND log_type = ? AND timestamp >= ?", 
                  (user_id, log_type, cutoff))
        res = c.fetchone()[0]
        if res:
            stats[log_type] = res
    return stats

def log_security_event(event_type, user_id, user_name, details):
    """Log a security event to the database."""
    c.execute("INSERT INTO security_logs (event_type, user_id, user_name, details, timestamp) VALUES (?, ?, ?, ?, ?)",
              (event_type, user_id, user_name, details, time.time()))
    conn.commit()
