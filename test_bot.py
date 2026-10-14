import asyncio
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from storage import Store, parse_time, period_key


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'test.sqlite3')
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def post(self, capacity=2):
        return self.store.create_post(1, 10, 'lfg', {'capacity': capacity, 'title':'Test'})

    def test_capacity_waitlist_and_promotion(self):
        p = self.post()
        for user in [10,11,12,13]: self.store.signup(p,user,'join')
        rows = self.store.members(p)
        self.assertEqual([r['state'] for r in rows],['joined','joined','reserve','reserve'])
        self.store.signup(p,10,'leave')
        rows = self.store.members(p)
        self.assertEqual([r['user'] for r in rows if r['state']=='joined'],[11,12])
        self.assertEqual([r['user'] for r in rows if r['state']=='reserve'],[13])

    def test_double_join_is_rejected(self):
        p=self.post()
        self.store.signup(p,10,'join')
        with self.assertRaises(ValueError): self.store.signup(p,10,'join')
        self.assertEqual(len(self.store.members(p)),1)

    def test_reserve_does_not_take_free_place_immediately(self):
        p=self.post()
        self.store.signup(p,10,'reserve')
        self.assertEqual(self.store.members(p)[0]['state'],'reserve')

    def test_cancelled_post_cannot_join(self):
        p=self.post()
        self.store.update_post(p,status='cancelled')
        with self.assertRaises(ValueError): self.store.signup(p,10,'join')

    def test_restart_preserves_profiles_signups_and_settings(self):
        p=self.post()
        self.store.signup(p,10,'join')
        self.store.profile(1,10,'Main',{'level':45})
        self.store.set(1,'reset_hour',7)
        second=Store(self.path)
        try:
            self.assertEqual(second.profile(1,10,'MAIN')['level'],45)
            self.assertEqual(second.get(1,'reset_hour'),7)
            self.assertEqual(second.members(p)[0]['user'],10)
        finally: second.db.close()

    def test_resource_scopes_do_not_duplicate_per_alt(self):
        a=self.store.add_task(1,10,'Weekly','weekly','account','alt1')
        b=self.store.add_task(1,10,'Weekly','weekly','account','alt2')
        self.assertEqual(a,b)
        x=self.store.add_task(1,10,'Daily','daily','character','alt1')
        y=self.store.add_task(1,10,'Daily','daily','character','alt2')
        self.assertNotEqual(x,y)

    def test_task_cannot_be_marked_by_other_member(self):
        task=self.store.add_task(1,10,'Cube route','once','character','main')
        with self.assertRaises(ValueError): self.store.check(1,11,task)
        self.store.check(1,10,task)
        self.assertTrue(self.store.is_done(self.store.tasks(1,10)[0]))

    def test_unverified_reset_is_not_assumed(self):
        task=self.store.add_task(1,10,'Daily','daily','character','main')
        with self.assertRaises(ValueError): self.store.check(1,10,task)

    def test_daily_boundary_and_undo(self):
        self.store.set(1,'reset_hour',7)
        task=self.store.add_task(1,10,'Daily','daily','character','main')
        before=datetime(2026,10,4,6,59,tzinfo=timezone.utc)
        after=datetime(2026,10,4,7,0,tzinfo=timezone.utc)
        self.store.check(1,10,task,now=before)
        row=self.store.tasks(1,10)[0]
        self.assertTrue(self.store.is_done(row, before))
        self.assertFalse(self.store.is_done(row, after))
        self.store.check(1,10,task,False,now=before)
        self.assertFalse(self.store.is_done(row,before))

    def test_weekly_boundary(self):
        before=datetime(2026,10,7,6,59,tzinfo=timezone.utc)
        after=datetime(2026,10,7,7,0,tzinfo=timezone.utc)
        self.assertNotEqual(period_key('weekly',7,2,before),period_key('weekly',7,2,after))
        self.assertEqual(period_key('weekly',7,2,after),period_key('weekly',7,2,datetime(2026,10,13,20,tzinfo=timezone.utc)))

    def test_timezone_required_and_equivalent(self):
        with self.assertRaises(ValueError): parse_time('2026-10-10T20:00')
        self.assertEqual(parse_time('2026-10-10T20:00+02:00'),parse_time('2026-10-10T18:00Z'))

    def test_rebuild_cancels_old_posts_but_preserves_personal_data(self):
        p=self.post()
        self.store.profile(1,10,'main',{'level':45})
        self.store.set(1,'channel:lfg',123)
        self.store.close_deleted_channels(1)
        self.assertEqual(self.store.post(p)['status'],'cancelled')
        self.assertIsNone(self.store.get(1,'channel:lfg'))
        self.assertIsNotNone(self.store.profile(1,10,'main'))


class DiscordRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_and_persistent_views(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ['DISCORD_TOKEN']='offline-test'
            os.environ['DISCORD_GUILD_ID']='1'
            os.environ['AION_DATA_PATH']=str(Path(directory)/'bot.sqlite3')
            app=importlib.import_module('bot')
            from community import RolesView, SignupView, url
            with self.assertRaises(ValueError): url('javascript:alert(1)')
            self.assertTrue(RolesView(app.community).is_persistent())
            self.assertTrue(SignupView(app.community,1).is_persistent())
            commands=app.tree.get_commands()
            self.assertGreaterEqual(len(commands),20)
            payload=[c.to_dict(app.tree) for c in commands]
            self.assertEqual(len({c['name'] for c in payload}),len(payload))
            self.assertTrue(all(len(c.get('options',[]))<=25 for c in payload))
            ident=app.store.create_post(1,10,'event',dict(title='Test',capacity=10,region='Europe',server='Example Realm',
                faction='Asmodian',activity='Training',difficulty='Normal',mode='Training',notes='Test',start=1800000000))
            for user in range(10,20): app.store.signup(ident,user,'join')
            self.assertLess(len(app.community.embed(ident)),6000)
            await app.bot.close()
            app.store.db.close()


if __name__=='__main__': unittest.main()
