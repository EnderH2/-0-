import discord
from discord.ext import commands, tasks
import os
import sqlite3
import asyncio
import yt_dlp
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ==================== CONFIGURATION ====================
TOKEN = os.getenv('DISCORD_TOKEN')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))  # Your Discord user ID
MASTER_KEY = os.getenv('MASTER_KEY', 'default-key-change-this')  # Secret phrase

# ==================== DATABASE SETUP ====================
def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS config (
            id INTEGER PRIMARY KEY,
            guild_id TEXT,
            channel_id TEXT,
            is_connected INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_config(guild_id, channel_id):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('DELETE FROM config')
    c.execute('INSERT INTO config (guild_id, channel_id) VALUES (?, ?)',
              (str(guild_id), str(channel_id)))
    conn.commit()
    conn.close()

def load_config():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('SELECT guild_id, channel_id FROM config LIMIT 1')
    result = c.fetchone()
    conn.close()
    return result if result else (None, None)

# ==================== BOT SETUP ====================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Global variables
voice_client = None
my_presence = {
    'status': 'offline',
    'activities': [],
    'custom_status': None,
    'last_update': None
}
current_song = None
music_queue = []

# ==================== SECURITY ====================
def is_owner_and_authenticated():
    async def predicate(ctx):
        if ctx.guild is not None:
            return False
        if ctx.author.id != OWNER_ID:
            return False
        if not ctx.message.content.startswith(MASTER_KEY):
            return False
        return True
    return commands.check(predicate)

# ==================== PRESENCE MONITORING ====================
def parse_activities(activities):
    """Extract readable activity information"""
    result = []
    
    for activity in activities:
        if activity.type == discord.ActivityType.custom:
            my_presence['custom_status'] = activity.state if activity.state else None
            continue
            
        elif activity.type == discord.ActivityType.playing:
            if hasattr(activity, 'details') and activity.details:
                result.append(f"🎮 Playing {activity.name} - {activity.details}")
            else:
                result.append(f"🎮 Playing {activity.name}")
                
        elif activity.type == discord.ActivityType.listening:
            if activity.name == "Spotify" and hasattr(activity, 'title') and hasattr(activity, 'artist'):
                result.append(f"🎵 Listening to '{activity.title}' by {activity.artist}")
            else:
                result.append(f"🎵 Listening to {activity.name}")
                
        elif activity.type == discord.ActivityType.watching:
            result.append(f"📺 Watching {activity.name}")
            
        elif activity.type == discord.ActivityType.streaming:
            result.append(f"🔴 Streaming {activity.name}")
    
    return result

def format_bot_presence():
    """Format how the bot should appear based on your presence"""
    global my_presence
    
    status = my_presence['status']
    activities = my_presence['activities']
    custom = my_presence['custom_status']
    
    if custom:
        return status, f"Endroo: {custom}"
    
    if activities:
        combined = " • ".join(activities[:2])
        return status, f"Endroo {combined.lower()}"
    
    status_map = {
        'online': '🟢 Endroo is online',
        'idle': '🌙 Endroo is idle',
        'dnd': '🔴 Endroo is busy',
        'offline': '⚫ Endroo is offline'
    }
    return status, status_map.get(status, f"Endroo is {status}")

@tasks.loop(seconds=5)
async def monitor_presence():
    """Monitor your Discord presence every 5 seconds"""
    global my_presence
    
    try:
        for guild in bot.guilds:
            member = guild.get_member(OWNER_ID)
            if member:
                new_status = str(member.status)
                new_activities = parse_activities(member.activities)
                
                if (new_status != my_presence['status'] or 
                    new_activities != my_presence['activities']):
                    
                    my_presence['status'] = new_status
                    my_presence['activities'] = new_activities
                    my_presence['last_update'] = datetime.now()
                    
                    await update_bot_presence()
                break
    except Exception as e:
        print(f"Presence monitor error: {e}")

async def update_bot_presence():
    """Update the bot's presence based on your status"""
    status, message = format_bot_presence()
    
    discord_status = {
        'online': discord.Status.online,
        'idle': discord.Status.idle,
        'dnd': discord.Status.dnd,
        'offline': discord.Status.offline
    }.get(my_presence['status'], discord.Status.online)
    
    activity = discord.Game(name=message)
    await bot.change_presence(status=discord_status, activity=activity)

# ==================== MUSIC PLAYER FUNCTIONS ====================
ydl_opts = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'ignoreerrors': True,
    'nocheckcertificate': True,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

async def play_next(ctx):
    """Play the next song in queue"""
    global current_song, music_queue
    
    if not music_queue:
        current_song = None
        return
    
    if not voice_client or not voice_client.is_connected():
        return
    
    current_song = music_queue.pop(0)
    url = current_song['url']
    title = current_song['title']
    
    def after_playing(error):
        if error:
            print(f"Playback error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
    
    voice_client.play(
        discord.FFmpegPCMAudio(url, **ffmpeg_options),
        after=after_playing
    )
    
    # Send notification to owner DM
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(f"🎵 Now playing: **{title}**")
    except:
        pass

async def ensure_voice_connection(guild_id, channel_id):
    """Ensure bot stays connected, reconnect if disconnected"""
    global voice_client
    
    if voice_client and voice_client.is_connected():
        return True
    
    try:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            return False
        
        channel = guild.get_channel(int(channel_id))
        if not channel or not isinstance(channel, discord.VoiceChannel):
            return False
        
        voice_client = await channel.connect()
        return True
    except Exception as e:
        print(f"Voice connection error: {e}")
        return False

@tasks.loop(seconds=30)
async def connection_monitor():
    """Monitor voice connection every 30 seconds"""
    guild_id, channel_id = load_config()
    if not guild_id or not channel_id:
        return
    
    await ensure_voice_connection(guild_id, channel_id)

# ==================== BOT EVENTS ====================
@bot.event
async def on_ready():
    print(f'✅ (0); Bot is online as {bot.user}')
    print(f'👑 Monitoring user ID: {OWNER_ID}')
    print(f'🔑 Master Key configured')
    
    init_db()
    monitor_presence.start()
    connection_monitor.start()
    
    # Auto-reconnect to saved channel
    guild_id, channel_id = load_config()
    if guild_id and channel_id:
        await ensure_voice_connection(guild_id, channel_id)
    
    owner = await bot.fetch_user(OWNER_ID)
    if owner:
        await owner.send(f"🟢 **System Online**\n`(0);` is now active.\nUse `{MASTER_KEY} help` for commands.")

# ==================== COMMANDS ====================
@bot.command(name='help')
@is_owner_and_authenticated()
async def help_command(ctx):
    """Show available commands"""
    embed = discord.Embed(
        title="🎮 (0); Bot Control Panel",
        description="All commands start with your master key:",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="📊 Status Commands",
        value=f"`{MASTER_KEY} status` - Show your current presence\n"
              f"`{MASTER_KEY} custom [text]` - Set manual status",
        inline=False
    )
    
    embed.add_field(
        name="🔊 Voice Commands",
        value=f"`{MASTER_KEY} join [channel_id]` - Join voice channel\n"
              f"`{MASTER_KEY} leave` - Leave voice channel\n"
              f"`{MASTER_KEY} list` - List servers & channels",
        inline=False
    )
    
    embed.add_field(
        name="🎵 Music Commands",
        value=f"`{MASTER_KEY} play [song]` - Play a song\n"
              f"`{MASTER_KEY} pause` - Pause current song\n"
              f"`{MASTER_KEY} resume` - Resume paused song\n"
              f"`{MASTER_KEY} skip` - Skip current song\n"
              f"`{MASTER_KEY} queue` - Show music queue\n"
              f"`{MASTER_KEY} clear` - Clear the queue\n"
              f"`{MASTER_KEY} np` - Now playing",
        inline=False
    )
    
    embed.add_field(
        name="ℹ️ Info",
        value=f"`{MASTER_KEY} ping` - Check bot latency",
        inline=False
    )
    
    await ctx.send(embed=embed)

# ==================== STATUS COMMANDS ====================
@bot.command(name='status')
@is_owner_and_authenticated()
async def show_status(ctx):
    """Show your current presence"""
    embed = discord.Embed(
        title="📊 Your Current Presence",
        color=discord.Color.green()
    )
    
    status_map = {
        'online': '🟢 Online',
        'idle': '🌙 Idle',
        'dnd': '🔴 Do Not Disturb',
        'offline': '⚫ Offline'
    }
    embed.add_field(name="Status", value=status_map.get(my_presence['status'], my_presence['status']), inline=True)
    
    if my_presence['custom_status']:
        embed.add_field(name="Custom", value=my_presence['custom_status'], inline=True)
    
    if my_presence['activities']:
        activities_text = "\n".join(my_presence['activities'])
        embed.add_field(name="Activities", value=activities_text, inline=False)
    
    if current_song:
        embed.add_field(name="🎵 Now Playing", value=current_song['title'], inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='custom')
@is_owner_and_authenticated()
async def set_custom(ctx, *, text: str):
    """Set a custom status manually"""
    await bot.change_presence(activity=discord.Game(name=f"Endroo: {text}"))
    await ctx.send(f"✅ Custom status set to: `Endroo: {text}`")

# ==================== VOICE COMMANDS ====================
@bot.command(name='list')
@is_owner_and_authenticated()
async def list_servers(ctx):
    """List all servers and voice channels"""
    embed = discord.Embed(
        title="📋 Available Servers & Voice Channels",
        color=discord.Color.purple()
    )
    
    for guild in bot.guilds:
        voice_channels = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
        if voice_channels:
            channel_list = "\n".join([f"  • `{c.name}` (ID: `{c.id}`)" for c in voice_channels[:5]])
            if len(voice_channels) > 5:
                channel_list += f"\n  • *...and {len(voice_channels)-5} more*"
            embed.add_field(
                name=f"🏠 {guild.name} (ID: `{guild.id}`)",
                value=channel_list,
                inline=False
            )
    
    await ctx.send(embed=embed)

@bot.command(name='join')
@is_owner_and_authenticated()
async def join_channel(ctx, channel_id: str = None):
    """Join a voice channel by ID"""
    global voice_client
    
    if not channel_id:
        return await ctx.send("❌ Please provide a channel ID: `[key] join 123456789`")
    
    target_channel = None
    target_guild = None
    
    for guild in bot.guilds:
        channel = guild.get_channel(int(channel_id))
        if channel and isinstance(channel, discord.VoiceChannel):
            target_channel = channel
            target_guild = guild
            break
    
    if not target_channel:
        return await ctx.send("❌ Voice channel not found.")
    
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
    
    try:
        voice_client = await target_channel.connect()
        save_config(target_guild.id, target_channel.id)
        
        embed = discord.Embed(title="✅ Connected to Voice Channel", color=discord.Color.green())
        embed.add_field(name="Server", value=target_guild.name, inline=True)
        embed.add_field(name="Channel", value=target_channel.name, inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Failed to connect: {str(e)}")

@bot.command(name='leave')
@is_owner_and_authenticated()
async def leave_channel(ctx):
    """Leave current voice channel"""
    global voice_client, current_song, music_queue
    
    if voice_client and voice_client.is_connected():
        guild_name = voice_client.guild.name
        channel_name = voice_client.channel.name
        current_song = None
        music_queue = []
        await voice_client.disconnect()
        voice_client = None
        await ctx.send(f"👋 Left `{channel_name}` in `{guild_name}`")
    else:
        await ctx.send("❌ Not in a voice channel.")

# ==================== MUSIC COMMANDS ====================
@bot.command(name='play')
@is_owner_and_authenticated()
async def play(ctx, *, search: str):
    """Play a song from YouTube"""
    global voice_client, music_queue, current_song
    
    if not voice_client or not voice_client.is_connected():
        return await ctx.send("❌ Not connected to a voice channel. Use `join` first.")
    
    await ctx.send(f"🔍 Searching for: **{search}**...")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if search.startswith('http'):
                info = ydl.extract_info(search, download=False)
            else:
                info = ydl.extract_info(f"ytsearch:{search}", download=False)['entries'][0]
            
            url = info['url']
            title = info['title']
        
        song_data = {'url': url, 'title': title}
        
        if voice_client.is_playing():
            music_queue.append(song_data)
            await ctx.send(f"📝 Added to queue: **{title}** (Position: {len(music_queue)})")
        else:
            current_song = song_data
            
            def after_playing(error):
                if error:
                    print(f"Playback error: {error}")
                asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            
            voice_client.play(
                discord.FFmpegPCMAudio(url, **ffmpeg_options),
                after=after_playing
            )
            await ctx.send(f"🎵 Now playing: **{title}**")
            
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

@bot.command(name='pause')
@is_owner_and_authenticated()
async def pause(ctx):
    """Pause current song"""
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await ctx.send("⏸️ Paused")
    else:
        await ctx.send("❌ Nothing playing")

@bot.command(name='resume')
@is_owner_and_authenticated()
async def resume(ctx):
    """Resume paused song"""
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await ctx.send("▶️ Resumed")
    else:
        await ctx.send("❌ Nothing paused")

@bot.command(name='skip')
@is_owner_and_authenticated()
async def skip(ctx):
    """Skip current song"""
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await ctx.send("⏭️ Skipped")
    else:
        await ctx.send("❌ Nothing to skip")

@bot.command(name='queue')
@is_owner_and_authenticated()
async def show_queue(ctx):
    """Show the music queue"""
    if not music_queue:
        return await ctx.send("📭 Queue is empty")
    
    queue_text = "**Current Queue:**\n"
    for i, song in enumerate(music_queue[:10], 1):
        queue_text += f"{i}. {song['title']}\n"
    
    if len(music_queue) > 10:
        queue_text += f"... and {len(music_queue)-10} more"
    
    await ctx.send(queue_text)

@bot.command(name='clear')
@is_owner_and_authenticated()
async def clear_queue(ctx):
    """Clear the music queue"""
    global music_queue
    music_queue = []
    await ctx.send("🧹 Queue cleared")

@bot.command(name='np')
@is_owner_and_authenticated()
async def now_playing(ctx):
    """Show currently playing song"""
    if current_song:
        await ctx.send(f"🎵 Now playing: **{current_song['title']}**")
    else:
        await ctx.send("❌ Nothing playing")

# ==================== UTILITY COMMANDS ====================
@bot.command(name='ping')
@is_owner_and_authenticated()
async def ping(ctx):
    """Check bot latency"""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! `{latency}ms`")

# ==================== RUN BOT ====================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN not set in .env file!")
        exit(1)
    
    if OWNER_ID == 0:
        print("❌ OWNER_ID not set in .env file!")
        exit(1)
    
    if MASTER_KEY == "default-key-change-this":
        print("⚠️ WARNING: Using default master key. Change it in .env file!")
    
    bot.run(TOKEN)