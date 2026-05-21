import os
import re
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================
#  API KEYS
# ============================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ============================================================
#  DASHBOARD / OAUTH2
# ============================================================
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
DASHBOARD_SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY", "lspd-dashboard-secret-key-change-me")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", 5000))

# ============================================================
#  CHANNEL IDS — Replace with your actual Discord channel IDs
# ============================================================
ROBBERY_LOG_CHANNEL_ID = 1506315881997664459
TICKET_LOG_CHANNEL_ID = 1506316016982954044
ARREST_LOG_CHANNEL_ID = 1506315918592966826
FTO_LOG_CHANNEL_ID = 1506315944031424604
BLACKLIST_ROLE_ID = 1506255462344036574
ON_DUTY_ROLE_ID = 1506726555622314034

# ============================================================
#  RANK SYSTEM
# ============================================================
RANK_ORDER = ["CDT", "OFC", "DET", "CRP", "SGT", "LT", "CPT", "MJR", "CMD", "DC", "AC", "COP", "HOP"]
RANK_NAMES = {
    "CDT": "Cadet", "OFC": "Officer", "DET": "Detective", "CRP": "Corporal",
    "SGT": "Sergeant", "LT": "Lieutenant", "CPT": "Captain", "MJR": "Major",
    "CMD": "Commander", "DC": "Deputy Chief", "AC": "Assistant Chief",
    "COP": "Chief of Police", "HOP": "Head of Police"
}
HC_RANKS = ["SGT", "LT", "CPT", "MJR", "CMD", "DC", "AC", "COP", "HOP"]

# ============================================================
#  ANTI-SPAM SETTINGS
# ============================================================
SPAM_THRESHOLD = 3       # number of messages within the window to trigger timeout
SPAM_WINDOW = 5          # time window in seconds
SPAM_TIMEOUT_MINS = 2    # timeout duration in minutes

# ============================================================
#  ANTI-SCAM — Domains & Phrases
# ============================================================
SCAM_DOMAINS = [
    # Fake Discord / Nitro
    "discord-nitro.com", "discordgift.com", "discord-gift.com", "discordapp.gift",
    "dlscord.com", "dlscord.gift", "discorcl.com", "disc0rd.com", "disc0rd.gift",
    "dicsord.com", "d1scord.com", "discordnitro.gift", "discordapp.co",
    "discordgiveaway.com", "discord-airdrop.com", "nitro-discord.com",
    "free-nitro.com", "nitro-gift.com", "gift-nitro.com", "freenitro.com",
    "discord-app.net", "discord-give.com", "discordi.gift", "discord.gift",
    "dlscord-nitro.com", "discordnitro.info", "discordgifts.info",
    # Fake Steam
    "steamcommunity.ru", "steamcommunlty.com", "steancommunity.com",
    "steamcommunitv.com", "store-steampowered.com", "steamcommunity.link",
    "steampowered.link", "steamcommunity.co", "steamcommunity.org",
    "steamcommynity.com", "stearncommunitv.com", "stearncomrnunity.com",
    # Fake CS/gaming skins
    "csgo-skins.com", "csgoskins.gift", "tradeoffer.link", "skinsgift.com",
    # Fake Roblox / Epic
    "epicgames-free.com", "roblox-free.com", "free-robux.com", "robux-free.com",
    # Fake login / phishing
    "login-microsoftonline.com", "microsoft-login.com",
    "paypal-login.com", "paypal-verify.com",
    "account-verify.com", "verify-account.com",
    # Crypto / airdrop scams
    "crypto-airdrop.com", "free-ethereum.com", "free-bitcoin.gift",
]

SCAM_PATTERNS = [
    r"free\s*nitro",
    r"claim\s*your\s*(nitro|gift|reward)",
    r"discord\s*nitro\s*for\s*free",
    r"steam\s*gift\s*card\s*free",
    r"free\s*gift\s*card",
    r"@everyone.*https?://",
    r"airdrop.*https?://",
    r"free\s*robux",
    r"earn\s*free\s*crypto",
    r"click\s*(here|this)\s*to\s*claim",
]

URL_REGEX = re.compile(r"https?://([^\s/]+)", re.IGNORECASE)

# ============================================================
#  ANTI-MASS BAN/KICK
# ============================================================
MASS_ACTION_THRESHOLD = 3   # bans/kicks within the window
MASS_ACTION_WINDOW = 60     # seconds

# ============================================================
#  ACCOUNT AGE GATE
# ============================================================
MIN_ACCOUNT_AGE_DAYS = 7    # accounts younger than this get kicked

# ============================================================
#  ANTI-MENTION SPAM
# ============================================================
MAX_MENTIONS_PER_MSG = 5    # max user mentions in one message
