"""AION 2 Global bot: --inspect audit, --apply reconcile, --rebuild destructive reset."""
import asyncio
import json
import logging
import os
import re
import sys
import unicodedata
from pathlib import Path
import discord
from discord import app_commands
from dotenv import load_dotenv
from community import Community, CLASSES, ROLE_GROUPS, RolesView, resource_embed, starter_guides, reply
from storage import Store
from guild_access import is_leader, led_guilds, own_guild, check_membership, normal

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', '0'))
if not TOKEN or not GUILD_ID:
    raise RuntimeError('DISCORD_TOKEN and DISCORD_GUILD_ID are required in .env')
MODE = next((x for x in ['--apply', '--inspect', '--rebuild', '--alerts-setup'] if x in sys.argv), '')
store = Store(os.getenv('AION_DATA_PATH', str(ROOT / 'data' / 'aion2.sqlite3')))
operation_lock = asyncio.Lock()


def clean(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).strip(' ﹒.')


def pretty(value):
    result = ''
    for char in clean(value):
        if 'A' <= char <= 'Z': char = chr(0x1D400 + ord(char) - ord('A'))
        elif 'a' <= char <= 'z': char = chr(0x1D41A + ord(char) - ord('a'))
        result += char
    return f'﹒ {result} ﹒'


def text_name(value):
    return '﹒' + re.sub(r'\s+', '-', clean(value).lower().replace('_', '-')) + '﹒'


def role_for(guild, name):
    return next((r for r in guild.roles if clean(r.name).casefold() == clean(name).casefold()), None)


async def ensure_role(guild, name, color=0x95A5A6, permissions=None):
    role = role_for(guild, name)
    if role is None:
        role = await guild.create_role(name=clean(name), colour=discord.Colour(color),
            permissions=permissions or discord.Permissions.none(), reason='AION 2 Global setup')
    return role


def private_access(guild, roles):
    result = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
            read_message_history=True, manage_channels=True, connect=True, speak=True)}
    for role in roles:
        if role:
            result[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                read_message_history=True, connect=True, speak=True)
    return result


async def category(guild, name, overwrites=None):
    aliases = {'start': {'start', 'start here'}}
    keys = aliases.get(clean(name).casefold(), {clean(name).casefold()})
    item = next((c for c in guild.categories if clean(c.name).casefold() in keys), None)
    if item is None:
        return await guild.create_category(pretty(name), overwrites=overwrites or {}, reason='AION 2 Global setup')
    await item.edit(name=pretty(name), overwrites=overwrites or {}, reason='AION 2 Global setup')
    return item


async def text(guild, parent, name, overwrites=None):
    item = next((c for c in guild.text_channels if clean(c.name).casefold() == clean(name).casefold()), None)
    if item is None:
        options = dict(category=parent, reason='AION 2 Global setup')
        if overwrites is not None: options['overwrites'] = overwrites
        return await guild.create_text_channel(text_name(name), **options)
    options = dict(name=text_name(name), category=parent, reason='AION 2 Global setup')
    if overwrites is not None: options['overwrites'] = overwrites
    else: options['sync_permissions'] = True
    await item.edit(**options)
    return item


async def discussion(guild, parent, name, tags):
    keys = {'looking-for-party', 'lfg'} if name == 'looking-for-party' else {clean(name).casefold()}
    existing = next((c for c in guild.channels if not isinstance(c, discord.CategoryChannel)
        and clean(c.name).casefold() in keys), None)
    if isinstance(existing, (discord.ForumChannel, discord.TextChannel)):
        await existing.edit(name=text_name(name), category=parent, sync_permissions=True)
        if isinstance(existing, discord.ForumChannel):
            prior = {t.name: t for t in existing.available_tags}
            await existing.edit(available_tags=[prior.get(n, discord.ForumTag(name=n)) for n in tags][:20])
        return existing
    if 'COMMUNITY' in guild.features:
        return await guild.create_forum(text_name(name), category=parent,
            available_tags=[discord.ForumTag(name=n) for n in tags][:20],
            topic='Global community. Label region and version; check sources before applying numbers.',
            reason='AION 2 Global setup')
    return await text(guild, parent, name)


async def voice(guild, parent, name):
    keys = {'lobby', 'general'} if name == 'Lobby' else {clean(name).casefold()}
    item = next((c for c in guild.voice_channels if clean(c.name).casefold() in keys), None)
    if item is None:
        return await guild.create_voice_channel(pretty(name), category=parent, reason='AION 2 Global setup')
    await item.edit(name=pretty(name), category=parent, sync_permissions=True)
    return item


async def panel(channel, key, content=None, embed=None, view=None):
    message_id = store.get(channel.guild.id, 'panel:' + key)
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            if message.author.id == bot.user.id:
                await message.edit(content=content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                return message
        except discord.NotFound: pass
    message = await channel.send(content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
    store.set(channel.guild.id, 'panel:' + key, message.id)
    return message


async def apply_settings(guild):
    permissions = guild.default_role.permissions
    permissions.update(mention_everyone=False, manage_channels=False, manage_roles=False,
        manage_webhooks=False, administrator=False, view_channel=True, read_message_history=True,
        send_messages=True, add_reactions=True, attach_files=True, embed_links=True,
        connect=True, speak=True, create_public_threads=True, send_messages_in_threads=True)
    await guild.default_role.edit(permissions=permissions, reason='Owner-authorized Global settings')
    welcome = guild.get_channel(store.get(guild.id, 'channel:welcome'))
    flags = guild.system_channel_flags
    flags.join_notifications = True
    flags.join_notification_replies = False
    await guild.edit(verification_level=discord.VerificationLevel.none,
        explicit_content_filter=discord.ContentFilter.all_members,
        default_notifications=discord.NotificationLevel.only_mentions,
        widget_enabled=False, system_channel=welcome, system_channel_flags=flags,
        reason='Owner-authorized Global settings: ordinary entry and optional roles')


async def ensure_alert_channels(guild):
    moderator = role_for(guild, 'Moderator')
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
            add_reactions=False, create_public_threads=False, send_messages_in_threads=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
            manage_messages=True, manage_webhooks=True),
    }
    if moderator:
        overwrites[moderator] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
            read_message_history=True, manage_messages=True)
    parent = await category(guild, 'Alerts', overwrites)
    board = await text(guild, parent, 'timer-board', overwrites)
    alerts = await text(guild, parent, 'event-alerts', overwrites)
    await board.edit(topic='Quiet AION 2 timer board. Pin the self-updating board here; keep role pings off.')
    await alerts.edit(topic='AION 2 event and field-boss alerts. Members opt into the Event Pings role in #choose-roles.')
    store.set(guild.id, 'channel:timer-board', board.id)
    store.set(guild.id, 'channel:event-alerts', alerts.id)
    news = await text(guild, parent, 'news-and-patches', overwrites)
    await news.edit(topic='AION 2 news, patch notes and hotfixes. Configure a separate notes-only webhook here.')
    store.set(guild.id, 'channel:news-and-patches', news.id)
    return board, alerts


async def alerts_setup(guild):
    if not guild.me.guild_permissions.administrator:
        raise ValueError('The bot needs Administrator permission to set up the alert channels and role.')
    role = await ensure_role(guild, 'Event Pings', 0xF1C40F)
    if not role.mentionable:
        await role.edit(mentionable=True, reason='Owner-requested event webhook role pings')
    if role.managed or role >= guild.me.top_role or role.permissions.value:
        raise ValueError('Event Pings must be an ordinary permission-free role below the bot.')
    news_role = await ensure_role(guild, 'News & Patches', 0x3498DB)
    if news_role.managed or news_role >= guild.me.top_role or news_role.permissions.value:
        raise ValueError('News & Patches must be an ordinary permission-free role below the bot.')
    if not news_role.mentionable:
        await news_role.edit(mentionable=True, reason='Owner-requested optional news and patch pings')
    print(f'NEWS_PATCH_ROLE={news_role.id}', flush=True)
    board, alerts = await ensure_alert_channels(guild)
    roles_channel = guild.get_channel(store.get(guild.id, 'channel:choose-roles'))
    if not isinstance(roles_channel, discord.TextChannel):
        raise ValueError('The choose-roles channel is not configured. Run /setupserver first.')
    await panel(roles_channel, 'roles',
        content='﹒ 𝐂𝐡𝐨𝐨𝐬𝐞 𝐘𝐨𝐮𝐫 𝐑𝐨𝐥𝐞𝐬 ﹒\n\nChoose your class, interests and playstyle. Alerts are optional.\nChoose Event Pings if you want AION 2 timer and field-boss notifications.\nChoose News & Patches to opt into news, patch notes and hotfix pings.\nNo email verification or guild role is required to enter.\nLeader and Officer roles are assigned by administrators.',
        view=RolesView(community))
    print(f'AION 2 ALERT CHANNELS READY: board={board.id} alerts={alerts.id} role={role.id}', flush=True)


async def base_setup(guild, progress=None):
    if not guild.me.guild_permissions.administrator:
        raise ValueError('Give the bot Administrator permission for setup and place its role above community roles.')
    moderator = await ensure_role(guild, 'Moderator', 0x9B59B6, discord.Permissions(
        manage_messages=True, moderate_members=True, kick_members=True, view_audit_log=True))
    await ensure_role(guild, 'Member', 0x3498DB)
    for group, names in ROLE_GROUPS.items():
        for name in names:
            color = 0x3498DB if group == 'class' else (0xF1C40F if group == 'timer_pings' else 0x95A5A6)
            role = await ensure_role(guild, name, color)
            if group in {'timer_pings', 'news_pings'} and not role.mentionable:
                await role.edit(mentionable=True, reason='Owner-requested event webhook role pings')
            if role.managed or role >= guild.me.top_role or role.permissions.value:
                raise ValueError(f'Self role {name} must have no permissions and sit below the bot.')
    readonly = {guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
        create_public_threads=False, send_messages_in_threads=False),
        moderator: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    start = await category(guild, 'Start')
    for name in ['welcome', 'rules', 'choose-roles', 'announcements']:
        item = await text(guild, start, name, readonly)
        store.set(guild.id, 'channel:' + name, item.id)
    all_area = await category(guild, 'All')
    for name in ['general', 'questions', 'screenshots', 'resources']:
        item = await text(guild, all_area, name, readonly if name == 'resources' else None)
        store.set(guild.id, 'channel:' + name, item.id)
    await voice(guild, all_area, 'Lobby')
    party = await category(guild, 'Party')
    for key, name in [('lfg', 'looking-for-party'), ('events', 'events')]:
        item = await discussion(guild, party, name, ['Global','Training','Clear','Speedrun','Exploration','PvP'])
        store.set(guild.id, 'channel:' + key, item.id)
    for name in ['Party 1','Party 2','Raid']: await voice(guild, party, name)
    builds = await category(guild, 'Builds')
    item = await discussion(guild, builds, 'builds', CLASSES + ['Global','PvE','PvP','Beginner','Endgame'])
    store.set(guild.id, 'channel:builds', item.id)
    item = await discussion(guild, builds, 'crafting', ['Global','Materials','Crafting help'])
    store.set(guild.id, 'channel:crafting', item.id)
    staff = await category(guild, 'Staff', private_access(guild, [moderator]))
    staff_chat = await text(guild, staff, 'staff-chat')
    reports = await text(guild, staff, 'reports')
    staff_welcome = await text(guild, staff, 'staff-welcome')
    store.set(guild.id, 'channel:staff-welcome', staff_welcome.id)
    if progress:
        await progress.edit(name=text_name('bot-logs'), category=staff, sync_permissions=True)
        store.set(guild.id, 'channel:bot-logs', progress.id)
    else:
        item = await text(guild, staff, 'bot-logs')
        store.set(guild.id, 'channel:bot-logs', item.id)
    await voice(guild, staff, 'Staff')
    store.set(guild.id, 'channel:reports', next(c.id for c in staff.text_channels if clean(c.name)=='reports'))
    # Preserve old public content, as well as manually created private guild areas.
    legacy = {'aion2-chat','market','choose-guild','server-guide','guild-recruitment',
        'guild-directory','guild-leaders','gvg-announcements','gvg-schedule','strategy','rosters','war room'}
    old = [c for c in guild.channels if not isinstance(c,discord.CategoryChannel)
        and clean(c.name).casefold() in legacy
        and c.category and clean(c.category.name).casefold() in {'start','start here','all','guild hub','pvp • gvg'}]
    if old:
        archive = await category(guild,'Archive',private_access(guild,[moderator]))
        for item in old:
            await item.edit(category=archive,sync_permissions=True,reason='Preserve legacy content in staff archive')
    for parent in list(guild.categories):
        if clean(parent.name).casefold() in {'guild hub','pvp • gvg'} and not parent.channels:
            await parent.delete(reason='Remove empty legacy category')
    await apply_settings(guild)
    await ensure_alert_channels(guild)
    await panel(await community.channel(guild, 'resources'), 'resources', embed=resource_embed())
    for number, embed in enumerate(starter_guides(), 1):
        await panel(await community.channel(guild, 'resources'), f'global-guide-{number}', embed=embed)
    roles = guild.get_channel(store.get(guild.id, 'channel:choose-roles'))
    await panel(roles, 'roles', content='﹒ 𝐂𝐡𝐨𝐨𝐬𝐞 𝐘𝐨𝐮𝐫 𝐑𝐨𝐥𝐞𝐬 ﹒\n\nChoose your class, interests and playstyle. Alerts are optional.\nChoose Event Pings if you want AION 2 timer and field-boss notifications.\nChoose News & Patches to opt into news, patch notes and hotfix pings.\nNo email verification or guild role is required to enter.\nLeader and Officer roles are assigned by administrators.', view=RolesView(community))
    await panel(staff_welcome, 'staff-welcome',
        content=f'﹒ 𝐖𝐞𝐥𝐜𝐨𝐦𝐞, 𝐒𝐭𝐚𝐟𝐟 ﹒\n\nThis private room is for AION 2 staff coordination.\nUse {staff_chat.mention} for day-to-day discussion and {reports.mention} for member issues.\nKeep public guidance and announcements in their public channels.\nNever request or share passwords, login codes, or bot tokens.')
    await panel(guild.get_channel(store.get(guild.id, 'channel:rules')), 'rules',
        content='﹒ 𝐑𝐮𝐥𝐞𝐬 ﹒\n\n1. Respect other members; no harassment, scams or spam.\n2. Keep announcements and role panels clear.\n3. Label Global/TW/KR and the version when sharing guides.\n4. Join runs you can attend; leave your signup if plans change.\n5. Never share passwords, login codes or bot tokens.\n6. Contact a moderator for disputes or reports.')
    print('BASIC SETUP COMPLETE', flush=True)


class AionClient(discord.Client):
    async def setup_hook(self):
        if MODE: return
        community.startup()
        target = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=target)
        synced = await tree.sync(guild=target)
        definitions = tree.get_commands(guild=None)
        try:
            tree.clear_commands(guild=None)
            await tree.sync()
        finally:
            for command in definitions: tree.add_command(command, guild=None, override=True)
        print(f'Synced {len(synced)} root slash commands to guild {GUILD_ID}', flush=True)


bot = AionClient(intents=discord.Intents.default(), activity=discord.Game(name='AION 2'), allowed_mentions=discord.AllowedMentions.none())
tree = app_commands.CommandTree(bot)
community = Community(bot, tree, store, GUILD_ID, role_for, operation_lock)
mode_done = False
mode_error = None


@bot.event
async def on_ready():
    global mode_done, mode_error
    print(f'Logged in as {bot.user} ({bot.user.id})', flush=True)
    if not MODE or mode_done: return
    mode_done = True
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild: raise ValueError('Configured Discord server is unavailable.')
        if MODE == '--inspect':
            info = dict(id=guild.id, name=guild.name, features=guild.features,
                bot_admin=guild.me.guild_permissions.administrator, verification=str(guild.verification_level),
                notifications=str(guild.default_notifications),
                channels=[dict(id=c.id,name=c.name,type=str(c.type),category=c.category_id,
                    public=c.permissions_for(guild.default_role).view_channel) for c in guild.channels],
                roles=[dict(id=r.id,name=r.name,position=r.position,managed=r.managed,permissions=r.permissions.value) for r in guild.roles])
            print(json.dumps(info, ensure_ascii=False), flush=True)
        elif MODE == '--alerts-setup':
            async with operation_lock: await alerts_setup(guild)
        elif MODE == '--apply':
            async with operation_lock: await base_setup(guild)
        elif MODE == '--rebuild':
            async with operation_lock: await rebuild(guild)
    except Exception as error:
        mode_error = error
        logging.exception('Maintenance failed')
    finally: await bot.close()


async def allowed(i, owner=False):
    if not await community.staff(i): return False
    if owner and i.user.id != i.guild.owner_id:
        await reply(i, 'Only the server owner can apply this operation.')
        return False
    return True


@tree.command(name='setupserver', description='Create or reconcile the Basic Global layout and settings')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def setupserver(i: discord.Interaction):
    if not await allowed(i, owner=True): return
    await i.response.defer(ephemeral=True)
    async with operation_lock: await base_setup(i.guild)
    await reply(i, 'Basic Global setup ready. No guild area was created. Use /help.')


@tree.command(name='serversettings', description='Owner: reapply ordinary entry and community permissions')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def serversettings(i: discord.Interaction):
    if not await allowed(i, owner=True): return
    await i.response.defer(ephemeral=True)
    async with operation_lock: await apply_settings(i.guild)
    await reply(i, 'No email verification; public entry; media filtering; mentions-only notifications; mass mentions disabled.')


async def rebuild(guild):
    if not guild.me.guild_permissions.administrator: raise ValueError('Bot needs Administrator for reset.')
    protected = {r.id for r in guild.me.roles}
    custom = [r for r in guild.roles if not r.is_default() and not r.managed and r.id not in protected]
    if any(r >= guild.me.top_role for r in custom): raise ValueError('Move the bot role above all custom roles before resetting.')
    channels = sorted(list(guild.channels), key=lambda c:isinstance(c, discord.CategoryChannel))
    progress = await guild.create_text_channel(text_name('reset-progress'), overwrites={
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True,send_messages=True)}, reason='Owner-authorized rebuild recovery log')
    store.close_deleted_channels(guild.id)
    try:
        await progress.send('Rebuilding Basic Global layout…')
        for channel in channels: await channel.delete(reason='Owner-authorized server reset')
        for role in sorted(custom, reverse=True): await role.delete(reason='Owner-authorized server reset')
        await base_setup(guild, progress)
        await progress.send('✅ Basic Global rebuild complete. Personal data preserved; old signups cancelled.')
    except Exception:
        await progress.send('Setup stopped. Fix permissions then use /setupserver; see service log for details.')
        raise


@tree.command(name='resetserver', description='Owner: ERASE all channels/messages and custom roles; rebuild Basic')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def resetserver(i: discord.Interaction, confirmation: str):
    if not await allowed(i, owner=True): return
    if confirmation != 'ERASE': raise ValueError('Type ERASE to delete all channels, messages and custom roles.')
    await reply(i, 'Reset started. Follow the private bot log. Personal bot data will be retained.')
    async with operation_lock: await rebuild(i.guild)


def guild_label(value):
    name = clean(value).title()
    if not name or len(name)>50: raise ValueError('Guild name must contain 1–50 characters.')
    return name


@tree.command(name='setupguild', description='Leader: create your own private guild area; admins can reconcile guilds')
@app_commands.guild_only()
async def setupguild(i: discord.Interaction, name: str):
    if not await community.usable(i): return
    admin = i.user.guild_permissions.administrator
    if not admin and not is_leader(i.user):
        raise ValueError('A Leader role or Administrator permission is required.')
    name = guild_label(name)
    await i.response.defer(ephemeral=True)
    async with operation_lock:
        actor = await i.guild.fetch_member(i.user.id)
        admin = actor.guild_permissions.administrator
        if not admin:
            if not is_leader(actor):
                raise ValueError('A Leader role or Administrator permission is required.')
            if led_guilds(actor):
                raise ValueError('Your guild is already set up. Staff handle layout repairs; use /guildmember for membership.')
            reserved = {'start','all','party','builds','staff','alerts','archive','guild leadership'}
            collisions = any(normal(c.name)==normal(name) for c in i.guild.categories)
            collisions |= any(role_for(i.guild, f'{name} {rank}') for rank in ['Leader','Officer','Member'])
            collisions |= any(normal(c.name).replace(' ','-').startswith(normal(name).replace(' ','-')+'-') for c in i.guild.channels)
            if normal(name) in reserved or collisions:
                raise ValueError('That name is already in use or reserved. Existing channels and roles were left unchanged.')
        roles = [await ensure_role(i.guild, f'{name} {rank}') for rank in ['Leader','Officer','Member']]
        parent = await category(i.guild,name,private_access(i.guild, roles+[role_for(i.guild,'Moderator')]))
        for suffix in ['announcements','chat','events','builds','roster']:
            overrides = None
            if suffix=='announcements':
                overrides = private_access(i.guild,roles+[role_for(i.guild,'Moderator')])
                overrides[roles[2]].send_messages=False
            await text(i.guild,parent,f'{name}-{suffix}',overrides)
        await voice(i.guild,parent,f'{name} Lobby')
        await voice(i.guild,parent,f'{name} Raid')
        if not admin:
            await actor.add_roles(roles[0], reason='Leader created their own private guild')
    await reply(i,f'Private {name} area ready. Assign membership with /guildmember.')


@tree.command(name='guildmember', description='Leader: assign Officer or Member in your own guild')
@app_commands.guild_only()
@app_commands.choices(rank=[app_commands.Choice(name=x,value=x) for x in ['Leader','Officer','Member']])
async def guildmember(i: discord.Interaction, member: discord.Member, guild_name: str, rank: str):
    if not await community.usable(i): return
    name=guild_label(guild_name)
    if not i.user.guild_permissions.administrator: name=own_guild(i.user,name)
    check_membership(i.user, member, name, rank)
    role=role_for(i.guild,f'{name} {rank}')
    if not role: raise ValueError('Create this guild with /setupguild first.')
    await i.response.defer(ephemeral=True)
    async with operation_lock:
        actor = await i.guild.fetch_member(i.user.id)
        member = await i.guild.fetch_member(member.id)
        check_membership(actor, member, name, rank)
        await member.add_roles(role,reason='Administrator assigned private guild rank')
        old=[r for r in member.roles if r!=role and clean(r.name).casefold() in {f'{name} {x}'.casefold() for x in ['Leader','Officer','Member']}]
        if old: await member.remove_roles(*old,reason='Guild rank update')
    await reply(i,'Guild rank assigned.')


@tree.command(name='guildremove', description='Leader: remove ordinary members from your own guild')
@app_commands.guild_only()
async def guildremove(i: discord.Interaction, member: discord.Member, guild_name: str):
    if not await community.usable(i): return
    name=guild_label(guild_name)
    if not i.user.guild_permissions.administrator: name=own_guild(i.user,name)
    check_membership(i.user, member, name)
    roles=[r for r in member.roles if clean(r.name).casefold() in {f'{name} {x}'.casefold() for x in ['Leader','Officer','Member']}]
    await i.response.defer(ephemeral=True)
    async with operation_lock:
        actor = await i.guild.fetch_member(i.user.id)
        member = await i.guild.fetch_member(member.id)
        check_membership(actor, member, name)
        if roles: await member.remove_roles(*roles,reason='Administrator removed private guild membership')
    await reply(i,'Guild ranks removed.')


@tree.error
async def command_error(i, error):
    original = getattr(error,'original',error)
    logging.error('Command error: %r',original)
    if isinstance(original,ValueError): message=str(original)
    elif isinstance(original,app_commands.CommandOnCooldown): message=f'Try again in {original.retry_after:.0f} seconds.'
    elif isinstance(original,discord.Forbidden): message='Bot permission is missing. Check channel permissions and role hierarchy.'
    else: message='Operation failed. Saved data is retained. Check the service log or contact an administrator.'
    try: await reply(i,message)
    except discord.HTTPException: pass


if __name__=='__main__':
    bot.run(TOKEN)
    if mode_error: raise SystemExit(1)
