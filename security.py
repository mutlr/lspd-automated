import discord
from discord.ext import commands
from datetime import timedelta, datetime, timezone
import re
import time

from config import (
    SCAM_DOMAINS, SCAM_PATTERNS, URL_REGEX,
    SPAM_THRESHOLD, SPAM_WINDOW, SPAM_TIMEOUT_MINS,
    MASS_ACTION_THRESHOLD, MASS_ACTION_WINDOW,
    MIN_ACCOUNT_AGE_DAYS, MAX_MENTIONS_PER_MSG,
    ROBBERY_LOG_CHANNEL_ID, TICKET_LOG_CHANNEL_ID,
    ARREST_LOG_CHANNEL_ID, FTO_LOG_CHANNEL_ID,
    BLACKLIST_ROLE_ID
)
from database import c, conn, log_security_event

# ============================================================
#  ANTI-NUKE SYSTEM — Channel & Role Backup/Restore
# ============================================================
channel_cache = {}      # channel_id -> dict of properties
role_cache = {}         # role_id -> dict of properties
role_members = {}       # role_id -> set of member_ids
_category_id_map = {}   # old_category_id -> new_category_id

def cache_channel(channel):
    """Snapshot a channel's properties for restoration."""
    if isinstance(channel, discord.CategoryChannel):
        ch_kind = 'category'
    elif isinstance(channel, discord.VoiceChannel):
        ch_kind = 'voice'
    elif isinstance(channel, discord.TextChannel):
        ch_kind = 'text'
    else:
        return

    overwrites = {}
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        overwrites[target.id] = {
            'is_role': isinstance(target, discord.Role),
            'allow': allow.value,
            'deny': deny.value
        }

    data = {
        'name': channel.name,
        'kind': ch_kind,
        'category_id': channel.category_id,
        'position': channel.position,
        'overwrites': overwrites,
    }

    if ch_kind == 'text':
        data['topic'] = channel.topic
        data['nsfw'] = channel.nsfw
        data['slowmode_delay'] = channel.slowmode_delay
    elif ch_kind == 'voice':
        data['bitrate'] = channel.bitrate
        data['user_limit'] = channel.user_limit

    channel_cache[channel.id] = data

def cache_role(role):
    """Snapshot a role's properties for restoration."""
    if role.is_default() or role.managed:
        return
    role_cache[role.id] = {
        'name': role.name,
        'color': role.color,
        'permissions': role.permissions,
        'hoist': role.hoist,
        'mentionable': role.mentionable,
    }

def snapshot_role_members(guild):
    """Cache which members hold each role."""
    role_members.clear()
    for member in guild.members:
        for role in member.roles:
            if role.id not in role_members:
                role_members[role.id] = set()
            role_members[role.id].add(member.id)

# ============================================================
#  RUNTIME STATE
# ============================================================
spam_tracker = {}       # user_id -> list of message timestamps
ban_tracker = {}        # user_id -> list of timestamps
kick_tracker = {}       # user_id -> list of timestamps
webhook_cache = {}      # guild_id -> set of webhook IDs
lockdown_active = False

# ============================================================
#  HELPER — Strip all roles from a nuker
# ============================================================
async def strip_roles_from_attacker(guild, user_id, reason):
    """Remove all roles from a member caught nuking."""
    member = guild.get_member(user_id)
    if member:
        removable = [r for r in member.roles if r != guild.default_role and not r.managed and r < guild.me.top_role]
        if removable:
            try:
                await member.remove_roles(*removable, reason=reason)
                print(f"Security: Stripped {len(removable)} roles from {member} — {reason}")
            except discord.Forbidden:
                print(f"Security: Cannot strip roles from {member} (role too high).")
        try:
            await member.timeout(timedelta(days=27), reason=reason)
            print(f"Security: Max-timed out {member} — {reason}")
        except discord.Forbidden:
            pass

# ============================================================
#  HELPER — Find who performed an action via Audit Log
# ============================================================
async def get_audit_attacker(guild, action, bot_user_id, target_id=None):
    """Check the audit log to find who performed a destructive action."""
    try:
        async for entry in guild.audit_logs(limit=1, action=action):
            if entry.user.id != bot_user_id:
                if target_id is None or entry.target.id == target_id:
                    return entry.user
    except discord.Forbidden:
        print("Security: Missing 'View Audit Log' permission.")
    return None

# ============================================================
#  SETUP — Register all security events on the bot
# ============================================================
def setup_security(bot):
    """Register all security event handlers on the bot."""

    @bot.event
    async def on_ready():
        print("Automated Channel-Tracking LSPD Bot Online.")

        # Step 1: Copy commands to each guild and sync
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            for channel in guild.channels:
                cache_channel(channel)
            for role in guild.roles:
                cache_role(role)
            snapshot_role_members(guild)
            try:
                hooks = await guild.webhooks()
                webhook_cache[guild.id] = {wh.id for wh in hooks}
            except discord.Forbidden:
                webhook_cache[guild.id] = set()

        # Step 2: Remove global commands so they don't show as duplicates
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()

        print(f"Commands synced to {len(bot.guilds)} guild(s). Global duplicates cleared.")
        print(f"Anti-Nuke: Cached {len(channel_cache)} channels, {len(role_cache)} roles.")
        print("Security: All protection layers active.")

    # ============================================================
    #  EVENT: on_member_join — Blacklist + Account Age Gate
    # ============================================================
    @bot.event
    async def on_member_join(member):
        c.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (member.id,))
        if c.fetchone():
            role = member.guild.get_role(BLACKLIST_ROLE_ID)
            if role:
                await member.add_roles(role)

        account_age = datetime.now(timezone.utc) - member.created_at
        if account_age.days < MIN_ACCOUNT_AGE_DAYS:
            try:
                await member.send(
                    f"⛔ Your account is too new ({account_age.days} days old). "
                    f"Minimum age required: {MIN_ACCOUNT_AGE_DAYS} days. Please try again later."
                )
            except discord.Forbidden:
                pass
            try:
                await member.kick(reason=f"Account age gate: {account_age.days} days old (min {MIN_ACCOUNT_AGE_DAYS})")
                print(f"Security: Kicked {member} — account only {account_age.days} days old.")
                log_security_event('AGE_GATE', member.id, str(member), f'Account {account_age.days} days old (min {MIN_ACCOUNT_AGE_DAYS})')
            except discord.Forbidden:
                print(f"Security: Cannot kick {member} (missing permissions).")
            return

    # ============================================================
    #  EVENT: on_message — Anti-Scam + Anti-Spam + Anti-Mention Spam
    # ============================================================
    @bot.event
    async def on_message(message):
        if message.author.bot:
            return

        if message.guild and isinstance(message.author, discord.Member):

            # --- Anti-Scam Link Detection ---
            content_lower = message.content.lower()
            scam_detected = False

            urls = URL_REGEX.findall(message.content)
            for domain in urls:
                domain = domain.lower()
                for scam in SCAM_DOMAINS:
                    if scam in domain:
                        scam_detected = True
                        break
                if scam_detected:
                    break

            if not scam_detected:
                for pattern in SCAM_PATTERNS:
                    if re.search(pattern, content_lower):
                        scam_detected = True
                        break

            if scam_detected:
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass
                print(f"Anti-Scam: Blocked scam from {message.author}: {message.content[:80]}")
                log_security_event('SCAM', message.author.id, str(message.author), f'Scam link detected: {message.content[:100]}')
                try:
                    await message.author.timeout(timedelta(minutes=SPAM_TIMEOUT_MINS), reason="Anti-Scam: Suspicious link detected")
                except discord.Forbidden:
                    print(f"Anti-Scam: Cannot timeout {message.author}.")
                await message.channel.send(
                    f"⚠️ {message.author.mention} — Your message was removed for containing a suspicious link.",
                    delete_after=10
                )
                return

            # --- Anti-Mention Spam ---
            if len(message.mentions) > MAX_MENTIONS_PER_MSG:
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass
                print(f"Anti-Mention: {message.author} pinged {len(message.mentions)} users.")
                log_security_event('MENTION_SPAM', message.author.id, str(message.author), f'Mass mention: {len(message.mentions)} pings')
                try:
                    await message.author.timeout(timedelta(minutes=SPAM_TIMEOUT_MINS), reason="Anti-Spam: Mass mention spam")
                except discord.Forbidden:
                    pass
                await message.channel.send(
                    f"⚠️ {message.author.mention} — Mass pinging is not allowed.",
                    delete_after=10
                )
                return

            # --- Anti-Spam: Character / Content Spam Detection ---
            content = message.content.strip()
            is_spam = False

            if len(content) > 0:
                if re.search(r"(.)\1{4,}", content):
                    is_spam = True
                words = content.lower().split()
                if len(words) >= 4 and len(set(words)) == 1:
                    is_spam = True
                if len(content) > 30 and content.count(" ") < 2 and not content.startswith("http"):
                    is_spam = True
                if len(content) > 10 and content.isupper():
                    is_spam = True

            if is_spam:
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass
                print(f"Anti-Spam: Detected spam content from {message.author}: {content[:50]}")
                log_security_event('SPAM', message.author.id, str(message.author), f'Spam content: {content[:100]}')
                try:
                    await message.author.timeout(timedelta(minutes=SPAM_TIMEOUT_MINS), reason="Anti-Spam: Spam content detected")
                except discord.Forbidden:
                    print(f"Anti-Spam: Cannot timeout {message.author}.")
                await message.channel.send(
                    f"🔇 {message.author.mention} — spam detected. Message removed.",
                    delete_after=10
                )
                return

            # --- Anti-Spam: Message Flood Detection ---
            now = time.time()
            uid = message.author.id

            if uid not in spam_tracker:
                spam_tracker[uid] = []

            spam_tracker[uid].append(now)
            spam_tracker[uid] = [t for t in spam_tracker[uid] if now - t <= SPAM_WINDOW]

            if len(spam_tracker[uid]) >= SPAM_THRESHOLD:
                spam_tracker[uid] = []
                print(f"Anti-Spam: Detected message flooding from {message.author}")
                log_security_event('FLOOD', message.author.id, str(message.author), 'Message flooding detected')
                try:
                    await message.author.timeout(timedelta(minutes=SPAM_TIMEOUT_MINS), reason="Anti-Spam: Message flooding")
                except discord.Forbidden:
                    print(f"Anti-Spam: Cannot timeout {message.author}.")
                await message.channel.send(
                    f"🔇 {message.author.mention} — slow down! Spamming detected.",
                    delete_after=10
                )
                return

        # --- Activity Log Channel Listener ---
        log_type = None
        if message.channel.id == ROBBERY_LOG_CHANNEL_ID:
            log_type = "robberies"
        elif message.channel.id == TICKET_LOG_CHANNEL_ID:
            log_type = "tickets"
        elif message.channel.id == ARREST_LOG_CHANNEL_ID:
            log_type = "arrests"
        elif message.channel.id == FTO_LOG_CHANNEL_ID:
            log_type = "fto"

        if log_type:
            c.execute("INSERT INTO activity_logs (user_id, log_type, amount, timestamp) VALUES (?, ?, 1.0, ?)",
                      (message.author.id, log_type, time.time()))
            conn.commit()
            await message.add_reaction("✅")

        await bot.process_commands(message)

    # ============================================================
    #  ANTI-NUKE EVENT HANDLERS
    # ============================================================
    @bot.event
    async def on_guild_channel_create(channel):
        cache_channel(channel)

    @bot.event
    async def on_guild_channel_update(before, after):
        cache_channel(after)

    @bot.event
    async def on_guild_channel_delete(channel):
        cached = channel_cache.pop(channel.id, None)
        if not cached:
            return

        guild = channel.guild
        print(f"Anti-Nuke: Restoring deleted channel #{cached['name']}")
        attacker = await get_audit_attacker(guild, discord.AuditLogAction.channel_delete, bot.user.id, channel.id)
        log_security_event('NUKE_CHANNEL', attacker.id if attacker else 0, str(attacker) if attacker else 'Unknown', f"Deleted channel #{cached['name']}")

        if attacker and attacker.id != guild.owner_id:
            await strip_roles_from_attacker(guild, attacker.id, f"Anti-Nuke: Deleted channel #{cached['name']}")

        overwrites = {}
        for target_id, ow in cached.get('overwrites', {}).items():
            target = guild.get_role(target_id) if ow['is_role'] else guild.get_member(target_id)
            if target:
                overwrites[target] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(ow['allow']),
                    discord.Permissions(ow['deny'])
                )

        cat_id = cached.get('category_id')
        if cat_id:
            cat_id = _category_id_map.get(cat_id, cat_id)
        category = guild.get_channel(cat_id) if cat_id else None

        try:
            if cached['kind'] == 'text':
                new_ch = await guild.create_text_channel(
                    name=cached['name'], category=category,
                    topic=cached.get('topic'), nsfw=cached.get('nsfw', False),
                    slowmode_delay=cached.get('slowmode_delay', 0),
                    overwrites=overwrites
                )
            elif cached['kind'] == 'voice':
                new_ch = await guild.create_voice_channel(
                    name=cached['name'], category=category,
                    bitrate=cached.get('bitrate', 64000),
                    user_limit=cached.get('user_limit', 0),
                    overwrites=overwrites
                )
            elif cached['kind'] == 'category':
                new_ch = await guild.create_category(
                    name=cached['name'], overwrites=overwrites
                )
                _category_id_map[channel.id] = new_ch.id
            else:
                return

            cache_channel(new_ch)
            print(f"Anti-Nuke: Channel #{cached['name']} restored successfully.")
        except discord.Forbidden:
            print(f"Anti-Nuke: Missing permissions to restore #{cached['name']}.")

    @bot.event
    async def on_guild_role_create(role):
        cache_role(role)

    @bot.event
    async def on_guild_role_update(before, after):
        cache_role(after)

    @bot.event
    async def on_guild_role_delete(role):
        cached = role_cache.pop(role.id, None)
        if not cached:
            return

        members_to_reassign = role_members.pop(role.id, set())
        guild = role.guild
        print(f"Anti-Nuke: Restoring deleted role @{cached['name']} ({len(members_to_reassign)} members)")
        attacker = await get_audit_attacker(guild, discord.AuditLogAction.role_delete, bot.user.id, role.id)
        log_security_event('NUKE_ROLE', attacker.id if attacker else 0, str(attacker) if attacker else 'Unknown', f"Deleted role @{cached['name']}")

        if attacker and attacker.id != guild.owner_id:
            await strip_roles_from_attacker(guild, attacker.id, f"Anti-Nuke: Deleted role @{cached['name']}")

        try:
            new_role = await guild.create_role(
                name=cached['name'], color=cached['color'],
                permissions=cached['permissions'],
                hoist=cached['hoist'], mentionable=cached['mentionable'],
                reason="Anti-Nuke: Role restoration"
            )
            cache_role(new_role)

            reassigned = 0
            for member_id in members_to_reassign:
                member = guild.get_member(member_id)
                if member:
                    try:
                        await member.add_roles(new_role, reason="Anti-Nuke: Role restoration")
                        reassigned += 1
                    except discord.Forbidden:
                        pass

            role_members[new_role.id] = members_to_reassign
            print(f"Anti-Nuke: Role @{cached['name']} restored. Reassigned to {reassigned} members.")
        except discord.Forbidden:
            print(f"Anti-Nuke: Missing permissions to restore @{cached['name']}.")

    # ============================================================
    #  ANTI-MASS BAN/KICK
    # ============================================================
    @bot.event
    async def on_member_ban(guild, user):
        attacker = await get_audit_attacker(guild, discord.AuditLogAction.ban, bot.user.id)
        if not attacker or attacker.id == bot.user.id:
            return

        now = time.time()
        uid = attacker.id

        if uid not in ban_tracker:
            ban_tracker[uid] = []
        ban_tracker[uid].append(now)
        ban_tracker[uid] = [t for t in ban_tracker[uid] if now - t <= MASS_ACTION_WINDOW]

        if len(ban_tracker[uid]) >= MASS_ACTION_THRESHOLD:
            ban_tracker[uid] = []
            print(f"🚨 ANTI-MASS-BAN: {attacker} banned {MASS_ACTION_THRESHOLD}+ members rapidly!")
            log_security_event('MASS_BAN', uid, str(attacker), f'Mass banning detected ({MASS_ACTION_THRESHOLD}+ bans)')
            await strip_roles_from_attacker(guild, uid, "Anti-Nuke: Mass banning detected")

            notify_ch = guild.system_channel or guild.text_channels[0]
            await notify_ch.send(
                f"🚨 **MASS BAN DETECTED** — {attacker.mention} has been stripped of all roles and timed out "
                f"for mass-banning members."
            )

    @bot.event
    async def on_member_remove(member):
        guild = member.guild
        attacker = await get_audit_attacker(guild, discord.AuditLogAction.kick, bot.user.id)
        if not attacker or attacker.id == bot.user.id:
            return

        now = time.time()
        uid = attacker.id

        if uid not in kick_tracker:
            kick_tracker[uid] = []
        kick_tracker[uid].append(now)
        kick_tracker[uid] = [t for t in kick_tracker[uid] if now - t <= MASS_ACTION_WINDOW]

        if len(kick_tracker[uid]) >= MASS_ACTION_THRESHOLD:
            kick_tracker[uid] = []
            print(f"🚨 ANTI-MASS-KICK: {attacker} kicked {MASS_ACTION_THRESHOLD}+ members rapidly!")
            log_security_event('MASS_KICK', uid, str(attacker), f'Mass kicking detected ({MASS_ACTION_THRESHOLD}+ kicks)')
            await strip_roles_from_attacker(guild, uid, "Anti-Nuke: Mass kicking detected")

            notify_ch = guild.system_channel or guild.text_channels[0]
            await notify_ch.send(
                f"🚨 **MASS KICK DETECTED** — {attacker.mention} has been stripped of all roles and timed out "
                f"for mass-kicking members."
            )

    # ============================================================
    #  ANTI-WEBHOOK SPAM
    # ============================================================
    @bot.event
    async def on_webhooks_update(channel):
        guild = channel.guild
        try:
            current_hooks = await guild.webhooks()
            current_ids = {wh.id for wh in current_hooks}
            old_ids = webhook_cache.get(guild.id, set())

            new_hooks = current_ids - old_ids
            if new_hooks:
                attacker = await get_audit_attacker(guild, discord.AuditLogAction.webhook_create, bot.user.id)
                if attacker and attacker.id != bot.user.id and attacker.id != guild.owner_id:
                    for wh in current_hooks:
                        if wh.id in new_hooks:
                            try:
                                await wh.delete(reason="Anti-Nuke: Unauthorized webhook")
                                print(f"Security: Deleted unauthorized webhook '{wh.name}' by {attacker}")
                            except discord.Forbidden:
                                pass
                    log_security_event('WEBHOOK', attacker.id, str(attacker), 'Unauthorized webhook creation')
                    await strip_roles_from_attacker(guild, attacker.id, "Anti-Nuke: Unauthorized webhook creation")

            webhook_cache[guild.id] = current_ids
        except discord.Forbidden:
            pass

    # ============================================================
    #  ROLE-MEMBER CACHE SYNC
    # ============================================================
    @bot.event
    async def on_member_update(before, after):
        removed = set(before.roles) - set(after.roles)
        added = set(after.roles) - set(before.roles)
        for role in removed:
            if role.id in role_members:
                role_members[role.id].discard(after.id)
        for role in added:
            if role.id not in role_members:
                role_members[role.id] = set()
            role_members[role.id].add(after.id)
