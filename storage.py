"""Persistent community state. No game credentials or scraping."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc


def parse_time(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Use a date with timezone: 2026-10-10T20:00+02:00')
    if date.tzinfo is None:
        raise ValueError('Include your timezone offset, for example +02:00 or Z.')
    return int(date.timestamp())


def period_key(cadence, hour, weekday, now=None):
    """UTC reset boundary, never dependent on the computer's local timezone."""
    if cadence == 'once':
        return 'once'
    if hour is None:
        raise ValueError('An administrator must configure /resetclock first.')
    now = now or datetime.now(UTC)
    anchor = now.astimezone(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    if anchor > now:
        anchor -= timedelta(days=1)
    if cadence == 'weekly':
        anchor -= timedelta(days=(anchor.weekday() - weekday) % 7)
    return anchor.isoformat()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (guild INTEGER, key TEXT, value TEXT,
                PRIMARY KEY(guild,key));
            CREATE TABLE IF NOT EXISTS profiles (guild INTEGER, user INTEGER, alias TEXT,
                data TEXT, PRIMARY KEY(guild,user,alias));
            CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, guild INTEGER,
                owner INTEGER, channel INTEGER, message INTEGER, kind TEXT, data TEXT,
                status TEXT DEFAULT 'open', reminded INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS signups (post INTEGER REFERENCES posts(id) ON DELETE CASCADE,
                user INTEGER, state TEXT, position INTEGER,
                PRIMARY KEY(post,user));
            CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, guild INTEGER,
                user INTEGER, label TEXT, cadence TEXT, scope TEXT, target TEXT);
            CREATE TABLE IF NOT EXISTS checks (task INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                period TEXT, PRIMARY KEY(task,period));
            CREATE TABLE IF NOT EXISTS goals (guild INTEGER, user INTEGER, character TEXT,
                target TEXT, step TEXT, link TEXT, PRIMARY KEY(guild,user,character));
        ''')
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.db:
            yield self.db

    def get(self, guild, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE guild=? AND key=?', (guild, key)).fetchone()
        return json.loads(row['value']) if row else default

    def set(self, guild, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?)',
                            (guild, key, json.dumps(value)))

    def profile(self, guild, user, alias, data=None):
        alias = alias.strip().casefold()
        if data is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)',
                                (guild, user, alias, json.dumps(data)))
        row = self.db.execute('SELECT data FROM profiles WHERE guild=? AND user=? AND alias=?',
                              (guild, user, alias)).fetchone()
        return json.loads(row['data']) if row else None

    def create_post(self, guild, owner, kind, data):
        with self.db:
            cur = self.db.execute('INSERT INTO posts(guild,owner,kind,data) VALUES(?,?,?,?)',
                                  (guild, owner, kind, json.dumps(data)))
        return cur.lastrowid

    def post(self, ident):
        row = self.db.execute('SELECT * FROM posts WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ValueError('This post no longer exists.')
        result = dict(row)
        result['data'] = json.loads(result['data'])
        return result

    def update_post(self, ident, **changes):
        if set(changes) - {'channel', 'message', 'data', 'status', 'reminded'}:
            raise ValueError('Unsupported post update')
        if 'data' in changes:
            changes['data'] = json.dumps(changes['data'])
        with self.db:
            self.db.execute('UPDATE posts SET ' + ','.join(k + '=?' for k in changes) + ' WHERE id=?',
                            (*changes.values(), ident))

    def members(self, ident):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM signups WHERE post=? ORDER BY position,user', (ident,))]

    def signup(self, ident, user, action):
        """Atomic capacity check, FIFO waitlist and promotion."""
        with self.transaction() as db:
            post = self.post(ident)
            if post['status'] != 'open':
                raise ValueError('Signups are closed.')
            rows = self.members(ident)
            if action == 'leave':
                db.execute('DELETE FROM signups WHERE post=? AND user=?', (ident, user))
            elif action in {'join', 'reserve'}:
                if any(row['user'] == user for row in rows):
                    raise ValueError('You are already signed up. Leave before changing your choice.')
                state = 'reserve'
                if action == 'join' and sum(r['state'] == 'joined' for r in rows) < post['data']['capacity']:
                    state = 'joined'
                db.execute('INSERT INTO signups VALUES(?,?,?,?)',
                           (ident, user, state, max((r['position'] for r in rows), default=0) + 1))
            else:
                raise ValueError('Unknown signup action')
            rows = self.members(ident)
            spaces = post['data']['capacity'] - sum(r['state'] == 'joined' for r in rows)
            # Explicit Reserve is also a FIFO waiting list; leaving a place promotes it.
            for row in [r for r in rows if r['state'] == 'reserve'][:max(0, spaces)]:
                if action != 'reserve':
                    db.execute("UPDATE signups SET state='joined' WHERE post=? AND user=?", (ident, row['user']))
        return self.members(ident)

    def add_task(self, guild, user, label, cadence, scope, target):
        target = target.strip().casefold()
        if scope == 'account':
            target = 'account'
        if not target:
            raise ValueError('Specify a character alias or region/server for this scope.')
        with self.db:
            exists = self.db.execute('SELECT id FROM tasks WHERE guild=? AND user=? AND label=? AND cadence=? AND scope=? AND target=?',
                                    (guild, user, label, cadence, scope, target)).fetchone()
            if exists:
                return exists['id']
            return self.db.execute('INSERT INTO tasks(guild,user,label,cadence,scope,target) VALUES(?,?,?,?,?,?)',
                                   (guild, user, label, cadence, scope, target)).lastrowid

    def tasks(self, guild, user):
        return [dict(r) for r in self.db.execute('SELECT * FROM tasks WHERE guild=? AND user=? ORDER BY id', (guild, user))]

    def check(self, guild, user, ident, done=True, now=None):
        task = next((t for t in self.tasks(guild, user) if t['id'] == ident), None)
        if task is None:
            raise ValueError('Task not found in your personal checklist.')
        key = period_key(task['cadence'], self.get(guild, 'reset_hour'), self.get(guild, 'reset_day', 2), now)
        with self.db:
            if done:
                self.db.execute('INSERT OR IGNORE INTO checks VALUES(?,?)', (ident, key))
            else:
                self.db.execute('DELETE FROM checks WHERE task=? AND period=?', (ident, key))
        return key

    def is_done(self, task, now=None):
        key = period_key(task['cadence'], self.get(task['guild'], 'reset_hour'), self.get(task['guild'], 'reset_day', 2), now)
        return bool(self.db.execute('SELECT 1 FROM checks WHERE task=? AND period=?', (task['id'], key)).fetchone())

    def close_deleted_channels(self, guild):
        with self.db:
            self.db.execute("UPDATE posts SET status='cancelled' WHERE guild=? AND status='open'", (guild,))
            self.db.execute("DELETE FROM settings WHERE guild=? AND (key LIKE 'channel:%' OR key LIKE 'panel:%')", (guild,))
