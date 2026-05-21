import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import time
import random
import secrets
import google.generativeai as genai

from config import (
    GEMINI_API_KEY, BLACKLIST_ROLE_ID,
    RANK_ORDER, RANK_NAMES, HC_RANKS,
    SPAM_THRESHOLD, SPAM_WINDOW, SPAM_TIMEOUT_MINS,
    MASS_ACTION_THRESHOLD, MASS_ACTION_WINDOW,
    MIN_ACCOUNT_AGE_DAYS, MAX_MENTIONS_PER_MSG,
    SCAM_DOMAINS, SCAM_PATTERNS
)
from database import c, conn, get_user_stats
from security import channel_cache, role_cache
import security  # To modify lockdown_active

# ============================================================
#  AI Configuration
# ============================================================
genai.configure(api_key=GEMINI_API_KEY)

# ============================================================
#  DUTY COMMANDS — Spawn Panel / Statcheck / Promoready
# ============================================================

def setup_commands(bot):
    """Register all slash commands on the bot."""

    @bot.tree.command(name="spawn_panel", description="Spawns the permanent clock-in buttons.")
    @app_commands.default_permissions(administrator=True)
    async def spawn_panel(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        from panels import DutyPanel, build_duty_embed
        embed = build_duty_embed(interaction.guild)
        await interaction.response.send_message(embed=embed, view=DutyPanel())

    @bot.tree.command(name="spawn_callsign_panel", description="Spawns the callsign assignment panel with rank dropdown.")
    @app_commands.default_permissions(administrator=True)
    async def spawn_callsign_panel(interaction: discord.Interaction):
        from panels import CallsignPanel
        embed = discord.Embed(
            title="🏷️ LSPD Callsign Assignment",
            description="Select your rank below to receive your **callsign** and **badge number**.\n\n"
                        "⚠️ This will overwrite your current callsign if you already have one.",
            color=discord.Color.blue()
        )
        embed.set_footer(text="LSPD Department • Callsign System")
        await interaction.response.send_message(embed=embed, view=CallsignPanel())

    @bot.tree.command(name="assign_callsign", description="Assigns rank, unit number, and badge.")
    @app_commands.choices(rank=[
        app_commands.Choice(name="Cadet (CDT)", value="CDT"),
        app_commands.Choice(name="Officer (OFC)", value="OFC"),
        app_commands.Choice(name="Detective (DET)", value="DET"),
        app_commands.Choice(name="Corporal (CRP)", value="CRP"),
        app_commands.Choice(name="Sergeant (SGT)", value="SGT"),
        app_commands.Choice(name="Lieutenant (LT)", value="LT"),
        app_commands.Choice(name="Captain (CPT)", value="CPT"),
        app_commands.Choice(name="Major (MJR)", value="MJR"),
        app_commands.Choice(name="Commander (CMD)", value="CMD"),
        app_commands.Choice(name="Deputy Chief (DC)", value="DC"),
        app_commands.Choice(name="Assistant Chief (AC)", value="AC"),
        app_commands.Choice(name="Chief of Police (COP)", value="COP"),
        app_commands.Choice(name="Head of Police (HOP)", value="HOP")
    ])
    async def assign_callsign(interaction: discord.Interaction, rank: app_commands.Choice[str]):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        c.execute("SELECT COUNT(*) FROM roster WHERE rank = ?", (rank.value,))
        row = c.fetchone()
        count = row[0] if row else 0

        if count >= 100:
            await interaction.response.send_message(f"Rank {rank.value} is full.", ephemeral=True)
            return

        # Random unique unit number per rank
        next_unit = None
        while next_unit is None:
            candidate = random.randint(1, 99)
            c.execute("SELECT 1 FROM roster WHERE rank = ? AND unit_number = ?", (rank.value, candidate))
            if not c.fetchone():
                next_unit = candidate

        badge = None
        while badge is None:
            candidate = random.randint(1, 999)
            c.execute("SELECT 1 FROM roster WHERE badge = ?", (candidate,))
            if not c.fetchone():
                badge = candidate

        c.execute("INSERT OR REPLACE INTO roster (user_id, rank, unit_number, badge) VALUES (?, ?, ?, ?)", 
                  (interaction.user.id, rank.value, next_unit, badge))
        conn.commit()
        
        callsign = f"[{rank.value} {next_unit:02d}]"
        # Strip old callsign from nickname if present
        clean_name = interaction.user.display_name
        if clean_name.startswith("[") and "]" in clean_name:
            clean_name = clean_name.split("]", 1)[-1].strip()
        try:
            await interaction.user.edit(nick=f"{callsign} {clean_name}")
            await interaction.response.send_message(f"Assigned {callsign} | Badge: {badge}. Nickname updated.")
        except discord.Forbidden:
            await interaction.response.send_message(f"Assigned {callsign} | Badge: {badge}. Nickname fallback generated manually.")

    @bot.tree.command(name="blacklist_user", description="Permanently blacklists a Discord ID.")
    @app_commands.default_permissions(administrator=True)
    async def blacklist_user(interaction: discord.Interaction, discord_id: str):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        try:
            user_id = int(discord_id)
        except ValueError:
            await interaction.response.send_message("Invalid ID format.")
            return

        c.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (user_id,))
        conn.commit()
        
        member = interaction.guild.get_member(user_id)
        if member:
            role = interaction.guild.get_role(BLACKLIST_ROLE_ID)
            if role:
                await member.add_roles(role)
        await interaction.response.send_message(f"User {user_id} added to database blacklist.")

    @bot.tree.command(name="statcheck", description="Check your current rolling activity stats. Limit: 2 per day.")
    @app_commands.checks.cooldown(2, 86400.0, key=lambda i: i.user.id)
    async def statcheck(interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        c.execute("SELECT rank FROM roster WHERE user_id = ?", (interaction.user.id,))
        rank_row = c.fetchone()
        rank = rank_row[0] if rank_row else "OFC"
        
        days_window = 14 if rank in HC_RANKS else 7
        
        data = get_user_stats(interaction.user.id, days_window)
        
        stats_text = (f"**Tracking Window:** Last {days_window} days based on rank\n"
                      f"**PD Hours:** {data['hours']:.2f}\n"
                      f"**Arrests Logs:** {int(data['arrests'])}\n"
                      f"**Robbery Logs:** {int(data['robberies'])}\n"
                      f"**Tickets Issued:** {int(data['tickets'])}\n"
                      f"**FTO Trainings:** {int(data['fto'])}")
                      
        embed = discord.Embed(title=f"Activity Check - {interaction.user.display_name}", description=stats_text, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @statcheck.error
    async def statcheck_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            hours_left = error.retry_after / 3600
            await interaction.response.send_message(f"Limit reached. Cooldown expires in {hours_left:.1f} hours.", ephemeral=True)

    @bot.tree.command(name="promoready", description="Displays Top active members using automated rolling cycles.")
    async def promoready(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        c.execute("SELECT user_id, rank FROM roster")
        all_members = c.fetchall()
        
        if not all_members:
            await interaction.response.send_message("Roster database is empty.")
            return

        officers_list = []
        hc_list = []

        for member_id, rank in all_members:
            user = interaction.guild.get_member(member_id)
            name = user.mention if user else f"<@{member_id}>"
            
            if rank in HC_RANKS:
                data = get_user_stats(member_id, 14)
                if data['hours'] > 0 or data['robberies'] > 0 or data['arrests'] > 0 or data['tickets'] > 0 or data['fto'] > 0:
                    hc_list.append((data['hours'], f"{name} • {data['hours']:.1f}h • 🔫 {int(data['robberies'])} • 🚔 {int(data['arrests'])} • 🎫 {int(data['tickets'])} • 📘 {int(data['fto'])}"))
            else:
                data = get_user_stats(member_id, 7)
                if data['hours'] > 0 or data['robberies'] > 0 or data['arrests'] > 0 or data['fto'] > 0:
                    officers_list.append((data['hours'], f"{name} • {data['hours']:.1f}h • 🔫 {int(data['robberies'])} • 🚔 {int(data['arrests'])} • 📘 {int(data['fto'])}"))

        officers_list.sort(key=lambda x: x[0], reverse=True)
        hc_list.sort(key=lambda x: x[0], reverse=True)
        
        officers_text = "\n".join([f"#{i+1:02d} {line[1]}" for i, line in enumerate(officers_list[:10])]) or "No active lower ranks recorded."
        hc_text = "\n".join([f"#{i+1:02d} {line[1]}" for i, line in enumerate(hc_list[:10])]) or "No active high command recorded."

        embed = discord.Embed(title="📋 ROSTER — Activity Stats", color=discord.Color.dark_theme())
        embed.add_field(name=f"🛡️ Officers — last 7 days ({len(officers_list[:10])})", value=officers_text, inline=False)
        embed.add_field(name=f"⭐ High Command — last 14 days ({len(hc_list[:10])})", value=hc_text, inline=False)

        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="ticket_summary", description="Uses AI to read and explain the ticket context.")
    async def ticket_summary(interaction: discord.Interaction):
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("This command can only be used in a text channel or thread.", ephemeral=True)
            return
        await interaction.response.defer()
        messages = [message async for message in interaction.channel.history(limit=20)]
        messages.reverse()
        
        if not messages:
            await interaction.followup.send("This channel is empty.")
            return

        raw_transcript = "\n".join([f"{m.author.display_name}: {m.content}" for m in messages if m.content])
        
        try:
            model = genai.GenerativeModel('gemini-2.0-flash')
            prompt = f"Read this transcript from a police roleplay Discord ticket. Briefly explain what is happening, who is involved, and what they need. Keep it professional and short:\n\n{raw_transcript}"
            response = model.generate_content(prompt)
            ai_summary = response.text
        except Exception as e:
            ai_summary = f"The AI failed to generate a summary.\n**Error:** `{e}`"
            print(f"AI Error: {e}")

        embed = discord.Embed(title="AI Ticket Summary", description=ai_summary, color=discord.Color.purple())
        await interaction.followup.send(embed=embed)

    # ============================================================
    #  MODERATION COMMANDS — Warn / Warnings / Purge
    # ============================================================

    @bot.tree.command(name="warn", description="⚠️ Issue a warning to a member.")
    @app_commands.default_permissions(manage_messages=True)
    async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        c.execute("INSERT INTO warnings (user_id, mod_id, reason, timestamp) VALUES (?, ?, ?, ?)",
                  (member.id, interaction.user.id, reason, time.time()))
        conn.commit()
        c.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ?", (member.id,))
        row = c.fetchone()
        total = row[0] if row else 0
        
        embed = discord.Embed(title="⚠️ Warning Issued", color=discord.Color.orange())
        embed.add_field(name="Member", value=member.mention, inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Total Warnings", value=str(total), inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        
        try:
            await member.send(f"⚠️ You received a warning in **{interaction.guild.name}**\n**Reason:** {reason}\n**Total Warnings:** {total}")
        except discord.Forbidden:
            pass

    @bot.tree.command(name="warnings", description="📋 View all warnings for a member.")
    async def warnings(interaction: discord.Interaction, member: discord.Member):
        c.execute("SELECT reason, mod_id, timestamp FROM warnings WHERE user_id = ? ORDER BY timestamp DESC", (member.id,))
        rows = c.fetchall()
        
        if not rows:
            await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
            return
        
        warn_list = []
        for i, (reason, mod_id, ts) in enumerate(rows, 1):
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            warn_list.append(f"**{i}.** {reason}\n   *By <@{mod_id}> on {date}*")
        
        embed = discord.Embed(
            title=f"⚠️ Warnings — {member.display_name}",
            description="\n\n".join(warn_list[:10]),
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"Total: {len(rows)} warning(s)")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="clearwarnings", description="🗑️ Clear all warnings for a member.")
    @app_commands.default_permissions(administrator=True)
    async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
        c.execute("DELETE FROM warnings WHERE user_id = ?", (member.id,))
        conn.commit()
        await interaction.response.send_message(f"✅ All warnings cleared for {member.mention}.")

    @bot.tree.command(name="purge", description="🗑️ Bulk delete messages from the current channel.")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(interaction: discord.Interaction, amount: int):
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("This command can only be used in a text channel or thread.", ephemeral=True)
            return
        if amount < 1 or amount > 100:
            await interaction.response.send_message("Amount must be between 1 and 100.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🗑️ Deleted **{len(deleted)}** messages.", ephemeral=True)

    # ============================================================
    #  ROSTER COMMANDS — Roster / Promote / Demote / Lookup
    # ============================================================

    @bot.tree.command(name="roster", description="📋 Display the full department roster with ranks.")
    async def roster(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        c.execute("SELECT user_id, rank, unit_number, badge FROM roster ORDER BY rank, unit_number")
        rows = c.fetchall()
        
        if not rows:
            await interaction.response.send_message("Roster is empty.")
            return
        
        grouped = {}
        for uid, rank, unit, badge in rows:
            if rank not in grouped:
                grouped[rank] = []
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"Unknown ({uid})"
            grouped[rank].append(f"`[{rank} {unit:03d}]` {name} — Badge #{badge}")
        
        embed = discord.Embed(title="📋 LSPD Department Roster", color=discord.Color.dark_blue())
        
        for rank_code in reversed(RANK_ORDER):
            if rank_code in grouped:
                rank_name = RANK_NAMES.get(rank_code, rank_code)
                embed.add_field(
                    name=f"{rank_name} ({len(grouped[rank_code])})",
                    value="\n".join(grouped[rank_code][:10]),
                    inline=False
                )
        
        embed.set_footer(text=f"Total: {len(rows)} officers")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="promote", description="⬆️ Promote a member to the next rank.")
    @app_commands.default_permissions(administrator=True)
    async def promote(interaction: discord.Interaction, member: discord.Member):
        c.execute("SELECT rank, unit_number, badge FROM roster WHERE user_id = ?", (member.id,))
        row = c.fetchone()
        if not row:
            await interaction.response.send_message(f"{member.mention} is not on the roster.", ephemeral=True)
            return
        
        current_rank = row[0]
        current_idx = RANK_ORDER.index(current_rank) if current_rank in RANK_ORDER else -1
        
        if current_idx >= len(RANK_ORDER) - 1:
            await interaction.response.send_message(f"{member.mention} is already at the highest rank.", ephemeral=True)
            return
        
        new_rank = RANK_ORDER[current_idx + 1]
        
        new_unit = None
        while new_unit is None:
            candidate = random.randint(1, 99)
            c.execute("SELECT 1 FROM roster WHERE rank = ? AND unit_number = ?", (new_rank, candidate))
            if not c.fetchone():
                new_unit = candidate
        
        c.execute("UPDATE roster SET rank = ?, unit_number = ? WHERE user_id = ?", (new_rank, new_unit, member.id))
        conn.commit()
        
        callsign = f"[{new_rank} {new_unit:02d}]"
        try:
            await member.edit(nick=f"{callsign} {member.display_name.split(']')[-1].strip()}")
        except discord.Forbidden:
            pass
        
        embed = discord.Embed(title="⬆️ Promotion", color=discord.Color.green())
        embed.add_field(name="Member", value=member.mention, inline=True)
        embed.add_field(name="From", value=f"{RANK_NAMES.get(current_rank, current_rank)}", inline=True)
        embed.add_field(name="To", value=f"{RANK_NAMES.get(new_rank, new_rank)}", inline=True)
        embed.add_field(name="New Callsign", value=callsign, inline=False)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="demote", description="⬇️ Demote a member to the previous rank.")
    @app_commands.default_permissions(administrator=True)
    async def demote(interaction: discord.Interaction, member: discord.Member):
        c.execute("SELECT rank, unit_number, badge FROM roster WHERE user_id = ?", (member.id,))
        row = c.fetchone()
        if not row:
            await interaction.response.send_message(f"{member.mention} is not on the roster.", ephemeral=True)
            return
        
        current_rank = row[0]
        current_idx = RANK_ORDER.index(current_rank) if current_rank in RANK_ORDER else -1
        
        if current_idx <= 0:
            await interaction.response.send_message(f"{member.mention} is already at the lowest rank.", ephemeral=True)
            return
        
        new_rank = RANK_ORDER[current_idx - 1]
        
        new_unit = None
        while new_unit is None:
            candidate = random.randint(1, 99)
            c.execute("SELECT 1 FROM roster WHERE rank = ? AND unit_number = ?", (new_rank, candidate))
            if not c.fetchone():
                new_unit = candidate
        
        c.execute("UPDATE roster SET rank = ?, unit_number = ? WHERE user_id = ?", (new_rank, new_unit, member.id))
        conn.commit()
        
        callsign = f"[{new_rank} {new_unit:02d}]"
        try:
            await member.edit(nick=f"{callsign} {member.display_name.split(']')[-1].strip()}")
        except discord.Forbidden:
            pass
        
        embed = discord.Embed(title="⬇️ Demotion", color=discord.Color.red())
        embed.add_field(name="Member", value=member.mention, inline=True)
        embed.add_field(name="From", value=f"{RANK_NAMES.get(current_rank, current_rank)}", inline=True)
        embed.add_field(name="To", value=f"{RANK_NAMES.get(new_rank, new_rank)}", inline=True)
        embed.add_field(name="New Callsign", value=callsign, inline=False)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="lookup", description="🔍 Full profile lookup — rank, badge, stats, and warnings.")
    async def lookup(interaction: discord.Interaction, member: discord.Member):
        c.execute("SELECT rank, unit_number, badge FROM roster WHERE user_id = ?", (member.id,))
        roster_row = c.fetchone()
        
        c.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ?", (member.id,))
        row = c.fetchone()
        warn_count = row[0] if row else 0
        
        if roster_row:
            rank = roster_row[0]
            days = 14 if rank in HC_RANKS else 7
            stats = get_user_stats(member.id, days)
            callsign = f"[{roster_row[0]} {roster_row[1]:03d}]"
            rank_name = RANK_NAMES.get(roster_row[0], roster_row[0])
        else:
            stats = get_user_stats(member.id, 7)
            callsign = "None"
            rank_name = "Not on roster"
            days = 7
        
        c.execute("SELECT start_date, end_date, reason, approved FROM loa_requests WHERE user_id = ?", (member.id,))
        loa_row = c.fetchone()
        loa_text = "None"
        if loa_row:
            status = "✅ Approved" if loa_row[3] else "⏳ Pending"
            loa_text = f"{loa_row[0]} to {loa_row[1]} — {status}"
        
        embed = discord.Embed(title=f"🔍 Profile — {member.display_name}", color=discord.Color.blue())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Rank", value=rank_name, inline=True)
        embed.add_field(name="Callsign", value=callsign, inline=True)
        embed.add_field(name="Badge", value=f"#{roster_row[2]}" if roster_row else "N/A", inline=True)
        embed.add_field(name=f"Stats (last {days} days)",
            value=f"🕐 {stats['hours']:.1f}h • 🚔 {int(stats['arrests'])} arrests • 🔫 {int(stats['robberies'])} robberies\n"
                  f"🎫 {int(stats['tickets'])} tickets • 📘 {int(stats['fto'])} FTO",
            inline=False)
        embed.add_field(name="Warnings", value=f"⚠️ {warn_count}", inline=True)
        embed.add_field(name="LOA", value=loa_text, inline=True)
        embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown", inline=True)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="userinfo", description="ℹ️ Show detailed Discord info about a user.")
    async def userinfo(interaction: discord.Interaction, member: discord.Member):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        account_age = datetime.now(timezone.utc) - member.created_at
        roles = [r.mention for r in reversed(member.roles) if r != interaction.guild.default_role]
        
        embed = discord.Embed(title=f"ℹ️ User Info — {member}", color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Nickname", value=member.nick or "None", inline=True)
        embed.add_field(name="Bot?", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Account Created", value=f"{member.created_at.strftime('%Y-%m-%d')} ({account_age.days} days ago)", inline=False)
        embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown", inline=True)
        embed.add_field(name="Top Role", value=member.top_role.mention, inline=True)
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles[:15]) if roles else "None", inline=False)
        await interaction.response.send_message(embed=embed)

    # ============================================================
    #  LOA SYSTEM — Leave of Absence
    # ============================================================

    @bot.tree.command(name="loa", description="📝 Request a Leave of Absence.")
    async def loa(interaction: discord.Interaction, start_date: str, end_date: str, reason: str):
        c.execute("INSERT OR REPLACE INTO loa_requests (user_id, start_date, end_date, reason, approved) VALUES (?, ?, ?, ?, 0)",
                  (interaction.user.id, start_date, end_date, reason))
        conn.commit()
        
        embed = discord.Embed(title="📝 LOA Request Submitted", color=discord.Color.gold())
        embed.add_field(name="Officer", value=interaction.user.mention, inline=True)
        embed.add_field(name="Period", value=f"{start_date} → {end_date}", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Status", value="⏳ Pending Approval", inline=False)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="loa_approve", description="✅ Approve a member's LOA request.")
    @app_commands.default_permissions(administrator=True)
    async def loa_approve(interaction: discord.Interaction, member: discord.Member):
        c.execute("SELECT start_date, end_date, reason FROM loa_requests WHERE user_id = ?", (member.id,))
        row = c.fetchone()
        if not row:
            await interaction.response.send_message(f"{member.mention} has no pending LOA.", ephemeral=True)
            return
        
        c.execute("UPDATE loa_requests SET approved = 1 WHERE user_id = ?", (member.id,))
        conn.commit()
        
        embed = discord.Embed(title="✅ LOA Approved", color=discord.Color.green())
        embed.add_field(name="Officer", value=member.mention, inline=True)
        embed.add_field(name="Period", value=f"{row[0]} → {row[1]}", inline=True)
        embed.add_field(name="Approved By", value=interaction.user.mention, inline=True)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="loa_deny", description="❌ Deny a member's LOA request.")
    @app_commands.default_permissions(administrator=True)
    async def loa_deny(interaction: discord.Interaction, member: discord.Member):
        c.execute("DELETE FROM loa_requests WHERE user_id = ?", (member.id,))
        conn.commit()
        await interaction.response.send_message(f"❌ LOA request from {member.mention} has been denied.")

    # ============================================================
    #  SUGGESTION SYSTEM
    # ============================================================

    @bot.tree.command(name="suggest", description="💡 Submit a suggestion.")
    async def suggest(interaction: discord.Interaction, suggestion: str):
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("This command can only be used in a text channel or thread.", ephemeral=True)
            return
        embed = discord.Embed(title="💡 New Suggestion", description=suggestion, color=discord.Color.gold())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"Suggestion by {interaction.user} • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
        
        msg = await interaction.channel.send(embed=embed)
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")
        await interaction.response.send_message("✅ Your suggestion has been posted!", ephemeral=True)

    # ============================================================
    #  SECURITY COMMANDS — Lockdown / Unlock / Security Status
    # ============================================================

    @bot.tree.command(name="lockdown", description="🚨 Emergency lockdown — locks all channels immediately.")
    @app_commands.default_permissions(administrator=True)
    async def lockdown(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        security.lockdown_active = True
        guild = interaction.guild
        locked = 0
        for ch in guild.text_channels:
            try:
                await ch.set_permissions(guild.default_role, send_messages=False, reason=f"Manual lockdown by {interaction.user}")
                locked += 1
            except discord.Forbidden:
                pass
        await interaction.response.send_message(f"🔒 **SERVER LOCKED DOWN** — {locked} channels locked. Use `/unlock` to restore.")

    @bot.tree.command(name="unlock", description="✅ Lifts the server lockdown and restores channel access.")
    @app_commands.default_permissions(administrator=True)
    async def unlock(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        security.lockdown_active = False
        guild = interaction.guild
        unlocked = 0
        for ch in guild.text_channels:
            try:
                await ch.set_permissions(guild.default_role, send_messages=None, reason=f"Lockdown lifted by {interaction.user}")
                unlocked += 1
            except discord.Forbidden:
                pass
        await interaction.response.send_message(f"🔓 **LOCKDOWN LIFTED** — {unlocked} channels unlocked.")

    @bot.tree.command(name="security_status", description="📊 View all active security layers and their settings.")
    async def security_status(interaction: discord.Interaction):
        status = (
            "**🛡️ LSPD Bot — Security Status**\n\n"
            f"🔒 **Server Lockdown:** {'🔴 ACTIVE' if security.lockdown_active else '🟢 Inactive'}\n\n"
            f"**Anti-Spam** — {SPAM_THRESHOLD} msgs / {SPAM_WINDOW}s → {SPAM_TIMEOUT_MINS}min timeout\n"
            f"**Anti-Spam** — Repeated chars, words, caps, keyboard mash → delete + timeout\n"
            f"**Anti-Scam** — {len(SCAM_DOMAINS)} scam domains + {len(SCAM_PATTERNS)} phrase patterns blocked\n"
            f"**Anti-Mention Spam** — Max {MAX_MENTIONS_PER_MSG} pings per message\n"
            f"**Account Age Gate** — Accounts < {MIN_ACCOUNT_AGE_DAYS} days old get kicked\n"
            f"**Anti-Mass Ban** — {MASS_ACTION_THRESHOLD} bans / {MASS_ACTION_WINDOW}s → strip roles + max timeout\n"
            f"**Anti-Mass Kick** — {MASS_ACTION_THRESHOLD} kicks / {MASS_ACTION_WINDOW}s → strip roles + max timeout\n"
            f"**Anti-Nuke** — {len(channel_cache)} channels, {len(role_cache)} roles cached for auto-restore\n"
            f"**Anti-Nuke** — Attacker gets all roles stripped + 27-day timeout\n"
            f"**Anti-Webhook** — Unauthorized webhooks auto-deleted\n"
        )
        embed = discord.Embed(title="Security Dashboard", description=status, color=discord.Color.red())
        await interaction.response.send_message(embed=embed)

    # ============================================================
    #  DASHBOARD — Access Key Generator
    # ============================================================

    @bot.tree.command(name="acc", description="🔑 Generate a dashboard access key (DMs you the key).")
    @app_commands.default_permissions(administrator=True)
    async def acc(interaction: discord.Interaction):
        key = secrets.token_hex(8)  # 16-character hex key
        c.execute("INSERT INTO access_keys (user_id, key, created_at) VALUES (?, ?, ?)",
                  (interaction.user.id, key, time.time()))
        conn.commit()

        embed = discord.Embed(
            title="🔑 LSPD Dashboard Access Key",
            description=(
                f"Your access key:\n```{key}```\n"
                f"🌐 **Dashboard URL:** http://localhost:5000\n\n"
                f"1️⃣ Open the URL in your browser\n"
                f"2️⃣ Paste your access key\n"
                f"3️⃣ Authorize with Discord\n\n"
                f"⚠️ This key can only be used **once**. Do not share it."
            ),
            color=discord.Color.blue()
        )

        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message("✅ Access key sent to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"🔑 Your access key: `{key}`\n🌐 Dashboard: http://localhost:5000\n⚠️ One-time use only.",
                ephemeral=True
            )
