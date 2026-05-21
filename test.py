import discord
from discord.ext import commands

from config import BOT_TOKEN
from database import c, conn
from panels import DutyPanel, CallsignPanel
from security import setup_security
from commands import setup_commands

# ============================================================
#  INTENTS
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True

# ============================================================
#  BOT SETUP
# ============================================================
class LSPDBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(DutyPanel())
        self.add_view(CallsignPanel())
        print("[OK] Active views loaded successfully (auto-sync skipped).")

bot = LSPDBot()

# Register all security event handlers and commands
setup_security(bot)
setup_commands(bot)

@bot.command()
@commands.is_owner()
async def sync(ctx):
    """Manually sync application commands to Discord."""
    await ctx.send("🔄 Syncing slash commands...")
    try:
        synced = await ctx.bot.tree.sync()
        await ctx.send(f"✅ Successfully synced {len(synced)} slash commands with Discord!")
    except Exception as e:
        await ctx.send(f"❌ Failed to sync: `{e}`")

# ============================================================
#  START THE BOT
# ============================================================
bot.run(BOT_TOKEN)