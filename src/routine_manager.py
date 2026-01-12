import discord
import asyncio
import logging
import random
import time
import datetime
import os
from typing import List, Dict, Any, Optional
from discord.ext import tasks
from config import Config

logger = logging.getLogger(__name__)

class RoutineManager:
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db
        self.routines = []
        self.timer_task = None

    async def load_routines(self):
        """Charge toutes les routines actives depuis la base de données."""
        self.routines = []
        for guild in self.bot.guilds:
            routines = await self.db.get_routines(str(guild.id))
            for r in routines:
                if r['active']:
                    self.routines.append(r)
        
        logger.info(f"Loaded {len(self.routines)} routines.")
        if not self.timer_task or self.timer_task.done():
            self.timer_task = self.bot.loop.create_task(self.timer_loop())

    async def timer_loop(self):
        """Boucle principale pour les routines basées sur le temps."""
        while not self.bot.is_closed():
            try:
                # Check timer routines
                for routine in self.routines:
                    if routine['trigger_type'] == 'timer':
                        await self.process_timer_routine(routine)
            except Exception as e:
                logger.error(f"Error in timer loop: {e}")
            
            await asyncio.sleep(1) # Check every 1 second

    async def process_timer_routine(self, routine):
        # Implement timer logic. 
        # Simple interval: check if current time % interval < 10 (since we sleep 10)
        # Or better: store last_run timestamp in memory.
        # For now, let's assume simple interval in minutes.
        
        last_run = routine.get('_last_run', 0)
        
        interval = routine['trigger_data'].get('interval_minutes', 0) * 60
        if interval == 0:
            interval = routine['trigger_data'].get('interval_seconds', 60)
        
        now = time.time()
        
        if now - last_run >= interval:
            # Try to find a valid context for this routine
            guild = self.bot.get_guild(int(routine['guild_id']))
            found_context = None
            
            if guild:
                # Iterate over all members in voice channels to find a match
                for vc in guild.voice_channels:
                    for member in vc.members:
                        ctx = {'guild': guild, 'channel': vc, 'member': member}
                        if await self.check_conditions(routine, ctx):
                            found_context = ctx
                            break
                    if found_context: break
            
            # If we found a context (e.g. user=X is present), execute with it.
            # If not, try executing with None context (only if conditions allow it, e.g. no user conditions)
            if found_context:
                await self.execute_actions(routine, found_context)
                routine['_last_run'] = now
            elif await self.check_conditions(routine, None):
                await self.execute_actions(routine, None)
                routine['_last_run'] = now

    async def on_voice_state_update(self, member, before, after):
        """Gère les événements vocaux."""
        event_type = None
        channel = None
        
        if before.channel is None and after.channel is not None:
            event_type = "voice_join"
            channel = after.channel
        elif before.channel is not None and after.channel is None:
            event_type = "voice_leave"
            channel = before.channel

        if not event_type:
            return

        context = {
            'member': member,
            'channel': channel,
            'guild': member.guild
        }

        for routine in self.routines:
            if str(routine['guild_id']) != str(member.guild.id):
                continue
            
            if routine['trigger_type'] == 'event' and routine['trigger_data'].get('event') == event_type:
                if await self.check_conditions(routine, context):
                    await self.execute_actions(routine, context)

    async def check_conditions(self, routine, context) -> bool:
        conditions = routine.get('conditions')
        if not conditions:
            return True
        
        return await self.evaluate_condition_node(conditions, context)

    async def evaluate_condition_node(self, node, context) -> bool:
        # Node structure: {'type': 'AND/OR/NOT', 'sub': [...]} OR {'type': 'user_id', 'op': '==', 'value': '...'}
        
        c_type = node.get('type')
        
        if c_type == 'AND':
            for sub in node.get('sub', []):
                if not await self.evaluate_condition_node(sub, context):
                    return False
            return True
        
        elif c_type == 'OR':
            for sub in node.get('sub', []):
                if await self.evaluate_condition_node(sub, context):
                    return True
            return False
        
        elif c_type == 'NOT':
            return not await self.evaluate_condition_node(node.get('sub')[0], context)
        
        # Leaf conditions
        return self.evaluate_leaf_condition(node, context)

    def evaluate_leaf_condition(self, node, context) -> bool:
        c_type = node.get('type')
        op = node.get('op', '==')
        value = node.get('value')
        
        actual_value = None
        
        if c_type == 'user_id':
            if context and 'member' in context:
                actual_value = str(context['member'].id)
        elif c_type == 'channel_id':
            if context and 'channel' in context:
                actual_value = str(context['channel'].id)
        elif c_type == 'role_id':
            if context and 'member' in context:
                if any(str(r.id) == str(value) for r in context['member'].roles):
                    return True # Special case for list check
                return False
        elif c_type == 'time_range':
            # Value format: "HH:MM-HH:MM"
            now = datetime.datetime.now().time()
            start_str, end_str = value.split('-')
            start = datetime.datetime.strptime(start_str, "%H:%M").time()
            end = datetime.datetime.strptime(end_str, "%H:%M").time()
            if start <= end:
                return start <= now <= end
            else: # Crosses midnight
                return start <= now or now <= end
        elif c_type == 'date_range':
            # Value format: "DD:MM-DD:MM" (or DD/MM)
            now = datetime.datetime.now().date()
            # We construct dummy dates with current year to compare
            current_year = now.year
            
            try:
                start_str, end_str = value.split('-')
                # Replace : with / to be safe if user uses either
                start_str = start_str.replace(':', '/')
                end_str = end_str.replace(':', '/')
                
                # Parse as dates in current year
                start_date = datetime.datetime.strptime(f"{start_str}/{current_year}", "%d/%m/%Y").date()
                end_date = datetime.datetime.strptime(f"{end_str}/{current_year}", "%d/%m/%Y").date()
                
                if start_date <= end_date:
                    return start_date <= now <= end_date
                else: # Crosses year boundary (e.g. Dec to Jan)
                    return start_date <= now or now <= end_date
            except ValueError:
                logger.error(f"Invalid date_range format: {value}")
                return False

        # Basic comparison
        if actual_value is None:
            return False

        if op == '==': return str(actual_value) == str(value)
        if op == '!=': return str(actual_value) != str(value)
        
        return False

    async def execute_actions(self, routine, context):
        actions = routine.get('actions', [])
        for action in actions:
            try:
                # Handle delay
                delay = action.get('delay', 0)
                if delay > 0:
                    await asyncio.sleep(delay)

                a_type = action.get('type')
                
                if a_type == 'wait':
                    # The delay was already handled above if 'delay' key was set.
                    # But if the action is JUST wait, we might store duration in 'duration' or 'delay'.
                    # If I parse "wait 5s" into {"type": "wait", "delay": 5}, it works with above code.
                    pass
                elif a_type == 'play_sound':
                    await self.action_play_sound(action, context, routine)
                elif a_type == 'message':
                    await self.action_send_message(action, context)
                    
            except Exception as e:
                logger.error(f"Error executing action {action} in routine {routine['name']}: {e}")

    def parse_routine_string(self, cmd_str: str):
        """
        Parse une commande textuelle type Minecraft pour créer une routine.
        Syntaxe: <trigger> [if <conditions>] do <actions>
        Exemples:
          timer 3s do play eennnnnnn
          on join if user=12345 do wait 2s then play welcome
        """
        parts = cmd_str.split(" do ")
        if len(parts) != 2:
            raise ValueError("Syntaxe invalide: séparateur 'do' manquant. Format: trigger ... do actions ...")
            
        lhs = parts[0].strip()
        rhs = parts[1].strip()
        
        # --- Parse Trigger & Conditions ---
        if " if " in lhs:
            trigger_part, condition_part = lhs.split(" if ", 1)
        else:
            trigger_part = lhs
            condition_part = None
            
        # Trigger
        trigger_tokens = trigger_part.split()
        if not trigger_tokens:
            raise ValueError("Trigger vide.")

        trigger_type = ""
        trigger_data = {}
        
        if trigger_tokens[0] == "timer":
            if len(trigger_tokens) < 2: raise ValueError("Durée timer manquante (ex: timer 3s).")
            duration_str = trigger_tokens[1]
            if duration_str.endswith("s"):
                trigger_data['interval_seconds'] = int(duration_str[:-1])
            elif duration_str.endswith("m"):
                trigger_data['interval_minutes'] = int(duration_str[:-1])
            elif duration_str.endswith("h"):
                trigger_data['interval_minutes'] = int(duration_str[:-1]) * 60
            elif duration_str.isdigit():
                 trigger_data['interval_seconds'] = int(duration_str)
            else:
                 raise ValueError("Format timer invalide. Utilisez 30s, 5m, 1h.")
            trigger_type = "timer"
            
        elif trigger_tokens[0] == "on":
            if len(trigger_tokens) < 2: raise ValueError("Événement manquant (ex: on join).")
            event_map = {
                "join": "voice_join",
                "leave": "voice_leave"
            }
            evt = trigger_tokens[1]
            if evt not in event_map:
                raise ValueError(f"Événement inconnu '{evt}'. Utilisez join ou leave.")
            trigger_type = "event"
            trigger_data['event'] = event_map[evt]
        else:
            raise ValueError("Trigger inconnu. Commencez par 'timer' ou 'on'.")
            
        # Conditions
        conditions = None
        if condition_part:
            cond_list = []
            cond_tokens = condition_part.split(" and ")
            for token in cond_tokens:
                op = "=="
                if "!=" in token:
                    op = "!="
                    key, val = token.split("!=", 1)
                elif "=" in token:
                    key, val = token.split("=", 1)
                else:
                    raise ValueError(f"Format condition invalide: {token}")
                
                key = key.strip()
                val = val.strip()
                
                c_type = ""
                if key == "user": c_type = "user_id"
                elif key == "channel": c_type = "channel_id"
                elif key == "role": c_type = "role_id"
                elif key == "time": c_type = "time_range"
                else:
                    raise ValueError(f"Clé de condition inconnue: {key}")
                
                cond_list.append({"type": c_type, "value": val, "op": op})
            
            if len(cond_list) == 1:
                conditions = cond_list[0]
            else:
                conditions = {"type": "AND", "sub": cond_list}

        # --- Parse Actions ---
        action_tokens = rhs.split(" then ")
        actions = []
        for token in action_tokens:
            parts = token.strip().split(" ", 1)
            verb = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            
            if verb == "play":
                actions.append({"type": "play_sound", "sound_name": args.strip(), "target_strategy": "active"})
            elif verb == "wait":
                dur = 0
                if args.endswith("s"): dur = int(args[:-1])
                elif args.isdigit(): dur = int(args)
                actions.append({"type": "wait", "delay": dur})
            else:
                raise ValueError(f"Action inconnue: {verb}")
                
        return trigger_type, trigger_data, conditions, actions

    async def action_play_sound(self, action, context, routine):
        sound_name = action.get('sound_name')
        guild_id = str(routine['guild_id'])
        
        # Determine channel
        channel = None
        if context and 'channel' in context:
            channel = context['channel']
        else:
            # For timer routines, find a channel
            guild = self.bot.get_guild(int(guild_id))
            if not guild: return
            
            # Strategy: "random_active" or "specific"
            target_strategy = action.get('target_strategy', 'active')
            
            if target_strategy == 'specific':
                cid = action.get('channel_id')
                channel = guild.get_channel(int(cid))
            elif target_strategy == 'active':
                # Find channel with most members? Or random with members?
                active_channels = [vc for vc in guild.voice_channels if len(vc.members) > 0]
                if active_channels:
                    channel = random.choice(active_channels)
        
        if not channel:
            return

        # Get sound
        sound_data = await self.db.get_sound(guild_id, sound_name)
        if not sound_data:
            sound_data = await self.db.get_sound("global", sound_name)
        
        if not sound_data:
            logger.warning(f"Sound {sound_name} not found for routine.")
            return

        file_path = os.path.join(Config.SOUNDS_DIR, sound_data['guild_id'], sound_data['filename'])
        
        if os.path.exists(file_path):
            player = self.bot.player_manager.get_player(int(guild_id))
            player.add_to_queue(file_path, "Routine", sound_name, channel)

    async def action_send_message(self, action, context):
        channel_id = action.get('channel_id')
        content = action.get('content')
        
        if not channel_id or not content:
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if channel:
            # Replace placeholders
            if context and 'member' in context:
                content = content.replace("{user}", context['member'].mention)
            
            await channel.send(content)
