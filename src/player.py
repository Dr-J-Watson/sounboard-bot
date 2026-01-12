import discord
import asyncio
import logging
from collections import deque
from typing import Optional, Dict

logger = logging.getLogger(__name__)

class GuildPlayer:
    def __init__(self, guild_id: int, bot, voice_timeout: int):
        self.guild_id = guild_id
        self.bot = bot
        self.queue = deque()
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_sound = None

    async def join(self, channel: discord.VoiceChannel):
        if self.voice_client is None or not self.voice_client.is_connected():
            self.voice_client = await channel.connect()
        elif self.voice_client.channel.id != channel.id:
            await self.voice_client.move_to(channel)

    async def disconnect(self):
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
        self.voice_client = None

    def add_to_queue(self, source_path: str, requester_name: str, sound_name: str, channel: discord.VoiceChannel):
        self.queue.append((source_path, requester_name, sound_name, channel))
        if not self.current_sound:
            asyncio.run_coroutine_threadsafe(self.process_next(), self.bot.loop)

    async def process_next(self):
        if not self.queue:
            self.current_sound = None
            return

        if self.voice_client and self.voice_client.is_playing():
            return

        source_path, requester_name, sound_name, channel = self.queue[0]

        try:
            if self.voice_client is None or not self.voice_client.is_connected():
                self.voice_client = await channel.connect()
            elif self.voice_client.channel.id != channel.id:
                await self.voice_client.move_to(channel)
        except Exception as e:
            logger.error(f"Erreur de connexion vocal: {e}")
            self.queue.popleft() # Remove failed item
            await self.process_next() # Try next
            return

        # Remove from queue only after successful connection/move
        self.queue.popleft()
        self.current_sound = (sound_name, requester_name)

        logger.info(f"Lecture de {sound_name} dans {channel.name}")

        try:
            source = discord.FFmpegPCMAudio(source_path)
            self.voice_client.play(source, after=lambda e: self.after_play(e))
        except Exception as e:
            logger.error(f"Erreur lecture: {e}")
            self.after_play(e)

    def after_play(self, error):
        if error:
            logger.error(f"Erreur: {error}")
        
        self.current_sound = None
        coro = self.process_next()
        fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
        try:
            fut.result()
        except Exception:
            pass

    def play_next(self):
        # Deprecated/Unused in favor of process_next
        pass


    def stop(self):
        self.queue.clear()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

    def clear_queue(self):
        self.queue.clear()

class PlayerManager:
    def __init__(self, bot, voice_timeout):
        self.bot = bot
        self.voice_timeout = voice_timeout
        self.players: Dict[int, GuildPlayer] = {}

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(guild_id, self.bot, self.voice_timeout)
        return self.players[guild_id]
