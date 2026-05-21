import sqlite3
import time
import os
from functools import wraps

from flask import Flask, render_template, redirect, url_for, session, request, flash
import requests as http_requests

from config import (
    DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI,
    DASHBOARD_SECRET_KEY, DASHBOARD_PORT, RANK_ORDER, RANK_NAMES, HC_RANKS
)

# ============================================================
#  FLASK APP SETUP
# ============================================================
app = Flask(__name__)
app.secret_key = DASHBOARD_SECRET_KEY

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_AUTH_URL = (
    f"https://discord.com/api/oauth2/authorize"
    f"?client_id={DISCORD_CLIENT_ID}"
    f"&redirect_uri={DISCORD_REDIRECT_URI}"
    f"&response_type=code"
    f"&scope=identify"
)

# ============================================================
#  DATABASE HELPER
# ============================================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lspd_automated.db')

def get_db():
    """Get a new database connection with Row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============================================================
#  AUTH DECORATOR
# ============================================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

# ============================================================
#  LOGIN / AUTH ROUTES
# ============================================================
@app.route('/health')
def health():
    """Simple lightweight health check endpoint for Render and UptimeRobot."""
    return {"status": "ok"}, 200

@app.route('/')
def index():
    """Login page — or redirect to dashboard if already logged in."""
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/verify-key', methods=['POST'])
def verify_key():
    """Verify an access key and redirect to Discord OAuth2."""
    key = request.form.get('access_key', '').strip()
    if not key:
        flash('Please enter an access key.', 'error')
        return redirect(url_for('index'))

    db = get_db()
    row = db.execute("SELECT * FROM access_keys WHERE key = ? AND used = 0", (key,)).fetchone()
    db.close()

    if not row:
        flash('Invalid or already used access key.', 'error')
        return redirect(url_for('index'))

    # Store key info in session for verification after OAuth
    session['pending_key'] = key
    session['pending_user_id'] = row['user_id']
    return redirect(DISCORD_AUTH_URL)

@app.route('/callback')
def callback():
    """Handle Discord OAuth2 callback."""
    code = request.args.get('code')
    if not code:
        flash('Discord authentication failed.', 'error')
        return redirect(url_for('index'))

    # Exchange authorization code for access token
    token_data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI,
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    token_resp = http_requests.post(f"{DISCORD_API_BASE}/oauth2/token", data=token_data, headers=headers)

    if token_resp.status_code != 200:
        flash('Failed to authenticate with Discord.', 'error')
        return redirect(url_for('index'))

    access_token = token_resp.json().get('access_token')

    # Get user info from Discord
    user_resp = http_requests.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={'Authorization': f'Bearer {access_token}'}
    )

    if user_resp.status_code != 200:
        flash('Failed to get Discord user info.', 'error')
        return redirect(url_for('index'))

    user_data = user_resp.json()

    # Verify the Discord user matches the access key owner
    pending_key = session.pop('pending_key', None)
    pending_user_id = session.pop('pending_user_id', None)

    if not pending_key:
        flash('No pending access key. Please start over.', 'error')
        return redirect(url_for('index'))

    if str(user_data['id']) != str(pending_user_id):
        flash('This Discord account does not match the access key owner.', 'error')
        return redirect(url_for('index'))

    # Mark the access key as used
    db = get_db()
    db.execute("UPDATE access_keys SET used = 1 WHERE key = ?", (pending_key,))
    db.commit()
    db.close()

    # Build avatar URL
    avatar_hash = user_data.get('avatar')
    if avatar_hash:
        avatar_url = f"https://cdn.discordapp.com/avatars/{user_data['id']}/{avatar_hash}.png?size=64"
    else:
        avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"

    # Set session
    session['user'] = {
        'id': user_data['id'],
        'username': user_data.get('global_name') or user_data['username'],
        'avatar': avatar_url
    }

    flash(f'Welcome, {session["user"]["username"]}!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    """Clear session and redirect to login."""
    session.clear()
    return redirect(url_for('index'))

# ============================================================
#  DASHBOARD — Overview
# ============================================================
@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    total_officers = db.execute("SELECT COUNT(*) FROM roster").fetchone()[0]
    on_duty = db.execute("SELECT COUNT(*) FROM on_duty").fetchone()[0]
    total_warnings = db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
    total_security = db.execute("SELECT COUNT(*) FROM security_logs").fetchone()[0]
    recent_events = db.execute(
        "SELECT event_type, user_id, user_name, details, timestamp FROM security_logs ORDER BY timestamp DESC LIMIT 10"
    ).fetchall()
    db.close()

    return render_template('home.html',
        total_officers=total_officers,
        on_duty=on_duty,
        total_warnings=total_warnings,
        total_security=total_security,
        recent_events=recent_events
    )

# ============================================================
#  ROSTER
# ============================================================
@app.route('/roster')
@login_required
def roster():
    db = get_db()
    rows = db.execute("SELECT user_id, rank, unit_number, badge FROM roster ORDER BY rank, unit_number").fetchall()
    db.close()

    grouped = {}
    for row in rows:
        rank = row['rank']
        if rank not in grouped:
            grouped[rank] = []
        grouped[rank].append(row)

    return render_template('roster.html',
        grouped=grouped,
        rank_order=list(reversed(RANK_ORDER)),
        rank_names=RANK_NAMES
    )

# ============================================================
#  ACTIVITY STATS
# ============================================================
@app.route('/stats')
@login_required
def stats():
    db = get_db()
    officers = db.execute("SELECT user_id, rank, unit_number, badge FROM roster").fetchall()

    stats_list = []
    for officer in officers:
        uid = officer['user_id']
        rank = officer['rank']
        days = 14 if rank in HC_RANKS else 7
        cutoff = time.time() - (days * 86400)

        stats_data = {"hours": 0.0, "arrests": 0, "robberies": 0, "tickets": 0, "fto": 0}
        for log_type in stats_data.keys():
            res = db.execute(
                "SELECT SUM(amount) FROM activity_logs WHERE user_id = ? AND log_type = ? AND timestamp >= ?",
                (uid, log_type, cutoff)
            ).fetchone()[0]
            if res:
                stats_data[log_type] = res

        stats_list.append({
            'user_id': uid,
            'rank': rank,
            'unit': officer['unit_number'],
            'badge': officer['badge'],
            'callsign': f"[{rank} {officer['unit_number']:02d}]",
            'days': days,
            **stats_data
        })

    db.close()
    return render_template('stats.html', stats=stats_list, rank_names=RANK_NAMES)

# ============================================================
#  WARNINGS
# ============================================================
@app.route('/warnings')
@login_required
def warnings():
    db = get_db()
    rows = db.execute(
        "SELECT id, user_id, mod_id, reason, timestamp FROM warnings ORDER BY timestamp DESC"
    ).fetchall()
    db.close()
    return render_template('warnings.html', warnings=rows)

@app.route('/warnings/clear/<int:user_id>', methods=['POST'])
@login_required
def clear_warnings(user_id):
    db = get_db()
    db.execute("DELETE FROM warnings WHERE user_id = ?", (user_id,))
    db.commit()
    db.close()
    flash(f'All warnings cleared for user {user_id}.', 'success')
    return redirect(url_for('warnings'))

# ============================================================
#  LOA REQUESTS
# ============================================================
@app.route('/loa')
@login_required
def loa():
    db = get_db()
    rows = db.execute("SELECT user_id, start_date, end_date, reason, approved FROM loa_requests").fetchall()
    db.close()
    return render_template('loa.html', loa_requests=rows)

@app.route('/loa/approve/<int:user_id>', methods=['POST'])
@login_required
def approve_loa(user_id):
    db = get_db()
    db.execute("UPDATE loa_requests SET approved = 1 WHERE user_id = ?", (user_id,))
    db.commit()
    db.close()
    flash(f'LOA approved for user {user_id}.', 'success')
    return redirect(url_for('loa'))

@app.route('/loa/deny/<int:user_id>', methods=['POST'])
@login_required
def deny_loa(user_id):
    db = get_db()
    db.execute("DELETE FROM loa_requests WHERE user_id = ?", (user_id,))
    db.commit()
    db.close()
    flash(f'LOA denied and removed for user {user_id}.', 'success')
    return redirect(url_for('loa'))

# ============================================================
#  SECURITY LOGS
# ============================================================
@app.route('/security')
@login_required
def security():
    db = get_db()
    rows = db.execute(
        "SELECT event_type, user_id, user_name, details, timestamp FROM security_logs ORDER BY timestamp DESC LIMIT 200"
    ).fetchall()
    db.close()
    return render_template('security.html', logs=rows)

# ============================================================
#  BLACKLIST
# ============================================================
@app.route('/blacklist')
@login_required
def blacklist():
    db = get_db()
    rows = db.execute("SELECT user_id FROM blacklist").fetchall()
    db.close()
    return render_template('blacklist.html', blacklist=rows)

@app.route('/blacklist/add', methods=['POST'])
@login_required
def add_blacklist():
    user_id = request.form.get('user_id', '').strip()
    try:
        uid = int(user_id)
    except ValueError:
        flash('Invalid user ID format.', 'error')
        return redirect(url_for('blacklist'))

    db = get_db()
    db.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (uid,))
    db.commit()
    db.close()
    flash(f'User {uid} added to blacklist.', 'success')
    return redirect(url_for('blacklist'))

@app.route('/blacklist/remove/<int:user_id>', methods=['POST'])
@login_required
def remove_blacklist(user_id):
    db = get_db()
    db.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
    db.commit()
    db.close()
    flash(f'User {user_id} removed from blacklist.', 'success')
    return redirect(url_for('blacklist'))

# ============================================================
#  ON DUTY
# ============================================================
@app.route('/onduty')
@login_required
def onduty():
    db = get_db()
    rows = db.execute("SELECT user_id, clock_in_time FROM on_duty").fetchall()
    db.close()
    return render_template('onduty.html', on_duty=rows)

# ============================================================
#  START THE DASHBOARD
# ============================================================
if __name__ == '__main__':
    print(f"[Web] LSPD Dashboard running at http://localhost:{DASHBOARD_PORT}")
    app.run(host='0.0.0.0', port=DASHBOARD_PORT, debug=True)
