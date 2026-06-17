import discord
from discord.ext import commands
import datetime
import os
import asyncio
import sqlite3
from dotenv import load_dotenv

# ---------------------------------------------------------
# BOT CONFIGURATION AND SETUP
# ---------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    load_dotenv()
    BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
    print("ERROR: Valid BOT_TOKEN not found. Set BOT_TOKEN as an environment variable.")
    exit(1)

# Enable required intents
# IMPORTANT: You must also enable these in the Discord Developer Portal
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

class PruneBot(commands.Bot):
    def __init__(self):
        # Prefix changed to &&
        super().__init__(command_prefix="&&", intents=intents)
        # Dictionary to store pending prune scans per guild
        self.pending_prunes = {}
        self.db_path = "bot_data.db"
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS strikes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            reason TEXT,
            timestamp TEXT,
            moderator_id INTEGER,
            log_channel_id INTEGER,
            log_message_id INTEGER
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS tempbans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            expiry_timestamp TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            reason TEXT,
            timestamp TEXT,
            moderator_id INTEGER
        )''')
        # Lightweight migration: add log columns to old strikes tables
        for col in ("log_channel_id", "log_message_id"):
            try:
                c.execute(f"ALTER TABLE strikes ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.close()

    async def setup_hook(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("Bot is ready and listening for && commands.")
        await self._check_expired_tempbans()

    async def _check_expired_tempbans(self):
        now = datetime.datetime.utcnow().isoformat()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT user_id, guild_id FROM tempbans WHERE expiry_timestamp <= ?", (now,))
        expired = c.fetchall()
        for user_id, guild_id in expired:
            guild = self.get_guild(guild_id)
            if guild:
                try:
                    await guild.unban(discord.Object(id=user_id), reason="Tempban expired")
                except:
                    pass
        c.execute("DELETE FROM tempbans WHERE expiry_timestamp <= ?", (now,))
        conn.commit()
        conn.close()

bot = PruneBot()

def get_role_by_name(guild, name):
    return discord.utils.get(guild.roles, name=name)

def has_role_or_higher(member, role_name):
    target = get_role_by_name(member.guild, role_name)
    if not target:
        return False
    for role in member.roles:
        if role.position >= target.position:
            return True
    return False

def is_staff(member):
    return has_role_or_higher(member, "STAFF")

def is_admin(member):
    return has_role_or_higher(member, "Admin")

def is_overseer(member):
    return has_role_or_higher(member, "Overseer")

def is_batman(member):
    return has_role_or_higher(member, "Batman")

def can_promote_target(author, target_role):
    manager_role = get_role_by_name(author.guild, "Manager | 2nd in command")
    batman_role = get_role_by_name(author.guild, "Batman")
    if not manager_role:
        return True
    if target_role.position > manager_role.position:
        if not batman_role:
            return False
        return has_role_or_higher(author, "Batman")
    return is_overseer(author)

def can_demote_target(author, target_role):
    return can_promote_target(author, target_role)

def get_log_channel(guild, name):
    return discord.utils.get(guild.text_channels, name=name)

def parse_duration(text):
    """Parse a duration string like '4h', '30m', '7d', '90s' into a timedelta. Returns None if invalid."""
    if not text:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = text[-1].lower()
    if unit not in units:
        return None
    try:
        amount = int(text[:-1])
    except ValueError:
        return None
    if amount <= 0:
        return None
    return datetime.timedelta(seconds=amount * units[unit])

# Recruitment Division rank ladder (lowest to highest)
RD_RANKS = [
    "Recruitment Prospect",
    "Recruitment Officer",
    "Senior Recruitment Officer",
    "Lead Recruiter",
    "Director of Recruitment",
]

def get_rd_rank_index(member):
    """Return the index of the member's highest RD rank, or -1 if none."""
    idx = -1
    for i, name in enumerate(RD_RANKS):
        role = get_role_by_name(member.guild, name)
        if role and role in member.roles:
            idx = i
    return idx

async def resolve_member(ctx, query):
    """Resolve a member from a mention, ID, or username. Works in guilds and DMs."""
    query = query.strip()
    # Strip mention formatting like <@123> or <@!123>
    if query.startswith("<@") and query.endswith(">"):
        query = query[2:-1].lstrip("!")

    # Candidate guilds: the command's guild, otherwise every guild the bot is in
    guilds = [ctx.guild] if ctx.guild else list(bot.guilds)

    # Try by ID first
    if query.isdigit():
        uid = int(query)
        for g in guilds:
            m = g.get_member(uid)
            if m:
                return m

    lowered = query.lower()
    # Exact name / display name / full tag match
    for g in guilds:
        for m in g.members:
            if lowered in (m.name.lower(), m.display_name.lower(), str(m).lower()):
                return m
    # Partial match fallback
    for g in guilds:
        for m in g.members:
            if lowered in m.name.lower() or lowered in m.display_name.lower():
                return m
    return None

# ---------------------------------------------------------
# COMMAND: &&inactivityprune
# ---------------------------------------------------------
@bot.command(name="inactivityprune", help="Scan for inactive members and show a preview before pruning.")
async def inactivityprune(ctx: commands.Context):
    # Send a status message because scanning takes time
    await ctx.send("Scanning message history for the last 5 days. This may take a moment...")
    
    guild = ctx.guild
    cutoff_days = 5
    cutoff_date = discord.utils.utcnow() - datetime.timedelta(days=cutoff_days)
    
    # Identify roles to exclude from the prune
    exclude_role_dot = discord.utils.get(guild.roles, name=".")
    exclude_role_outsider = discord.utils.get(guild.roles, name="OUTSIDER & UNRANKED")
    
    # Track the most recent message date for each member found in the scan
    last_message_dates = {}
    
    # Gather all text channels, voice channels (they have text chats), and threads
    channels_to_scan = []
    channels_to_scan.extend(guild.text_channels)
    channels_to_scan.extend(guild.voice_channels)
    
    # Collect threads from text channels and forum channels
    for channel in guild.text_channels + guild.forums:
        channels_to_scan.extend(channel.threads) # Active threads
        try:
            # Fetch archived threads
            async for thread in channel.archived_threads(limit=None):
                channels_to_scan.append(thread)
        except discord.Forbidden:
            pass 
            
    # Scan message history backwards up to the 5-day limit
    for channel in channels_to_scan:
        try:
            # Ensure the bot has permission to read message history in the channel
            if isinstance(channel, discord.Thread):
                parent = channel.parent
                if parent and not parent.permissions_for(guild.me).read_message_history:
                    continue
            else:
                if not channel.permissions_for(guild.me).read_message_history:
                    continue

            # Iterate through messages up to the cutoff_date
            async for message in channel.history(limit=None, after=cutoff_date):
                author_id = message.author.id
                if author_id not in last_message_dates or message.created_at > last_message_dates[author_id]:
                    last_message_dates[author_id] = message.created_at
        except (discord.Forbidden, discord.HTTPException):
            continue
            
    inactive_members = []
    preview_lines = []
    
    # Members who joined within the last 3 days are too new to count as inactive
    join_grace_cutoff = discord.utils.utcnow() - datetime.timedelta(days=3)

    # Determine which members are inactive
    for member in guild.members:
        if member.bot or member == guild.owner:
            continue
        if exclude_role_dot and exclude_role_dot in member.roles:
            continue
        if exclude_role_outsider and exclude_role_outsider in member.roles:
            continue
        # Skip members who joined in the last 3 days
        if member.joined_at and member.joined_at > join_grace_cutoff:
            continue
            
        last_msg_date = last_message_dates.get(member.id)
        
        # If no message was found in the 5-day scan, they are inactive.
        if not last_msg_date:
            inactive_members.append(member)
            preview_lines.append(f"{member.name} — No messages found")
            
    if not inactive_members:
        await ctx.send("No inactive members found! Everyone has been active in the last 5 days or is excluded.")
        return
        
    # Store the results in memory for the confirmation step
    bot.pending_prunes[guild.id] = {
        'members': inactive_members,
        'details': preview_lines
    }
    
    # Build the preview message
    response_msg = f"**Found {len(inactive_members)} inactive members:**\n\n"
    
    # Handle Discord's 2000 character limit gracefully
    joined_lines = "\n".join(preview_lines)
    if len(response_msg) + len(joined_lines) > 1900:
        response_msg += joined_lines[:1800] + "\n... (truncated due to length)"
    else:
        response_msg += joined_lines
        
    response_msg += "\n\nRun `&&confirmprune` to continue or `&&cancelprune` to cancel."
    
    await ctx.send(response_msg)


# ---------------------------------------------------------
# COMMAND: &&confirmprune
# ---------------------------------------------------------
@bot.command(name="confirmprune", help="Confirm and apply roles to the pending inactive members.")
async def confirmprune(ctx: commands.Context):
    guild = ctx.guild
    
    # Check if a preview exists in memory
    if guild.id not in bot.pending_prunes:
        await ctx.send("There is no pending prune. Run `&&inactivityprune` first.")
        return
        
    await ctx.send("Applying roles, please wait...")
    
    # Retrieve and clear the pending prune
    prune_data = bot.pending_prunes.pop(guild.id)
    members_to_prune = prune_data['members']
    
    # Locate the target role
    target_role = discord.utils.get(guild.roles, name="OUTSIDER & UNRANKED")
    if not target_role:
        await ctx.send("❌ Setup Error: The role `OUTSIDER & UNRANKED` does not exist in this server. Please create it and run the scan again.")
        return
        
    success_count = 0
    failures = []
    
    # Prepare log file entries
    log_entries = []
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entries.append(f"--- PRUNE OPERATION ---")
    log_entries.append(f"Date/Time: {now_str}")
    log_entries.append(f"Guild: {guild.name}")
    log_entries.append(f"Guild ID: {guild.id}")
    
    # Apply role changes
    for member in members_to_prune:
        try:
            await member.edit(roles=[target_role], reason="Inactivity Prune")
            success_count += 1
            log_entries.append(f"Pruned: {member.name} (ID: {member.id}) - Days inactive: >5 (No recent messages found)")
            await asyncio.sleep(1)
        except discord.Forbidden:
            failures.append(f"{member.name} (Missing permissions - bot role may be too low)")
        except discord.HTTPException as e:
            failures.append(f"{member.name} (HTTP Error: {str(e)})")
            
    log_entries.append(f"Operation finished. Success: {success_count}, Failures: {len(failures)}\n")
    
    # Append to log file
    with open("prune_log.txt", "a", encoding="utf-8") as f:
        f.write("\n".join(log_entries) + "\n")
        
    # Build the final response
    response = f"✅ Successfully pruned {success_count} members."
    if failures:
        response += f"\n❌ Failed to prune {len(failures)} members:\n"
        failures_str = "\n".join(failures)
        if len(failures_str) > 1000:
            failures_str = failures_str[:1000] + "\n... (truncated)"
        response += failures_str
        
    await ctx.send(response)


# ---------------------------------------------------------
# COMMAND: &&cancelprune
# ---------------------------------------------------------
@bot.command(name="cancelprune", help="Cancel a pending inactivity prune.")
async def cancelprune(ctx: commands.Context):
    guild = ctx.guild
    
    # Clear the stored preview results if they exist
    if guild.id in bot.pending_prunes:
        del bot.pending_prunes[guild.id]
        await ctx.send("✅ The pending prune has been cancelled. You will need to run a new scan before pruning.")
    else:
        await ctx.send("There is no pending prune to cancel.")

# ---------------------------------------------------------
# COMMAND: &&promo
# ---------------------------------------------------------
@bot.command(name="promo", help="Promote a user to the next higher role. Usage: &&promo @user reason")
async def promo(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_overseer(ctx.author):
        await ctx.send("❌ You need the Overseer role or higher to use this command.")
        return

    member_roles = [r for r in member.roles if r.name != "@everyone"]
    if not member_roles:
        await ctx.send("❌ User has no assignable roles.")
        return

    current_highest = max(member_roles, key=lambda r: r.position)
    next_role = None
    for role in ctx.guild.roles:
        if role.position > current_highest.position and role.name != "@everyone":
            if next_role is None or role.position < next_role.position:
                next_role = role

    if not next_role:
        await ctx.send("❌ No higher role found to promote to.")
        return

    if not can_promote_target(ctx.author, next_role):
        await ctx.send("❌ You do not have permission to promote to that role.")
        return

    new_roles = [r for r in member.roles if r != current_highest]
    if next_role not in new_roles:
        new_roles.append(next_role)

    try:
        await member.edit(roles=new_roles, reason=f"Promoted by {ctx.author.name}: {reason}")
        await ctx.send(f"✅ Promoted {member.mention} from `{current_highest.name}` to `{next_role.name}`.")

        log_ch = get_log_channel(ctx.guild, "promo-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"promotion; {current_highest.name} / {next_role.name}\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to edit roles.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# ---------------------------------------------------------
# COMMAND: &&demote
# ---------------------------------------------------------
@bot.command(name="demote", help="Demote a user to the next lower role. Usage: &&demote @user reason")
async def demote(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_overseer(ctx.author):
        await ctx.send("❌ You need the Overseer role or higher to use this command.")
        return

    member_roles = [r for r in member.roles if r.name != "@everyone"]
    if not member_roles:
        await ctx.send("❌ User has no assignable roles.")
        return

    current_highest = max(member_roles, key=lambda r: r.position)
    next_lower = None
    for role in ctx.guild.roles:
        if role.position < current_highest.position and role.name != "@everyone":
            if next_lower is None or role.position > next_lower.position:
                next_lower = role

    if not next_lower:
        await ctx.send("❌ No lower role found to demote to.")
        return

    if not can_demote_target(ctx.author, current_highest):
        await ctx.send("❌ You do not have permission to demote from that role.")
        return

    new_roles = [r for r in member.roles if r != current_highest]
    if next_lower not in new_roles:
        new_roles.append(next_lower)

    try:
        await member.edit(roles=new_roles, reason=f"Demoted by {ctx.author.name}: {reason}")
        await ctx.send(f"✅ Demoted {member.mention} from `{current_highest.name}` to `{next_lower.name}`.")

        log_ch = get_log_channel(ctx.guild, "punishment-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"demotion; {current_highest.name} / {next_lower.name}\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to edit roles.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# ---------------------------------------------------------
# COMMAND: &&strike
# ---------------------------------------------------------
@bot.command(name="strike", help="Issue a strike to a user. 3 strikes = 7 day tempban.")
async def strike(ctx: commands.Context, member: discord.Member, *, reason: str):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role to use this command.")
        return

    now = datetime.datetime.utcnow().isoformat()
    conn = sqlite3.connect(bot.db_path)
    c = conn.cursor()
    c.execute("INSERT INTO strikes (user_id, guild_id, reason, timestamp, moderator_id) VALUES (?, ?, ?, ?, ?)",
              (member.id, ctx.guild.id, reason, now, ctx.author.id))
    conn.commit()
    strike_id = c.lastrowid
    c.execute("SELECT COUNT(*) FROM strikes WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
    count = c.fetchone()[0]
    conn.close()

    try:
        owner = ctx.guild.owner
        if owner:
            await owner.send(f"🚨 **Strike Issued**\nUser: {member.mention} ({member.name})\nReason: {reason}\nModerator: {ctx.author.name}\nStrike count: {count}/3")
    except discord.Forbidden:
        pass

    log_ch = get_log_channel(ctx.guild, "punishment-logs")
    if log_ch:
        log_msg = await log_ch.send(
            f"user; <@{member.id}>\n"
            f"strike; {count}/3\n"
            f"reason; {reason}"
        )
        # Save the log message so &&removestrike can reply "Reverted" to it
        conn = sqlite3.connect(bot.db_path)
        c = conn.cursor()
        c.execute("UPDATE strikes SET log_channel_id = ?, log_message_id = ? WHERE id = ?",
                  (log_ch.id, log_msg.id, strike_id))
        conn.commit()
        conn.close()

    if count >= 3:
        expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=7)).isoformat()
        conn = sqlite3.connect(bot.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO tempbans (user_id, guild_id, expiry_timestamp) VALUES (?, ?, ?)",
                  (member.id, ctx.guild.id, expiry))
        conn.commit()
        conn.close()

        try:
            await member.ban(reason="3 strikes - 7 day tempban")
            await ctx.send(f"🔨 {member.mention} has received **3 strikes** and has been tempbanned for 7 days.")

            if log_ch:
                await log_ch.send(
                    f"user; <@{member.id}>\n"
                    f"action; tempban (7 days)\n"
                    f"reason; 3 strikes reached"
                )

            async def unban_task(guild, user_id):
                await asyncio.sleep(7 * 24 * 60 * 60)
                try:
                    await guild.unban(discord.Object(id=user_id), reason="Tempban expired")
                except:
                    pass
                conn = sqlite3.connect(bot.db_path)
                c = conn.cursor()
                c.execute("DELETE FROM tempbans WHERE user_id = ? AND guild_id = ?", (user_id, guild.id))
                conn.commit()
                conn.close()

            asyncio.create_task(unban_task(ctx.guild, member.id))
        except discord.Forbidden:
            await ctx.send("❌ Missing permissions to ban.")
        except discord.HTTPException as e:
            await ctx.send(f"❌ Error banning: {str(e)}")
    else:
        await ctx.send(f"⚠️ Strike issued to {member.mention}. Reason: `{reason}`. Strike {count}/3.")


# ---------------------------------------------------------
# COMMAND: &&removestrike
# ---------------------------------------------------------
@bot.command(name="removestrike", help="Remove the most recent strike from a user.")
async def removestrike(ctx: commands.Context, member: discord.Member):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role to use this command.")
        return

    conn = sqlite3.connect(bot.db_path)
    c = conn.cursor()
    c.execute("SELECT id, reason, log_channel_id, log_message_id FROM strikes WHERE user_id = ? AND guild_id = ? ORDER BY id DESC LIMIT 1", (member.id, ctx.guild.id))
    row = c.fetchone()
    if not row:
        conn.close()
        await ctx.send("❌ No strikes found for this user.")
        return

    strike_id, reason, log_channel_id, log_message_id = row
    c.execute("DELETE FROM strikes WHERE id = ?", (strike_id,))
    conn.commit()
    c.execute("SELECT COUNT(*) FROM strikes WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
    count = c.fetchone()[0]
    conn.close()

    await ctx.send(f"✅ Removed 1 strike from {member.mention}. Current strikes: {count}/3.")

    # Reply "Reverted" to the original strike log message
    if log_channel_id and log_message_id:
        try:
            log_ch = ctx.guild.get_channel(log_channel_id)
            if log_ch:
                original = await log_ch.fetch_message(log_message_id)
                await original.reply("Reverted")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # DM the server owner
    try:
        owner = ctx.guild.owner
        if owner:
            await owner.send(
                f"♻️ **Strike Reverted**\nUser: {member.mention} ({member.name})\n"
                f"Reverted reason: {reason}\nModerator: {ctx.author.name}\nStrike count now: {count}/3"
            )
    except discord.Forbidden:
        pass


# ---------------------------------------------------------
# COMMAND: &&ban
# ---------------------------------------------------------
@bot.command(name="ban", help="Ban a user. Usage: &&ban @user reason")
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_overseer(ctx.author):
        await ctx.send("❌ You need the Overseer role or higher to use this command.")
        return

    try:
        await member.ban(reason=f"Banned by {ctx.author.name}: {reason}")
        await ctx.send(f"🔨 Banned {member.mention}. Reason: `{reason}`")

        log_ch = get_log_channel(ctx.guild, "punishment-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"action; ban\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to ban.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# ---------------------------------------------------------
# COMMAND: &&kick
# ---------------------------------------------------------
@bot.command(name="kick", help="Kick a user. Usage: &&kick @user reason")
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_admin(ctx.author):
        await ctx.send("❌ You need the Admin role or higher to use this command.")
        return

    try:
        await member.kick(reason=f"Kicked by {ctx.author.name}: {reason}")
        await ctx.send(f"👢 Kicked {member.mention}. Reason: `{reason}`")

        log_ch = get_log_channel(ctx.guild, "punishment-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"action; kick\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to kick.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# ---------------------------------------------------------
# COMMAND: &&mute
# ---------------------------------------------------------
@bot.command(name="mute", help="Timeout a user for a duration. Usage: &&mute @user 4h spamming")
async def mute(ctx: commands.Context, member: discord.Member, duration: str = "1h", *, reason: str = "No reason provided"):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return

    delta = parse_duration(duration)
    if delta is None:
        # Treat the would-be duration as part of the reason and default to 1 hour
        reason = (duration + " " + reason).strip() if reason != "No reason provided" else duration
        delta = datetime.timedelta(hours=1)
        duration_label = "1h"
    else:
        duration_label = duration

    if delta > datetime.timedelta(days=28):
        await ctx.send("❌ Discord timeouts cannot exceed 28 days.")
        return

    try:
        await member.timeout(delta, reason=f"Muted by {ctx.author.name}: {reason}")
        await ctx.send(f"🔇 Muted {member.mention} for `{duration_label}`. Reason: `{reason}`")

        log_ch = get_log_channel(ctx.guild, "punishment-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"action; mute ({duration_label})\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to timeout.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# ---------------------------------------------------------
# COMMAND: &&unmute
# ---------------------------------------------------------
@bot.command(name="unmute", help="Remove a timeout from a user. Usage: &&unmute @user")
async def unmute(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return

    try:
        await member.timeout(None, reason=f"Unmuted by {ctx.author.name}: {reason}")
        await ctx.send(f"🔊 Unmuted {member.mention}.")

        log_ch = get_log_channel(ctx.guild, "punishment-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"action; unmute\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to remove timeout.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# ---------------------------------------------------------
# COMMAND: &&unban
# ---------------------------------------------------------
@bot.command(name="unban", help="Unban a user by ID. Usage: &&unban <user_id>")
async def unban(ctx: commands.Context, user_id: int, *, reason: str = "No reason provided"):
    if not is_overseer(ctx.author):
        await ctx.send("❌ You need the Overseer role or higher to use this command.")
        return

    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"Unbanned by {ctx.author.name}: {reason}")
        await ctx.send(f"✅ Unbanned `{user}`.")

        log_ch = get_log_channel(ctx.guild, "punishment-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{user_id}>\n"
                f"action; unban\n"
                f"reason; {reason}"
            )
    except discord.NotFound:
        await ctx.send("❌ That user is not banned or does not exist.")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to unban.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# ---------------------------------------------------------
# COMMAND: &&warn / &&warnings / &&clearwarnings
# ---------------------------------------------------------
@bot.command(name="warn", help="Warn a user. Usage: &&warn @user reason")
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return

    now = datetime.datetime.utcnow().isoformat()
    conn = sqlite3.connect(bot.db_path)
    c = conn.cursor()
    c.execute("INSERT INTO warnings (user_id, guild_id, reason, timestamp, moderator_id) VALUES (?, ?, ?, ?, ?)",
              (member.id, ctx.guild.id, reason, now, ctx.author.id))
    conn.commit()
    c.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
    count = c.fetchone()[0]
    conn.close()

    await ctx.send(f"⚠️ Warned {member.mention}. Reason: `{reason}`. Total warnings: {count}.")
    try:
        await member.send(f"You have been warned in **{ctx.guild.name}**.\nReason: {reason}")
    except discord.Forbidden:
        pass

    log_ch = get_log_channel(ctx.guild, "punishment-logs")
    if log_ch:
        await log_ch.send(
            f"user; <@{member.id}>\n"
            f"action; warn (#{count})\n"
            f"reason; {reason}"
        )


@bot.command(name="warnings", help="List a user's warnings. Usage: &&warnings @user")
async def warnings(ctx: commands.Context, member: discord.Member):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return

    conn = sqlite3.connect(bot.db_path)
    c = conn.cursor()
    c.execute("SELECT reason, timestamp FROM warnings WHERE user_id = ? AND guild_id = ? ORDER BY id ASC", (member.id, ctx.guild.id))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await ctx.send(f"{member.mention} has no warnings.")
        return

    lines = [f"**Warnings for {member.name}:**"]
    for i, (reason, ts) in enumerate(rows, start=1):
        lines.append(f"{i}. {reason} — `{ts[:10]}`")
    await ctx.send("\n".join(lines)[:1900])


@bot.command(name="clearwarnings", help="Clear all warnings for a user. Usage: &&clearwarnings @user")
async def clearwarnings(ctx: commands.Context, member: discord.Member):
    if not is_admin(ctx.author):
        await ctx.send("❌ You need the Admin role or higher to use this command.")
        return

    conn = sqlite3.connect(bot.db_path)
    c = conn.cursor()
    c.execute("DELETE FROM warnings WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
    conn.commit()
    conn.close()
    await ctx.send(f"✅ Cleared all warnings for {member.mention}.")


# ---------------------------------------------------------
# COMMAND: &&purge / &&clear
# ---------------------------------------------------------
@bot.command(name="purge", aliases=["clear"], help="Bulk delete messages. Usage: &&purge 20")
async def purge(ctx: commands.Context, amount: int):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return
    if amount < 1 or amount > 1000:
        await ctx.send("❌ Provide a number between 1 and 1000.")
        return
    try:
        deleted = await ctx.channel.purge(limit=amount + 1)
        msg = await ctx.send(f"🧹 Deleted {len(deleted) - 1} messages.")
        await asyncio.sleep(3)
        await msg.delete()
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to manage messages.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# ---------------------------------------------------------
# COMMAND: &&slowmode
# ---------------------------------------------------------
@bot.command(name="slowmode", help="Set channel slowmode in seconds. Usage: &&slowmode 10")
async def slowmode(ctx: commands.Context, seconds: int):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return
    if seconds < 0 or seconds > 21600:
        await ctx.send("❌ Slowmode must be between 0 and 21600 seconds.")
        return
    try:
        await ctx.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await ctx.send("✅ Slowmode disabled.")
        else:
            await ctx.send(f"🐌 Slowmode set to {seconds} seconds.")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to edit the channel.")


# ---------------------------------------------------------
# COMMAND: &&lock / &&unlock
# ---------------------------------------------------------
@bot.command(name="lock", help="Lock the current channel. Usage: &&lock")
async def lock(ctx: commands.Context):
    if not is_admin(ctx.author):
        await ctx.send("❌ You need the Admin role or higher to use this command.")
        return
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send("🔒 Channel locked.")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to edit the channel.")


@bot.command(name="unlock", help="Unlock the current channel. Usage: &&unlock")
async def unlock(ctx: commands.Context):
    if not is_admin(ctx.author):
        await ctx.send("❌ You need the Admin role or higher to use this command.")
        return
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send("🔓 Channel unlocked.")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to edit the channel.")


# ---------------------------------------------------------
# COMMAND: &&nick
# ---------------------------------------------------------
@bot.command(name="nick", help="Change a user's nickname. Usage: &&nick @user new name")
async def nick(ctx: commands.Context, member: discord.Member, *, nickname: str = None):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return
    try:
        await member.edit(nick=nickname)
        if nickname:
            await ctx.send(f"✅ Changed {member.mention}'s nickname to `{nickname}`.")
        else:
            await ctx.send(f"✅ Reset {member.mention}'s nickname.")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to change that nickname.")


# ---------------------------------------------------------
# COMMAND: &&role (add/remove a role)
# ---------------------------------------------------------
@bot.command(name="role", help="Toggle a role on a user. Usage: &&role @user RoleName")
async def role(ctx: commands.Context, member: discord.Member, *, role_name: str):
    if not is_admin(ctx.author):
        await ctx.send("❌ You need the Admin role or higher to use this command.")
        return
    target = get_role_by_name(ctx.guild, role_name)
    if not target:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    try:
        if target in member.roles:
            await member.remove_roles(target, reason=f"By {ctx.author.name}")
            await ctx.send(f"✅ Removed `{target.name}` from {member.mention}.")
        else:
            await member.add_roles(target, reason=f"By {ctx.author.name}")
            await ctx.send(f"✅ Added `{target.name}` to {member.mention}.")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions or role hierarchy too low.")


# ---------------------------------------------------------
# COMMAND: &&say / &&announce
# ---------------------------------------------------------
@bot.command(name="say", help="Make the bot say something. Usage: &&say message")
async def say(ctx: commands.Context, *, message: str):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    await ctx.send(message)


@bot.command(name="announce", help="Send an announcement embed. Usage: &&announce message")
async def announce(ctx: commands.Context, *, message: str):
    if not is_admin(ctx.author):
        await ctx.send("❌ You need the Admin role or higher to use this command.")
        return
    embed = discord.Embed(description=message, color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.set_author(name=f"Announcement from {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ---------------------------------------------------------
# COMMAND: &&userinfo / &&serverinfo / &&avatar
# ---------------------------------------------------------
@bot.command(name="userinfo", aliases=["whois"], help="Show info about a user. Usage: &&userinfo @user")
async def userinfo(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    embed = discord.Embed(title=f"User Info — {member}", color=member.color, timestamp=discord.utils.utcnow())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=False)
    embed.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles) if roles else "None", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="serverinfo", help="Show info about the server. Usage: &&serverinfo")
async def serverinfo(ctx: commands.Context):
    guild = ctx.guild
    embed = discord.Embed(title=f"Server Info — {guild.name}", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=str(guild.owner), inline=True)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Roles", value=len(guild.roles), inline=True)
    embed.add_field(name="Channels", value=len(guild.channels), inline=True)
    embed.add_field(name="Created", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="ID", value=guild.id, inline=True)
    await ctx.send(embed=embed)


@bot.command(name="avatar", aliases=["av"], help="Show a user's avatar. Usage: &&avatar @user")
async def avatar(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"{member.display_name}'s avatar", color=member.color)
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="ping", help="Check the bot's latency.")
async def ping(ctx: commands.Context):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")


# ---------------------------------------------------------
# RECRUITMENT DIVISION COMMANDS
# ---------------------------------------------------------
RDA_MESSAGE = (
    "Congratulations! Your application has been accepted.\n\n"
    "You have been assigned the Recruitment Prospect rank and are now a member of the Secret Recruitment Division.\n\n"
    "To be promoted to Recruitment Officer, you must:\n\n"
    "• Complete your 3-day trial period\n"
    "• Recruit 3 verified members\n\n"
    "Please review the Recruitment Handbook and Recruitment SOP before beginning.\n\n"
    "Good luck."
)

RDF_MESSAGE = (
    "Thank you for applying to the Secret Recruitment Division.\n\n"
    "After reviewing your application, we have decided not to move forward with it at this time.\n\n"
    "This is not necessarily a permanent denial. You may reapply in the future as activity and department needs change.\n\n"
    "Thank you for your interest in helping Secret grow."
)

RD_APPLICATION_FORM = (
    "# Secret Recruitment Division Application\n\n"
    "Thank you for your interest in joining the Secret Recruitment Division. Please answer all questions honestly and with effort.\n\n"
    "**1. Roblox Username**\n\n"
    "**2. Discord Username**\n\n"
    "**3. How active are you on Roblox and Discord?**\n\n"
    "**4. Why do you want to join the Recruitment Division?**\n\n"
    "**5. If accepted, how would you recruit active players to Secret? Be specific.**\n\n"
    "**6. Have you ever recruited, staffed, moderated, or helped grow a community before? If so, explain.**\n\n"
    "**7. What makes a recruit valuable to Secret besides simply joining the group?**\n\n"
    "**8. If another recruiter was submitting fake recruits to gain rewards, what would you do?**\n\n"
    "**9. How many active recruits do you realistically believe you could bring to Secret each week?**\n\n"
    "**10. Why should we choose you over other applicants?**\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "By submitting this application, you confirm that:\n\n"
    "• You understand that fake recruit submissions will result in removal from the Recruitment Division.\n"
    "• You understand that recruit quality is more important than recruit quantity.\n"
    "• You understand that all recruits must be verified before they count toward rewards or promotions.\n"
    "• You understand that Recruitment Prospects must complete a 3-day trial period and recruit 3 verified members before promotion.\n\n"
    "Reply to this message with your completed application."
)


@bot.command(name="RDApply", help="Apply to the Recruitment Division. The bot will DM you the form.")
async def rdapply(ctx: commands.Context):
    # Determine which guild's owner should receive the application
    guild = ctx.guild
    if guild is None:
        mutual = [g for g in bot.guilds if g.get_member(ctx.author.id)]
        guild = mutual[0] if mutual else None

    try:
        dm = await ctx.author.create_dm()
        await dm.send(RD_APPLICATION_FORM)
    except discord.Forbidden:
        await ctx.send("❌ I couldn't DM you. Enable **Direct Messages** from server members and try again.")
        return

    if ctx.guild:
        await ctx.send(f"📨 {ctx.author.mention}, check your DMs for the application form!")

    def check(m):
        return m.author.id == ctx.author.id and isinstance(m.channel, discord.DMChannel)

    try:
        reply = await bot.wait_for("message", check=check, timeout=86400)  # 24 hours
    except asyncio.TimeoutError:
        try:
            await dm.send("⏰ Your application timed out. Run `&&RDApply` again whenever you're ready.")
        except discord.Forbidden:
            pass
        return

    owner = guild.owner if guild else None
    if not owner:
        await dm.send("⚠️ Application received, but I couldn't locate the server owner to forward it to.")
        return

    header = (
        f"📥 **New RD Application**\n"
        f"Applicant: {ctx.author.mention} ({ctx.author} | `{ctx.author.id}`)\n"
        f"Accept: `&&RDA {ctx.author.id}`   |   Deny: `&&RDF {ctx.author.id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    body = reply.content if reply.content else "(no text submitted)"
    full = header + body

    try:
        # Split into <=1900 char chunks to respect Discord's message limit
        for i in range(0, len(full), 1900):
            await owner.send(full[i:i + 1900])
        await dm.send("✅ Your application has been submitted! You'll be notified of the decision.")
    except discord.Forbidden:
        await dm.send("⚠️ Application received, but I couldn't DM the server owner.")


async def _check_rd_permission(ctx, member):
    """Confirm the command author is STAFF+ in the target member's guild. Works in DMs."""
    author_member = member.guild.get_member(ctx.author.id)
    if not author_member or not is_staff(author_member):
        await ctx.send("❌ You need the STAFF role or higher (in the server) to use this command.")
        return False
    return True


@bot.command(name="RDA", help="Accept a recruitment applicant. Usage: &&RDA @user OR &&RDA username")
async def rda(ctx: commands.Context, *, query: str):
    member = await resolve_member(ctx, query)
    if not member:
        await ctx.send("❌ Could not find that user. Try using their ID.")
        return
    if not await _check_rd_permission(ctx, member):
        return

    prospect_role = get_role_by_name(member.guild, "Recruitment Prospect")
    if not prospect_role:
        await ctx.send("❌ The `Recruitment Prospect` role does not exist.")
        return

    try:
        await member.add_roles(prospect_role, reason=f"RD accepted by {ctx.author.name}")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to assign the Recruitment Prospect role.")
        return

    dm_ok = True
    try:
        await member.send(RDA_MESSAGE)
    except discord.Forbidden:
        dm_ok = False

    note = "" if dm_ok else " (could not DM the user — they may have DMs disabled)"
    await ctx.send(f"✅ {member} accepted and given `Recruitment Prospect`.{note}")


@bot.command(name="RDF", help="Deny a recruitment applicant. Usage: &&RDF @user OR &&RDF username")
async def rdf(ctx: commands.Context, *, query: str):
    member = await resolve_member(ctx, query)
    if not member:
        await ctx.send("❌ Could not find that user. Try using their ID.")
        return
    if not await _check_rd_permission(ctx, member):
        return

    dm_ok = True
    try:
        await member.send(RDF_MESSAGE)
    except discord.Forbidden:
        dm_ok = False

    if dm_ok:
        await ctx.send(f"✅ {member} has been sent a denial notice.")
    else:
        await ctx.send(f"⚠️ Could not DM {member} (they may have DMs disabled).")


@bot.command(name="RDPromo", help="Promote within the Recruitment Division. Usage: &&RDPromo @user")
async def rdpromo(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return

    idx = get_rd_rank_index(member)
    if idx == -1:
        await ctx.send("❌ That user is not in the Recruitment Division (no RD rank).")
        return
    if idx >= len(RD_RANKS) - 1:
        await ctx.send("❌ That user is already at the highest RD rank (Director of Recruitment).")
        return

    # Find the next existing rank above the current one
    next_idx = None
    for i in range(idx + 1, len(RD_RANKS)):
        if get_role_by_name(ctx.guild, RD_RANKS[i]):
            next_idx = i
            break
    if next_idx is None:
        await ctx.send("❌ No higher RD rank role exists in this server.")
        return

    old_role = get_role_by_name(ctx.guild, RD_RANKS[idx])
    new_role = get_role_by_name(ctx.guild, RD_RANKS[next_idx])
    try:
        if old_role:
            await member.remove_roles(old_role, reason=f"RD promo by {ctx.author.name}")
        await member.add_roles(new_role, reason=f"RD promo by {ctx.author.name}: {reason}")
        await ctx.send(f"✅ Promoted {member.mention} from `{RD_RANKS[idx]}` to `{RD_RANKS[next_idx]}`.")

        log_ch = get_log_channel(ctx.guild, "promo-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"RD promotion; {RD_RANKS[idx]} / {RD_RANKS[next_idx]}\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions or role hierarchy too low.")


@bot.command(name="RDDemote", help="Demote within the Recruitment Division. Usage: &&RDDemote @user")
async def rddemote(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return

    idx = get_rd_rank_index(member)
    if idx == -1:
        await ctx.send("❌ That user is not in the Recruitment Division (no RD rank).")
        return
    if idx == 0:
        await ctx.send("❌ That user is already at the lowest RD rank (Recruitment Prospect).")
        return

    # Find the next existing rank below the current one
    prev_idx = None
    for i in range(idx - 1, -1, -1):
        if get_role_by_name(ctx.guild, RD_RANKS[i]):
            prev_idx = i
            break
    if prev_idx is None:
        await ctx.send("❌ No lower RD rank role exists in this server.")
        return

    old_role = get_role_by_name(ctx.guild, RD_RANKS[idx])
    new_role = get_role_by_name(ctx.guild, RD_RANKS[prev_idx])
    try:
        if old_role:
            await member.remove_roles(old_role, reason=f"RD demote by {ctx.author.name}")
        await member.add_roles(new_role, reason=f"RD demote by {ctx.author.name}: {reason}")
        await ctx.send(f"✅ Demoted {member.mention} from `{RD_RANKS[idx]}` to `{RD_RANKS[prev_idx]}`.")

        log_ch = get_log_channel(ctx.guild, "punishment-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"RD demotion; {RD_RANKS[idx]} / {RD_RANKS[prev_idx]}\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions or role hierarchy too low.")


# ---------------------------------------------------------
# ERROR HANDLER
# ---------------------------------------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Check `&&help {ctx.command}`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument. Make sure you mentioned a valid user/value.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Could not find that member.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"❌ Error: {str(error)}")


# Run the bot
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
