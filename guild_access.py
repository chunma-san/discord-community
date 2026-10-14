import unicodedata

def normal(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).strip(' ﹒.').casefold()

def role_named(guild, name):
    matches = [r for r in guild.roles if normal(r.name) == normal(name) and not r.managed]
    if len(matches) > 1:
        raise ValueError('Duplicate guild role names need staff attention.')
    return matches[0] if matches else None

def led_guilds(member):
    names = []
    for role in member.roles:
        label = unicodedata.normalize('NFKC', role.name).strip(' ﹒.')
        if label.casefold().endswith(' leader') and not role.managed:
            name = label[:-7].strip()
            if all(role_named(member.guild, name+' '+rank) for rank in ['Leader','Officer','Member']):
                names.append(name)
    return list(dict.fromkeys(names))

def is_leader(member):
    return bool(led_guilds(member)) or any(normal(r.name)=='leader' and not r.managed for r in member.roles)

def own_guild(member, requested=''):
    names = led_guilds(member)
    if requested:
        match = next((n for n in names if normal(n)==normal(requested)), None)
        if match: return match
    elif len(names)==1:
        return names[0]
    raise ValueError('You can manage only your assigned guild. Staff must assign its Guild Leader role first.')

def check_membership(member, target, name, rank=None):
    if member.guild_permissions.administrator: return
    own_guild(member, name)
    if rank is not None:
        role = role_named(member.guild, name+' '+rank)
        if role is None or role.managed or role.permissions.value or role >= member.guild.me.top_role:
            raise ValueError('This guild rank must be permission-free and below the bot.')
    if rank == 'Leader':
        raise ValueError('Only administrators can assign or transfer the Leader rank.')
    leader = role_named(member.guild, name+' Leader')
    if target.id == member.guild.owner_id or target.guild_permissions.administrator or (leader and leader in target.roles):
        raise ValueError('Only administrators can change a guild leader or server staff membership.')

def guild_channel(guild, name, suffix):
    matches=[c for c in guild.text_channels if normal(c.name).replace(' ','-')==normal(name+'-'+suffix).replace(' ','-')
             and c.category and normal(c.category.name)==normal(name)]
    if len(matches)!=1:
        raise ValueError('The guild channel needs staff attention; no server layout was changed.')
    return matches[0]
