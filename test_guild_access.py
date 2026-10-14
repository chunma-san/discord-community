import unittest
from types import SimpleNamespace as NS
from guild_access import is_leader, own_guild, check_membership, guild_channel

class Role:
    def __init__(self, name, value=0, position=1):
        self.name=name; self.managed=False; self.permissions=NS(value=value); self.position=position
    def __ge__(self, other):return self.position>=other.position

class GuildAccessTests(unittest.TestCase):
    def setUp(self):
        self.roles=[Role(n) for n in ['Leader','Alpha Leader','Alpha Officer','Alpha Member','Beta Leader','Beta Officer','Beta Member']]
        self.guild=NS(roles=self.roles,owner_id=99,me=NS(top_role=Role('bot',position=10)),text_channels=[])
        self.alpha=self.member(1,['Leader','Alpha Leader'])
        self.target=self.member(2,['Alpha Member'])
    def member(self, ident, names, admin=False):
        return NS(id=ident,guild=self.guild,roles=[r for r in self.roles if r.name in names],guild_permissions=NS(administrator=admin))
    def test_generic_leader_cannot_manage_unassigned_guild(self):
        m=self.member(3,['Leader']);self.assertTrue(is_leader(m))
        with self.assertRaises(ValueError):own_guild(m,'Alpha')
    def test_leader_infers_own_guild_and_cannot_select_other(self):
        self.assertEqual(own_guild(self.alpha),'Alpha')
        with self.assertRaises(ValueError):own_guild(self.alpha,'Beta')
    def test_leader_can_assign_member_but_cannot_promote_leader(self):
        check_membership(self.alpha,self.target,'Alpha','Member')
        with self.assertRaises(ValueError):check_membership(self.alpha,self.target,'Alpha','Leader')
    def test_leader_cannot_remove_leader_or_administrator(self):
        with self.assertRaises(ValueError):check_membership(self.alpha,self.alpha,'Alpha')
        with self.assertRaises(ValueError):check_membership(self.alpha,self.member(4,[],True),'Alpha')
    def test_permission_bearing_guild_role_rejected(self):
        next(r for r in self.roles if r.name=='Alpha Officer').permissions.value=8
        with self.assertRaises(ValueError):check_membership(self.alpha,self.target,'Alpha','Officer')
    def test_administrator_retains_cross_guild_access(self):
        check_membership(self.member(4,[],True),self.target,'Beta','Leader')
    def test_announcement_requires_matching_private_category(self):
        fake=NS(name='Alpha-announcements',category=NS(name='Beta'))
        self.guild.text_channels=[fake]
        with self.assertRaises(ValueError):guild_channel(self.guild,'Alpha','announcements')
        real=NS(name='﹒alpha-announcements﹒',category=NS(name='Alpha'))
        self.guild.text_channels.append(real)
        self.assertIs(guild_channel(self.guild,'Alpha','announcements'),real)

if __name__=='__main__':unittest.main()
