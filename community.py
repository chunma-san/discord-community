"""Global community commands and persistent interactions."""
import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import tasks

from storage import parse_time, period_key
from guild_access import is_leader, own_guild, role_named, guild_channel

CLASSES = ['Gladiator', 'Templar', 'Assassin', 'Ranger', 'Chanter', 'Cleric', 'Sorcerer', 'Spiritmaster']
INTERESTS = ['PvE', 'PvP', 'Exploration', 'Crafting']
PLAYSTYLES = ['Tank', 'Healer', 'DPS', 'Support']
ALERTS = ['Events', 'Rifts', 'Bosses', 'News']
TIMER_PINGS = ['Event Pings']
ROLE_GROUPS = {'class': CLASSES, 'interests': INTERESTS, 'playstyle': PLAYSTYLES, 'alerts': ALERTS, 'timer_pings': TIMER_PINGS, 'news_pings': ['News & Patches']}
RESOURCES = [
    ('Official Global news', 'https://aion2.plaync.com/en-us/board/notice/list'),
    ('Official game overview', 'https://aion2.plaync.com/en-us/about/index'),
    ('Questlog — profiles, builds, database', 'https://questlog.gg/aion-2/en/db/search'),
    ('Gaming Tools — progression and data', 'https://aion2.gaming.tools/progression'),
    ('Gamers4.life — database and calculators', 'https://gamers4.life/aion-2/database/en/tools/'),
    ('Aion2Hub.com — Global/Asia filters', 'https://aion2hub.com/tools'),
    ('Aion2Hub.me — mixed-region tools', 'https://aion2hub.me/en/tools'),
    ('Aion2Kina — calculators; check region', 'https://www.aion2kina.com/en/tools/'),
    ('DBAion2 — tools; check region', 'https://dbaion2.ru/en/'),
    ('Interactive Map — Verteron', 'https://interactivemap.app/aion2/maps/verteron'),
    ('Aion2Maps — Altgard / Global guides', 'https://aion2maps.com/altgard/'),
    ('Aion2Tracker — manual progression', 'https://aion2tracker.net/progression'),
]


def safe(value, limit=900):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))[:limit]


def url(value):
    if not value:
        return ''
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in value):
        raise ValueError('Use a public http/https link without credentials or spaces.')
    if len(value) > 500:
        raise ValueError('Link is too long (maximum 500 characters).')
    return value


def short(value, limit, label='Text'):
    value = value.strip()
    if not value or len(value) > limit:
        raise ValueError(f'{label} must contain 1–{limit} characters.')
    return value


async def reply(interaction, content=None, **kwargs):
    kwargs.setdefault('ephemeral', True)
    if interaction.response.is_done():
        return await interaction.followup.send(content, **kwargs)
    return await interaction.response.send_message(content, **kwargs)


class Community:
    def __init__(self, bot, tree, store, guild_id, role_for, operation_lock):
        self.bot, self.tree, self.store, self.guild_id = bot, tree, store, guild_id
        self.role_for, self.operation_lock = role_for, operation_lock
        self.lock = asyncio.Lock()
        self.register_commands()

    def startup(self):
        self.bot.add_view(RolesView(self))
        for row in self.store.db.execute("SELECT id,message FROM posts WHERE status='open' AND message IS NOT NULL AND guild=?", (self.guild_id,)):
            self.bot.add_view(SignupView(self, row['id']), message_id=row['message'])
        self.reminders.start()

    async def usable(self, interaction):
        if interaction.guild_id != self.guild_id or self.operation_lock.locked():
            await reply(interaction, 'This server is unavailable or setup is in progress. Try again shortly.')
            return False
        return True

    async def staff(self, interaction):
        if not await self.usable(interaction):
            return False
        if not interaction.user.guild_permissions.administrator:
            await reply(interaction, 'Administrator permission is required.')
            return False
        return True

    async def channel(self, guild, key):
        ident = self.store.get(guild.id, 'channel:' + key)
        channel = guild.get_channel(ident) if ident else None
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            raise ValueError('Run /setupserver first or configure /botchannel.')
        return channel

    async def publish(self, guild, key, title, content=None, embed=None, view=None, tags=None):
        channel = await self.channel(guild, key)
        if isinstance(channel, discord.ForumChannel):
            applied = [t for t in channel.available_tags if t.name in (tags or [])][:5]
            result = await channel.create_thread(name=short(title, 100), content=content,
                embed=embed, view=view, applied_tags=applied, allowed_mentions=discord.AllowedMentions.none())
            return result.thread, result.message
        # Text threads also work on servers without Discord Community mode.
        message = await channel.send(content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        try:
            await message.create_thread(name=short(title, 100), auto_archive_duration=1440)
        except discord.HTTPException:
            logging.exception('Discussion thread could not be created; main post remains usable')
        return channel, message

    def embed(self, ident):
        post = self.store.post(ident)
        data = post['data']
        embed = discord.Embed(title=safe(data['title'], 150), color=0x5865F2)
        embed.description = safe(data.get('notes', ''), 1300)
        embed.add_field(name='Region / Server / Faction', value=safe(data['region']) + ' / ' + safe(data['server']) + ' / ' + safe(data['faction']), inline=False)
        embed.add_field(name='Activity / Difficulty / Run', value=safe(data['activity']) + ' / ' + safe(data['difficulty']) + ' / ' + safe(data['mode']), inline=False)
        embed.add_field(name='Start', value=f"<t:{data['start']}:F> • <t:{data['start']}:R>", inline=False)
        embed.add_field(name='Organizer', value=f"<@{post['owner']}>")
        embed.add_field(name='Status', value=post['status'].title())
        rows = self.store.members(ident)
        for state, label in [('joined', 'Joined'), ('reserve', 'Reserve / waiting list')]:
            people = [f"<@{r['user']}>" for r in rows if r['state'] == state]
            embed.add_field(name=f"{label} ({len(people)}" + (f"/{data['capacity']})" if state == 'joined' else ')'),
                            value=(' '.join(people) or '—')[:1024], inline=False)
        if data.get('source'):
            embed.add_field(name='Source / version', value=f"{safe(data.get('version', 'Global'))}\n{data['source']}", inline=False)
        embed.set_footer(text=f"{post['kind'].upper()} #{ident} • Manual signup • Times shown in your timezone")
        return embed

    async def refresh(self, ident):
        post = self.store.post(ident)
        channel = self.bot.get_channel(post['channel'])
        if channel is None:
            channel = await self.bot.fetch_channel(post['channel'])
        message = await channel.fetch_message(post['message'])
        await message.edit(embed=self.embed(ident), view=SignupView(self, ident) if post['status'] == 'open' else None,
                           allowed_mentions=discord.AllowedMentions.none())

    async def signup(self, interaction, ident, action):
        if not await self.usable(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock:
                post = self.store.post(ident)
                if post['guild'] != interaction.guild_id:
                    raise ValueError('This post belongs to another server.')
                if post['data']['start'] <= int(datetime.now(timezone.utc).timestamp()):
                    raise ValueError('This activity has already started.')
                if action == 'cancel':
                    if interaction.user.id != post['owner'] and not interaction.user.guild_permissions.administrator:
                        raise ValueError('Only the organizer or an administrator can cancel this post.')
                    self.store.update_post(ident, status='cancelled')
                else:
                    self.store.signup(ident, interaction.user.id, action)
                try:
                    await self.refresh(ident)
                except discord.HTTPException:
                    await reply(interaction, 'Saved. The public post could not be refreshed; an administrator can use /activity refresh.')
                    return
            await reply(interaction, 'Saved. Reserve members are promoted in order when a place opens.')
        except ValueError as error:
            await reply(interaction, str(error))

    @tasks.loop(seconds=30)
    async def reminders(self):
        now = int(datetime.now(timezone.utc).timestamp())
        for row in list(self.store.db.execute("SELECT id FROM posts WHERE guild=? AND status='open' AND message IS NOT NULL", (self.guild_id,))):
            try:
                async with self.lock:
                    post = self.store.post(row['id'])
                    if post['status'] != 'open':
                        continue
                    start = post['data']['start']
                    if start <= now:
                        self.store.update_post(post['id'], status='started')
                        await self.refresh(post['id'])
                    elif not post['reminded'] and start - now <= 900:
                        channel = self.bot.get_channel(post['channel']) or await self.bot.fetch_channel(post['channel'])
                        guild = self.bot.get_guild(post['guild'])
                        role = self.role_for(guild, 'Events') if guild else None
                        ping = role.mention + ' ' if role else ''
                        await channel.send(f"{ping}Starting soon: **{safe(post['data']['title'], 150)}** <t:{start}:R>\nhttps://discord.com/channels/{post['guild']}/{post['channel']}/{post['message']}",
                            allowed_mentions=discord.AllowedMentions(roles=[role] if role else [], users=False, everyone=False))
                        self.store.update_post(post['id'], reminded=1)
            except discord.NotFound:
                self.store.update_post(row['id'], status='missing')
            except Exception:
                logging.exception('Reminder failed for post %s; will retry', row['id'])

    @reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()

    def register_commands(self):
        tree, store = self.tree, self.store
        profile = app_commands.Group(name='profile', description='Your manually updated character cards', guild_only=True)
        checklist = app_commands.Group(name='checklist', description='Private daily, weekly and progression tasks', guild_only=True)
        activity = app_commands.Group(name='activity', description='Manage LFG and event posts', guild_only=True)
        goal = app_commands.Group(name='goal', description='Personal progression goals', guild_only=True)
        build = app_commands.Group(name='build', description='Build sharing and review', guild_only=True)

        @profile.command(name='set', description='Save a character card; it stays private until you share it')
        @app_commands.choices(class_name=[app_commands.Choice(name=x, value=x) for x in CLASSES],
            region=[app_commands.Choice(name=x, value=x) for x in ['Europe', 'NA East', 'NA West', 'South America', 'Asia']],
            faction=[app_commands.Choice(name=x, value=x) for x in ['Asmodian', 'Elyos']],
            kind=[app_commands.Choice(name=x, value=x) for x in ['Main', 'Alt']],
            metric=[app_commands.Choice(name=x, value=x) for x in ['Item level', 'Gear score', 'Combat power']])
        async def profile_set(i: discord.Interaction, character: str, region: str, server: str, faction: str,
            class_name: str, level: app_commands.Range[int, 1, 100], metric: str,
            equipment_value: app_commands.Range[int, 0, 10000000], kind: str,
            availability: str, build_link: str = ''):
            if not await self.usable(i): return
            alias = short(character, 40, 'Character')
            data = dict(character=alias, region=region, server=short(server, 50), faction=faction,
                class_name=class_name, level=level, metric=metric, equipment_value=equipment_value,
                kind=kind, availability=short(availability, 200), build_link=url(build_link),
                updated=int(datetime.now(timezone.utc).timestamp()))
            store.profile(i.guild_id, i.user.id, alias, data)
            await reply(i, 'Character saved privately. Use /profile show or /profile share.')

        def card(data, user):
            e = discord.Embed(title=safe(data['character']), color=0x3498DB)
            e.description = f"<@{user}> • {data['kind']} • Global\n{safe(data['region'])} / {safe(data['server'])} / {data['faction']}"
            e.add_field(name='Character', value=f"{data['class_name']} • Level {data['level']}")
            e.add_field(name=data['metric'], value=str(data['equipment_value']))
            e.add_field(name='Availability', value=safe(data['availability']), inline=False)
            if data['build_link']: e.add_field(name='Build', value=data['build_link'], inline=False)
            e.set_footer(text='Self-reported snapshot; no live game sync')
            e.timestamp = datetime.fromtimestamp(data['updated'], timezone.utc)
            return e

        @profile.command(name='show', description='View your private character card')
        async def profile_show(i: discord.Interaction, character: str):
            if not await self.usable(i): return
            data = store.profile(i.guild_id, i.user.id, character)
            if not data: raise ValueError('Save this character with /profile set first.')
            await reply(i, embed=card(data, i.user.id))

        @profile.command(name='share', description='Publish your character card in this channel')
        async def profile_share(i: discord.Interaction, character: str):
            if not await self.usable(i): return
            data = store.profile(i.guild_id, i.user.id, character)
            if not data: raise ValueError('Save this character with /profile set first.')
            await reply(i, embed=card(data, i.user.id), ephemeral=False, allowed_mentions=discord.AllowedMentions.none())

        @profile.command(name='list', description='List your saved characters')
        async def profile_list(i: discord.Interaction):
            if not await self.usable(i): return
            aliases = [r['alias'] for r in store.db.execute('SELECT alias FROM profiles WHERE guild=? AND user=?', (i.guild_id, i.user.id))]
            await reply(i, '\n'.join(safe(a, 40) for a in aliases)[:1900] or 'No saved characters yet.')

        @profile.command(name='delete', description='Delete one of your private character cards')
        async def profile_delete(i: discord.Interaction, character: str):
            if not await self.usable(i): return
            with store.db:
                store.db.execute('DELETE FROM profiles WHERE guild=? AND user=? AND alias=?', (i.guild_id, i.user.id, character.strip().casefold()))
            await reply(i, 'Private card deleted. Previously shared public messages are not deleted.')

        async def create_activity(i, kind, title, when, capacity, region, server, faction, name, difficulty, mode, notes, source='', version='Global'):
            if not await self.usable(i): return
            start = parse_time(when)
            now = int(datetime.now(timezone.utc).timestamp())
            if not now + 60 <= start <= now + 366 * 86400:
                raise ValueError('Choose a start time between one minute and one year from now.')
            data = dict(title=short(title, 100), start=start, capacity=capacity, region=short(region, 50),
                server=short(server, 50), faction=short(faction, 30), activity=short(name, 80),
                difficulty=short(difficulty, 50), mode=mode, notes=short(notes, 1000), source=url(source), version=short(version, 80))
            await i.response.defer(ephemeral=True)
            async with self.lock:
                ident = store.create_post(i.guild_id, i.user.id, kind, data)
                try:
                    channel, message = await self.publish(i.guild, 'lfg' if kind == 'lfg' else 'events', title,
                        embed=self.embed(ident), view=SignupView(self, ident), tags=[mode, 'Global'])
                except Exception:
                    store.update_post(ident, status='failed')
                    raise
                store.update_post(ident, channel=channel.id, message=message.id)
                store.signup(ident, i.user.id, 'join')
                await self.refresh(ident)
            await reply(i, f'Created #{ident}: {message.jump_url}\nYou are included in the party capacity.')

        modes = [app_commands.Choice(name=x, value=x) for x in ['Training', 'Clear', 'Speedrun', 'Exploration', 'PvP']]

        @tree.command(name='lfg', description='Create a party with Join, Leave and Reserve buttons')
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
        @app_commands.choices(mode=modes)
        async def lfg(i: discord.Interaction, title: str, when: str, capacity: app_commands.Range[int, 2, 40],
                      region: str, server: str, faction: str, activity_name: str, difficulty: str, mode: str, notes: str):
            await create_activity(i, 'lfg', title, when, capacity, region, server, faction, activity_name, difficulty, mode, notes)

        @tree.command(name='event', description='Schedule a Global event with signup and opt-in reminders')
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
        @app_commands.choices(mode=modes)
        async def event(i: discord.Interaction, title: str, when: str, capacity: app_commands.Range[int, 2, 40],
                        region: str, server: str, faction: str, activity_name: str, difficulty: str, mode: str,
                        notes: str, source: str = '', version: str = 'Global'):
            await create_activity(i, 'event', title, when, capacity, region, server, faction, activity_name, difficulty, mode, notes, source, version)

        async def owned(i, ident):
            post = store.post(ident)
            if post['guild'] != i.guild_id or (post['owner'] != i.user.id and not i.user.guild_permissions.administrator):
                raise ValueError('Only this organizer or an administrator can manage the post.')
            return post

        @activity.command(name='edit', description='Reschedule or change notes and capacity')
        async def edit(i: discord.Interaction, post_id: int, when: str = '',
                       capacity: app_commands.Range[int, 2, 40] = None, notes: str = ''):
            if not await self.usable(i): return
            await i.response.defer(ephemeral=True)
            async with self.lock:
                post = await owned(i, post_id)
                if post['status'] != 'open': raise ValueError('This activity is closed; create a new one.')
                data = post['data']
                if when:
                    start = parse_time(when)
                    now = int(datetime.now(timezone.utc).timestamp())
                    if not now + 60 <= start <= now + 366 * 86400: raise ValueError('Choose a future time within one year.')
                    data['start'] = start
                if capacity is not None:
                    joined = sum(r['state'] == 'joined' for r in store.members(post_id))
                    if capacity < joined: raise ValueError('Capacity cannot be lower than the number already joined.')
                    data['capacity'] = capacity
                if notes: data['notes'] = short(notes, 1000)
                store.update_post(post_id, data=data, reminded=0 if when else post['reminded'])
                # Filling newly available places promotes the FIFO waitlist.
                if capacity is not None: store.signup(post_id, -1, 'leave')
                await self.refresh(post_id)
            await reply(i, 'Updated. Check the new time on the public post.')

        @activity.command(name='close', description='Close signups or cancel an activity')
        @app_commands.choices(status=[app_commands.Choice(name=x, value=x) for x in ['closed', 'cancelled']])
        async def close(i: discord.Interaction, post_id: int, status: str):
            if not await self.usable(i): return
            await i.response.defer(ephemeral=True)
            async with self.lock:
                await owned(i, post_id)
                store.update_post(post_id, status=status)
                await self.refresh(post_id)
            await reply(i, 'Activity ' + status + '.')

        @activity.command(name='refresh', description='Refresh a saved activity after a message update failed')
        async def refresh(i: discord.Interaction, post_id: int):
            if not await self.usable(i): return
            await owned(i, post_id)
            await i.response.defer(ephemeral=True)
            async with self.lock: await self.refresh(post_id)
            await reply(i, 'Post refreshed.')

        @activity.command(name='list', description='Show upcoming signups')
        async def listing(i: discord.Interaction):
            if not await self.usable(i): return
            lines = []
            for row in store.db.execute("SELECT id FROM posts WHERE guild=? AND status='open' AND message IS NOT NULL ORDER BY id DESC LIMIT 20", (i.guild_id,)):
                p = store.post(row['id'])
                lines.append(f"#{p['id']} **{safe(p['data']['title'], 70)}** <t:{p['data']['start']}:R>")
            await reply(i, '\n'.join(lines)[:1900] or 'No open activities.')

        @checklist.command(name='add', description='Add a personal task with an explicit resource scope')
        @app_commands.choices(cadence=[app_commands.Choice(name=x, value=x) for x in ['daily', 'weekly', 'once']],
            scope=[app_commands.Choice(name=x, value=x) for x in ['character', 'server', 'account']])
        async def add(i: discord.Interaction, label: str, cadence: str, scope: str, target: str = ''):
            if not await self.usable(i): return
            if len(store.tasks(i.guild_id, i.user.id)) >= 60: raise ValueError('Maximum 60 personal tasks. Remove completed tasks first.')
            ident = store.add_task(i.guild_id, i.user.id, short(label, 100), cadence, scope, short(target, 80) if target else '')
            await reply(i, f'Task #{ident} saved. Scope: {scope}. Resource limits are entered manually.')

        @checklist.command(name='show', description='View your personal checklist privately')
        async def show(i: discord.Interaction, page: app_commands.Range[int, 1, 10] = 1):
            if not await self.usable(i): return
            rows = store.tasks(i.guild_id, i.user.id)
            lines = []
            for task in rows[(page-1)*10:page*10]:
                try: state = '✅' if store.is_done(task) else '⬜'
                except ValueError: state = '⏰'
                lines.append(f"{state} #{task['id']} {safe(task['label'], 100)}\n{task['cadence']} • {task['scope']}:{safe(task['target'], 80)}")
            await reply(i, '\n\n'.join(lines) or 'No tasks on this page. Use /checklist add.')

        @checklist.command(name='mark', description='Complete or undo one of your tasks for the current period')
        async def mark(i: discord.Interaction, task_id: int, done: bool = True):
            if not await self.usable(i): return
            store.check(i.guild_id, i.user.id, task_id, done)
            await reply(i, 'Task marked complete.' if done else 'Task unchecked.')

        @checklist.command(name='remove', description='Remove one of your tasks')
        async def remove(i: discord.Interaction, task_id: int):
            if not await self.usable(i): return
            with store.db: store.db.execute('DELETE FROM tasks WHERE id=? AND guild=? AND user=?', (task_id, i.guild_id, i.user.id))
            await reply(i, 'Task removed from your checklist.')

        @tree.command(name='resetclock', description='Admin: configure a verified UTC reset schedule for checklists')
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.choices(weekly_day=[app_commands.Choice(name=x, value=n) for n, x in enumerate(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'])])
        async def clock(i: discord.Interaction, utc_hour: app_commands.Range[int, 0, 23], weekly_day: int, source: str):
            if not await self.staff(i): return
            link = url(source)
            if not link: raise ValueError('Include the source used to verify the Global reset.')
            store.set(i.guild_id, 'reset_hour', utc_hour)
            store.set(i.guild_id, 'reset_day', weekly_day)
            store.set(i.guild_id, 'reset_source', link)
            await reply(i, f'Reset saved: {utc_hour:02}:00 UTC, weekly weekday {weekly_day} (Monday=0).\n{link}')

        @goal.command(name='set', description='Save your next progression goal privately')
        async def goal_set(i: discord.Interaction, character: str, target: str, next_step: str, guide_link: str = ''):
            if not await self.usable(i): return
            values = (i.guild_id, i.user.id, short(character, 40).casefold(), short(target, 150), short(next_step, 500), url(guide_link))
            with store.db: store.db.execute('INSERT OR REPLACE INTO goals VALUES(?,?,?,?,?,?)', values)
            await reply(i, 'Progression goal saved privately.')

        @goal.command(name='show', description='View your next progression goal')
        async def goal_show(i: discord.Interaction, character: str):
            if not await self.usable(i): return
            row = store.db.execute('SELECT * FROM goals WHERE guild=? AND user=? AND character=?', (i.guild_id, i.user.id, character.strip().casefold())).fetchone()
            if not row: raise ValueError('Use /goal set first.')
            await reply(i, f"**{safe(row['target'])}**\nNext: {safe(row['step'])}\n{row['link']}")

        @build.command(name='share', description='Share a Global build and request optional coaching')
        @app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
        @app_commands.choices(class_name=[app_commands.Choice(name=x,value=x) for x in CLASSES],
            mode=[app_commands.Choice(name=x,value=x) for x in ['PvE','PvP']],
            stage=[app_commands.Choice(name=x,value=x) for x in ['Beginner','Endgame']])
        async def build_share(i: discord.Interaction, title: str, class_name: str, mode: str, stage: str,
            link: str, version: str, notes: str, review_requested: bool = False):
            if not await self.usable(i): return
            e = discord.Embed(title=safe(short(title, 100)), description=safe(short(notes, 1000)), color=0x9B59B6)
            e.add_field(name='Build', value=url(link), inline=False)
            e.add_field(name='Global version / class', value=f'{safe(short(version,80))} / {class_name}')
            e.add_field(name='Use', value=f'{mode} / {stage}')
            e.add_field(name='Coaching', value='Review requested' if review_requested else 'Sharing only')
            e.set_footer(text=f'Shared by {i.user.display_name} • Snapshot, not a live performance score')
            await i.response.defer(ephemeral=True)
            _, message = await self.publish(i.guild, 'builds', title, embed=e, tags=[class_name,mode,stage,'Global'])
            await reply(i, message.jump_url)

        @tree.command(name='craft', description='Publish a material or crafting help request')
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
        async def craft(i: discord.Interaction, item: str, quantity: app_commands.Range[int,1,10000],
            owned_materials: str, missing_materials: str, notes: str, recipe_link: str = '', price_note: str = ''):
            if not await self.usable(i): return
            title = short(item, 100)
            e = discord.Embed(title=safe(title), description=safe(short(notes,700)), color=0xE67E22)
            e.add_field(name='Quantity', value=str(quantity))
            e.add_field(name='Already owned', value=safe(short(owned_materials,700)), inline=False)
            e.add_field(name='Missing', value=safe(short(missing_materials,700)), inline=False)
            if recipe_link: e.add_field(name='Recipe reference',value=url(recipe_link),inline=False)
            if price_note: e.add_field(name='Manual price / date',value=safe(short(price_note,300)),inline=False)
            e.set_footer(text=f'{i.user.display_name} • No live auction prices or automated transactions')
            await i.response.defer(ephemeral=True)
            _, message = await self.publish(i.guild,'crafting',title,embed=e)
            await reply(i,message.jump_url)

        @tree.command(name='bossreport', description='Report a sighting or kill; this is a player report, not a prediction')
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 60, key=lambda i: (i.guild_id, i.user.id))
        @app_commands.choices(state=[app_commands.Choice(name=x,value=x) for x in ['Seen','Killed']])
        async def bossreport(i: discord.Interaction, boss: str, region: str, server: str, location: str, state: str, when: str, notes: str):
            if not await self.usable(i): return
            stamp = parse_time(when)
            now = int(datetime.now(timezone.utc).timestamp())
            if not now-86400 <= stamp <= now+60: raise ValueError('Use an observed time within the last 24 hours, not a future prediction.')
            e = discord.Embed(title=f'{state}: {safe(short(boss,80))}',description=safe(short(notes,700)),color=0xE74C3C)
            e.add_field(name='Region / server', value=f'{safe(short(region,50))} / {safe(short(server,50))}')
            e.add_field(name='Location', value=safe(short(location,200)),inline=False)
            e.add_field(name='Observed',value=f'<t:{stamp}:F>')
            e.set_footer(text=f'Unverified player report by {i.user.display_name}; not a spawn schedule')
            await i.response.defer(ephemeral=True)
            channel = await self.channel(i.guild,'events')
            if isinstance(channel,discord.ForumChannel):
                _,message=await self.publish(i.guild,'events',f'{state} {boss}',embed=e)
            else: message=await channel.send(embed=e,allowed_mentions=discord.AllowedMentions.none())
            await reply(i,message.jump_url)

        @tree.command(name='guidecard', description='Admin: publish a sourced, versioned guide or official news')
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.choices(region=[app_commands.Choice(name=x,value=x) for x in ['Global','TW','KR']],
            kind=[app_commands.Choice(name=x,value=x) for x in ['Guide','News']])
        async def guidecard(i: discord.Interaction,title: str,region: str,version: str,summary: str,source: str,kind: str):
            if not await self.staff(i): return
            link=url(source)
            if not link: raise ValueError('A source link is required.')
            e=discord.Embed(title=safe(short(title,150)),description=safe(short(summary,1500)),url=link,color=0x2ECC71)
            e.add_field(name='Region / version',value=f'{region} / {safe(short(version,100))}')
            e.add_field(name='Source',value=link,inline=False)
            e.set_footer(text='Staff-curated • Check the region before applying numbers')
            e.timestamp=datetime.now(timezone.utc)
            await i.response.defer(ephemeral=True)
            key='announcements' if kind=='News' else 'resources'
            channel=await self.channel(i.guild,key)
            await channel.send(embed=e,allowed_mentions=discord.AllowedMentions.none())
            await reply(i,'Published with source and region labels.')

        @tree.command(name='alert', description='Publish an admin announcement or a Leader announcement inside your own guild')
        @app_commands.guild_only()
        @app_commands.choices(topic=[app_commands.Choice(name=x,value=x) for x in ALERTS])
        async def alert(i: discord.Interaction, topic: str, message: str, source: str,
                        guild_name: str = '', ping: bool = False):
            if not await self.usable(i): return
            admin = i.user.guild_permissions.administrator
            scoped = bool(guild_name) or not admin
            if scoped:
                name = guild_name if admin else own_guild(i.user, guild_name)
                channel = guild_channel(i.guild, name, 'announcements')
                roles = [role_named(i.guild, name+' '+rank) for rank in ['Leader','Officer','Member']] if ping else []
                if any(r is None or r.managed or r.permissions.value for r in roles):
                    raise ValueError('The guild notification roles need staff attention.')
                heading = name+' • '+topic
            else:
                role = self.role_for(i.guild,topic)
                if not role or role.managed or role.permissions.value:
                    raise ValueError('The optional alert role is not ready.')
                roles = [role]
                channel = await self.channel(i.guild,'announcements')
                heading = 'Global • '+topic
            link=url(source)
            if not link: raise ValueError('A source link is required for an alert.')
            body=safe(short(message,1400))
            await i.response.defer(ephemeral=True)
            mentions=' '.join(r.mention for r in roles)
            content=mentions+' **'+safe(heading,100)+'**'+chr(10)+body+chr(10)+link
            await channel.send(content,allowed_mentions=discord.AllowedMentions(roles=roles,users=False,everyone=False))
            await reply(i,'Published in '+channel.mention+'.')

        @tree.command(name='resources', description='Useful official links, maps and build tools')
        @app_commands.guild_only()
        async def resources(i: discord.Interaction):
            if not await self.usable(i): return
            await reply(i,embed=resource_embed())

        @tree.command(name='report', description='Send a private report to server moderators')
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 60, key=lambda i: (i.guild_id, i.user.id))
        async def report(i: discord.Interaction, details: str, message_link: str = ''):
            if not await self.usable(i): return
            description=safe(short(details,1500))
            link=url(message_link)
            await i.response.defer(ephemeral=True)
            channel=await self.channel(i.guild,'reports')
            e=discord.Embed(title='Member report',description=description,color=0xE74C3C)
            e.add_field(name='Reporter',value=f'<@{i.user.id}>')
            if link: e.add_field(name='Reference',value=link,inline=False)
            e.timestamp=datetime.now(timezone.utc)
            await channel.send(embed=e,allowed_mentions=discord.AllowedMentions.none())
            await reply(i,'Sent privately to moderators. Reports are visible to server staff.')

        @tree.command(name='botchannel', description='Admin: choose where bot posts are published')
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.choices(purpose=[app_commands.Choice(name=x,value=x) for x in ['lfg','events','builds','crafting','resources','announcements']])
        async def botchannel(i: discord.Interaction,purpose: str,channel: discord.TextChannel | discord.ForumChannel):
            if not await self.staff(i): return
            if purpose in {'resources','announcements'} and not isinstance(channel,discord.TextChannel):
                raise ValueError('Choose a text channel for this purpose.')
            store.set(i.guild_id,'channel:'+purpose,channel.id)
            await reply(i,'Destination saved.')

        @tree.command(name='help', description='How to use the AION 2 community bot')
        @app_commands.guild_only()
        async def help_command(i: discord.Interaction):
            if not await self.usable(i): return
            e=discord.Embed(title='AION 2 • Global community',color=0x5865F2)
            e.description=('**Roles:** choose your class, interests, playstyle and optional alerts in choose-roles.\n'
                '**Profile:** /profile set, show, share, list, delete. Sharing publishes your card.\n'
                '**Parties:** /lfg or /event; Join / Leave / Reserve. Organizer counts toward capacity.\n'
                '**Manage:** /activity list, edit, close, refresh.\n'
                '**Time format:** `2026-10-10T20:00+02:00` or `2026-10-10T18:00Z`.\n'
                '**Personal tasks:** /checklist add, show, mark, remove. Specify character/server/account scope.\n'
                '**Goals:** /goal set, show.\n**Builds:** /build share; request coaching if useful.\n'
                '**Crafting:** /craft with owned/missing materials.\n**Reports:** /bossreport for actual observations.\n'
                '**Sources:** /resources. All game values are manually maintained; no live game sync.\n'
                '**Reminder:** one alert up to 15 minutes before an activity, for the optional Events role.')
            await reply(i,embed=e)

        for group in [profile,checklist,activity,goal,build]: tree.add_command(group)


def resource_embed():
    e=discord.Embed(title='﹒ Global resources ﹒',color=0x3498DB)
    e.description='\n'.join(f'[{name}]({link})' for name,link in RESOURCES)
    e.set_footer(text='Check region and version. Datamined entries may include unreleased content. Timers need verification.')
    return e


def starter_guides():
    """Short Global onboarding cards; no unverified caps or Asia timer defaults."""
    cards = [
        ('Start with your Global character',
         'Pick your region, server and faction before organizing runs. Server names can repeat across regions. '
         'Choose a self-role for your class, then save each Main/Alt with /profile set. '
         'Global classes: Gladiator, Templar, Assassin, Ranger, Chanter, Cleric, Sorcerer, Spiritmaster.',
         'https://aion2.plaync.com/en-us/about/index'),
        ('Progression and your next unlock',
         'Follow character quests and check activity unlocks and equipment requirements in the current Global build. '
         'Use /goal set for your next target and /checklist for your personal routine. '
         'Distinguish an individual equipment item level from total character equipment metrics. '
         'The linked fan guide labels datamined requirements and creator advice; verify numbers in game.',
         'https://aion2maps.com/guides/progression-path/'),
        ('Builds, skills and character growth',
         'Share a build with /build share: class, PvE/PvP, beginner/endgame, Global version and a planner link. '
         'Review skills, Stigma, Daevanion, Arcana, Pantheon and pet bonuses together. '
         'Request coaching if useful. Calculators and Armory profiles are snapshots, not a live performance score.',
         'https://questlog.gg/aion-2/en/db/search'),
        ('Dungeon parties and training',
         'Create /lfg or /event with the exact activity, difficulty, capacity and Training/Clear/Speedrun mode. '
         'Write any equipment or experience requirement in the notes; composition remains organizer-controlled. '
         'The organizer counts toward capacity. Leave your signup if unavailable. '
         'Study boss mechanics before a training run; Asian guide headcounts may differ on Global.',
         'https://aion2maps.com/guides/ludra/'),
        ('Exploration, gathering and crafting',
         'Organize exploration runs for traces, cubes, sealed dungeons and gathering routes. '
         'Possible cube spawn locations are not the number of distinct cubes. '
         'Use /craft for quantities, materials already owned and missing materials; prices are entered manually. '
         'Map completion is tracked by players, without automatic game sync.',
         'https://aion2maps.com/altgard/'),
        ('PvP, resets and optional alerts',
         'Choose PvP and optional Rifts/Bosses/Events roles if interested. '
         'Keep region, source and version on schedules. Staff can publish a verified /alert. '
         '/bossreport records an observed sighting or kill; it is not a spawn prediction. '
         'Daily/weekly checklist resets require /resetclock with a verified UTC source. '
         'Resource scopes are character, server or account: do not duplicate a shared limit for every alt.',
         'https://aion2maps.com/guides/daily-and-weekly/'),
    ]
    for title, description, link in cards:
        e = discord.Embed(title=title, description=description, url=link, color=0x3498DB)
        e.add_field(name='Reference', value=link, inline=False)
        e.set_footer(text='Global onboarding • Check region/version • Fan sources are not official announcements')
        yield e


class SignupView(discord.ui.View):
    def __init__(self, community, ident):
        super().__init__(timeout=None)
        for action,label,style in [('join','Join',discord.ButtonStyle.success),('leave','Leave',discord.ButtonStyle.secondary),
            ('reserve','Reserve',discord.ButtonStyle.primary),('cancel','Cancel • organizer',discord.ButtonStyle.danger)]:
            button=discord.ui.Button(label=label,style=style,custom_id=f'aion2:signup:{ident}:{action}')
            async def callback(i, action=action): await community.signup(i,ident,action)
            button.callback=callback
            self.add_item(button)

    async def on_error(self,i,error,item):
        logging.exception('Signup interaction error',exc_info=error)
        await reply(i,'Could not finish this signup. Try again or contact the organizer.')


class RoleSelect(discord.ui.Select):
    def __init__(self, community, group, member):
        self.community,self.group=community,group
        names=ROLE_GROUPS[group]
        current={r.id for r in member.roles}
        group_label = {'timer_pings': 'AION 2 event pings', 'news_pings': 'news and patch pings'}.get(group, group)
        super().__init__(placeholder='Choose '+group_label+' (empty clears this group)',min_values=0,
            max_values=1 if group=='class' else len(names), options=[discord.SelectOption(label=n,value=n,
                default=bool(community.role_for(member.guild,n) and community.role_for(member.guild,n).id in current)) for n in names])

    async def callback(self,i):
        c=self.community
        if not await c.usable(i): return
        await i.response.defer(ephemeral=True)
        if any(name not in ROLE_GROUPS[self.group] for name in self.values):
            await reply(i,'Invalid role selection. Reopen the role panel.')
            return
        async with c.lock:
            roles=[c.role_for(i.guild,n) for n in ROLE_GROUPS[self.group]]
            if any(r is None or r.managed or r >= i.guild.me.top_role or r.permissions.value != 0 for r in roles):
                await reply(i,'The self-role panel needs administrator attention. Roles must be ordinary, permission-free and below the bot.')
                return
            try:
                member=await i.guild.fetch_member(i.user.id)
                # Whitelist only the explicitly selected roles; no arbitrary Discord role IDs.
                wanted=[c.role_for(i.guild,n) for n in self.values]
                add=[r for r in wanted if r not in member.roles]
                remove=[r for r in roles if r in member.roles and r not in wanted]
                if add: await member.add_roles(*add,reason='Self-selected community roles')
                if remove: await member.remove_roles(*remove,reason='Updated self-selected community roles')
                await reply(i,'Roles updated. '+(', '.join(self.values) or 'This group is cleared.'))
            except discord.HTTPException:
                await reply(i,'Could not update all roles. Check the bot role hierarchy, then try again.')


class RolesView(discord.ui.View):
    def __init__(self,community):
        super().__init__(timeout=None)
        for group,label in [('class','Class'),('interests','Interests'),('playstyle','Playstyle'),('alerts','Optional alerts'),('timer_pings','AION 2 event pings'),('news_pings','News & Patches')]:
            button=discord.ui.Button(label=label,style=discord.ButtonStyle.primary,custom_id='aion2:roles:'+group)
            async def callback(i,group=group):
                if not await community.usable(i): return
                view=discord.ui.View(timeout=120)
                member=await i.guild.fetch_member(i.user.id)
                view.add_item(RoleSelect(community,group,member))
                await reply(i,'Select your roles. Submitting an empty selection removes this group.',view=view)
            button.callback=callback
            self.add_item(button)

    async def on_error(self,i,error,item):
        logging.exception('Role panel error',exc_info=error)
        await reply(i,'Could not open the role panel. Check bot permissions or retry.')
