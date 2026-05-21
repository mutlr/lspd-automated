import discord
import time
import random

from config import RANK_ORDER, RANK_NAMES, ON_DUTY_ROLE_ID
from database import c, conn

# ============================================================
#  DUTY PANEL — Clock In / Clock Out Buttons
# ============================================================

def build_duty_embed(guild):
    """Build the duty terminal embed showing who's currently on duty."""
    c.execute("SELECT user_id, clock_in_time FROM on_duty")
    rows = c.fetchall()

    if rows:
        lines = []
        for uid, clock_in_time in rows:
            member = guild.get_member(uid)
            name = member.display_name if member else f"Unknown ({uid})"
            timestamp = int(clock_in_time)
            lines.append(f"🟢 **{name}** — <t:{timestamp}:R>")
        duty_list = "\n".join(lines)
    else:
        duty_list = "*No officers currently on duty.*"

    embed = discord.Embed(
        title="LSPD Duty Terminal",
        description="Log your shift status below.",
        color=discord.Color.dark_gray()
    )
    embed.add_field(name=f"🛡️ On Duty ({len(rows)})", value=duty_list, inline=False)
    return embed


class DutyPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Clock In", style=discord.ButtonStyle.green, custom_id="btn_clock_in")
    async def clock_in_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        c.execute("SELECT clock_in_time FROM on_duty WHERE user_id = ?", (interaction.user.id,))
        row = c.fetchone()

        if row and row[0] is not None:
            await interaction.response.send_message("You are already clocked in.", ephemeral=True)
            return

        c.execute("INSERT OR REPLACE INTO on_duty (user_id, clock_in_time) VALUES (?, ?)", (interaction.user.id, time.time()))
        conn.commit()

        # Add On Duty role
        role = interaction.guild.get_role(ON_DUTY_ROLE_ID)
        if role:
            try:
                await interaction.user.add_roles(role, reason="Clocked in")
            except discord.Forbidden:
                pass

        # Update the panel embed to show who's on duty
        await interaction.response.edit_message(embed=build_duty_embed(interaction.guild), view=self)
        await interaction.followup.send(f"{interaction.user.mention} is now **10-41** (On Duty).", silent=True)

    @discord.ui.button(label="Clock Out", style=discord.ButtonStyle.red, custom_id="btn_clock_out")
    async def clock_out_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        c.execute("SELECT clock_in_time FROM on_duty WHERE user_id = ?", (interaction.user.id,))
        row = c.fetchone()

        if not row or row[0] is None:
            await interaction.response.send_message("You are not clocked in.", ephemeral=True)
            return

        session_time = (time.time() - row[0]) / 3600

        c.execute("INSERT INTO activity_logs (user_id, log_type, amount, timestamp) VALUES (?, 'hours', ?, ?)",
                  (interaction.user.id, session_time, time.time()))
        c.execute("DELETE FROM on_duty WHERE user_id = ?", (interaction.user.id,))
        conn.commit()

        # Remove On Duty role
        role = interaction.guild.get_role(ON_DUTY_ROLE_ID)
        if role:
            try:
                await interaction.user.remove_roles(role, reason="Clocked out")
            except discord.Forbidden:
                pass

        # Update the panel embed to show who's on duty
        await interaction.response.edit_message(embed=build_duty_embed(interaction.guild), view=self)
        await interaction.followup.send(f"{interaction.user.mention} is now **10-42** (Off Duty). Shift: **{session_time:.2f}h**.", silent=True)

# ============================================================
#  CALLSIGN PANEL — Rank Dropdown Selector
# ============================================================
class CallsignDropdown(discord.ui.Select):
    def __init__(self):
        options = []
        for code in RANK_ORDER:
            name = RANK_NAMES.get(code, code)
            options.append(discord.SelectOption(label=f"{name} ({code})", value=code))
        super().__init__(placeholder="Select your rank...", options=options, custom_id="callsign_rank_select")

    async def callback(self, interaction: discord.Interaction):
        rank = self.values[0]
        rank_name = RANK_NAMES.get(rank, rank)

        c.execute("SELECT COUNT(*) FROM roster WHERE rank = ?", (rank,))
        count = c.fetchone()[0]

        if count >= 100:
            await interaction.response.send_message(f"Rank {rank} is full.", ephemeral=True)
            return

        # Random unique unit number per rank
        next_unit = None
        while next_unit is None:
            candidate = random.randint(1, 99)
            c.execute("SELECT 1 FROM roster WHERE rank = ? AND unit_number = ?", (rank, candidate))
            if not c.fetchone():
                next_unit = candidate

        badge = None
        while badge is None:
            candidate = random.randint(1, 999)
            c.execute("SELECT 1 FROM roster WHERE badge = ?", (candidate,))
            if not c.fetchone():
                badge = candidate

        c.execute("INSERT OR REPLACE INTO roster (user_id, rank, unit_number, badge) VALUES (?, ?, ?, ?)",
                  (interaction.user.id, rank, next_unit, badge))
        conn.commit()

        callsign = f"[{rank} {next_unit:02d}]"
        # Strip old callsign from nickname if present
        clean_name = interaction.user.display_name
        if clean_name.startswith("[") and "]" in clean_name:
            clean_name = clean_name.split("]", 1)[-1].strip()
        try:
            await interaction.user.edit(nick=f"{callsign} {clean_name}")
        except discord.Forbidden:
            pass

        embed = discord.Embed(title="✅ Callsign Assigned", color=discord.Color.green())
        embed.add_field(name="Rank", value=rank_name, inline=True)
        embed.add_field(name="Callsign", value=callsign, inline=True)
        embed.add_field(name="Badge", value=f"#{badge}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class CallsignPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CallsignDropdown())
