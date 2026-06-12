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
            moderator_id INTEGER
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS tempbans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            expiry_timestamp TEXT
        )''')
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
    
    # Determine which members are inactive
    for member in guild.members:
        if member.bot or member == guild.owner:
            continue
        if exclude_role_dot and exclude_role_dot in member.roles:
            continue
        if exclude_role_outsider and exclude_role_outsider in member.roles:
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
        await log_ch.send(
            f"user; <@{member.id}>\n"
            f"strike; {count}/3\n"
            f"reason; {reason}"
        )

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
    c.execute("SELECT id FROM strikes WHERE user_id = ? AND guild_id = ? ORDER BY id DESC LIMIT 1", (member.id, ctx.guild.id))
    row = c.fetchone()
    if not row:
        conn.close()
        await ctx.send("❌ No strikes found for this user.")
        return

    c.execute("DELETE FROM strikes WHERE id = ?", (row[0],))
    conn.commit()
    c.execute("SELECT COUNT(*) FROM strikes WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
    count = c.fetchone()[0]
    conn.close()

    await ctx.send(f"✅ Removed 1 strike from {member.mention}. Current strikes: {count}/3.")


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
@bot.command(name="mute", help="Timeout/mute a user for 1 hour. Usage: &&mute @user reason")
async def mute(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_staff(ctx.author):
        await ctx.send("❌ You need the STAFF role or higher to use this command.")
        return

    duration = datetime.timedelta(hours=1)
    try:
        await member.timeout(duration, reason=f"Muted by {ctx.author.name}: {reason}")
        await ctx.send(f"🔇 Muted {member.mention} for 1 hour. Reason: `{reason}`")

        log_ch = get_log_channel(ctx.guild, "punishment-logs")
        if log_ch:
            await log_ch.send(
                f"user; <@{member.id}>\n"
                f"action; mute (1 hour)\n"
                f"reason; {reason}"
            )
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to timeout.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Error: {str(e)}")


# Run the bot
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
