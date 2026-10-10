"""SQLite model and Telegram authentication. No network or environment access."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

class Problem(Exception):
    def __init__(self, text, status=400):
        super().__init__(text)
        self.status = status

def telegram_identity(raw, token, now=None):
    if not raw or len(raw) > 16000:
        raise Problem('Откройте приложение через своего Telegram-бота.', 401)
    pairs = parse_qsl(raw, keep_blank_values=True)
    if len(dict(pairs)) != len(pairs):
        raise Problem('Некорректные данные входа.', 401)
    data = dict(pairs)
    supplied = data.pop('hash', '')
    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise Problem('Не удалось проверить вход через Telegram.', 401)
    try:
        auth_date = int(data['auth_date'])
        clock = time.time() if now is None else now
        if not -30 <= clock - auth_date <= 86400:
            raise ValueError()
        user = json.loads(data['user'])
        uid = user['id']
        if type(uid) is not int or uid <= 0:
            raise ValueError()
        name = ' '.join(str(user.get(k, '')) for k in ('first_name','last_name')).strip()[:100]
        return {'id':uid, 'name':name or 'Участник'}
    except (KeyError, ValueError, TypeError):
        raise Problem('Вход устарел. Закройте и заново откройте Mini App.', 401)

def validate_plan(payload):
    try:
        name = payload['name'].strip()
        start = date.fromisoformat(payload['start'])
        days = payload['days']
        tasks = payload['tasks']
        if not name or len(name) > 80 or type(days) is not int or not 1 <= days <= 366 or not 1 <= len(tasks) <= 8:
            raise ValueError()
        clean = []
        for t in tasks:
            title = t['title'].strip()
            description = t.get('description','').strip()
            hour = datetime.strptime(t['time'], '%H:%M').strftime('%H:%M')
            unit = t['unit']
            options = t['options']
            if not title or len(title) > 80 or len(description) > 700 or unit not in ('раз','мин','сек','шагов'):
                raise ValueError()
            if not isinstance(options, list) or not 1 <= len(options) <= 5 or any(type(x) is not int or not 1 <= x <= 100000 for x in options) or len(set(options)) != len(options):
                raise ValueError()
            clean.append({'title':title,'description':description,'time':hour,'unit':unit,'options':sorted(options)})
        return {'name':name,'start':start.isoformat(),'days':days,'tasks':clean}
    except (KeyError,ValueError,TypeError,AttributeError):
        raise Problem('Проверьте название, дату, длительность и упражнения. Значения должны быть положительными целыми числами.')

class Store:
    def __init__(self, path, timezone='Europe/Moscow'):
        self.tz = ZoneInfo(timezone)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, public_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL, subscribed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS challenges(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, start TEXT NOT NULL, days INTEGER NOT NULL, invite TEXT UNIQUE NOT NULL, paused INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, challenge INTEGER NOT NULL REFERENCES challenges(id), title TEXT NOT NULL, description TEXT NOT NULL, hour TEXT NOT NULL, unit TEXT NOT NULL, options TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_tasks_challenge ON tasks(challenge);
        CREATE TABLE IF NOT EXISTS members(challenge INTEGER NOT NULL REFERENCES challenges(id), user INTEGER NOT NULL REFERENCES users(id), joined TEXT NOT NULL, PRIMARY KEY(challenge,user));
        CREATE TABLE IF NOT EXISTS results(challenge INTEGER NOT NULL, user INTEGER NOT NULL, day TEXT NOT NULL, task INTEGER NOT NULL REFERENCES tasks(id), amount INTEGER NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(challenge,user,day,task), FOREIGN KEY(challenge,user) REFERENCES members(challenge,user));
        CREATE TABLE IF NOT EXISTS deliveries(challenge INTEGER NOT NULL, user INTEGER NOT NULL, day TEXT NOT NULL, task INTEGER NOT NULL, state TEXT NOT NULL, claimed REAL NOT NULL, PRIMARY KEY(challenge,user,day,task));
        ''')
    def now(self):
        return datetime.now(self.tz)
    def user(self, identity, subscribe=None):
        with self.lock, self.db:
            self.db.execute('INSERT INTO users(id,public_id,name) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (identity['id'], secrets.token_hex(12), identity['name']))
            if subscribe is not None:
                self.db.execute('UPDATE users SET subscribed=? WHERE id=?',(int(subscribe),identity['id']))
            return dict(self.db.execute('SELECT * FROM users WHERE id=?',(identity['id'],)).fetchone())
    def current(self):
        with self.lock:
            c = self.db.execute('SELECT * FROM challenges ORDER BY id DESC LIMIT 1').fetchone()
            if not c:
                return None
            c = dict(c)
            c['tasks'] = []
            for row in self.db.execute('SELECT * FROM tasks WHERE challenge=? ORDER BY hour,id',(c['id'],)):
                t = dict(row)
                t['time'] = t.pop('hour')
                t['options'] = json.loads(t['options'])
                c['tasks'].append(t)
            return c
    def create(self, payload):
        plan = validate_plan(payload)
        if date.fromisoformat(plan['start']) < self.now().date():
            raise Problem('Дата старта должна быть сегодня или в будущем.')
        with self.lock, self.db:
            cid = self.db.execute('INSERT INTO challenges(name,start,days,invite) VALUES(?,?,?,?)',(plan['name'],plan['start'],plan['days'],secrets.token_urlsafe(18))).lastrowid
            for t in plan['tasks']:
                self.db.execute('INSERT INTO tasks(challenge,title,description,hour,unit,options) VALUES(?,?,?,?,?,?)',(cid,t['title'],t['description'],t['time'],t['unit'],json.dumps(t['options'])))
        return self.current()
    def join(self, uid, cid, invite, consent):
        with self.lock, self.db:
            c = self.current()
            if not c or c['id'] != cid or not hmac.compare_digest(str(invite), c['invite']):
                raise Problem('Неверный код приглашения. Попросите организатора прислать актуальный код.',403)
            if consent is not True:
                raise Problem('Подтвердите, что результаты будут видны участникам.')
            end = date.fromisoformat(c['start']) + timedelta(days=c['days'])
            if self.now().date() >= end:
                raise Problem('Этот челлендж уже закончился.')
            self.db.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(cid,uid,self.now().date().isoformat()))
    def is_member(self, uid, cid):
        with self.lock:
            return bool(self.db.execute('SELECT 1 FROM members WHERE challenge=? AND user=?',(cid,uid)).fetchone())
    def save_result(self, uid, cid, task_id, day, amount):
        with self.lock, self.db:
            c = self.current()
            if not c or c['id'] != cid or not self.is_member(uid,cid):
                raise Problem('Сначала вступите в актуальный челлендж.',403)
            now = self.now()
            if c['paused']:
                raise Problem('Организатор приостановил челлендж.')
            if day != now.date().isoformat() or not (date.fromisoformat(c['start']) <= now.date() < date.fromisoformat(c['start']) + timedelta(days=c['days'])):
                raise Problem('Отмечать упражнения можно только за сегодня в дни челленджа.')
            task = next((t for t in c['tasks'] if t['id'] == task_id),None)
            if not task or type(amount) is not int or amount not in task['options']:
                raise Problem('Выберите один из предложенных результатов.')
            self.db.execute('INSERT INTO results VALUES(?,?,?,?,?,?) ON CONFLICT(challenge,user,day,task) DO UPDATE SET amount=excluded.amount,updated=excluded.updated',(cid,uid,day,task_id,amount,now.isoformat()))
    def clear_result(self, uid, cid, task_id, day):
        with self.lock, self.db:
            c = self.current()
            if not c or c['id'] != cid or not self.is_member(uid,cid) or day != self.now().date().isoformat() or c['paused']:
                raise Problem('Изменять отметки можно только за сегодня в текущем челлендже.')
            self.db.execute('DELETE FROM results WHERE challenge=? AND user=? AND day=? AND task=?',(cid,uid,day,task_id))
    def summary(self, uid, c):
        with self.lock:
            joined = self.db.execute('SELECT joined FROM members WHERE challenge=? AND user=?',(c['id'],uid)).fetchone()['joined']
            rows = [dict(r) for r in self.db.execute('SELECT day,task,amount FROM results WHERE challenge=? AND user=? ORDER BY day,task',(c['id'],uid))]
            now = self.now().date()
            calendar = []
            start = date.fromisoformat(c['start'])
            full = partial = missed = 0
            for i in range(c['days']):
                day = start + timedelta(days=i)
                selected = [r for r in rows if r['day']==day.isoformat()]
                count = len(selected)
                if day.isoformat() < joined:
                    status = 'before_join'
                elif day > now:
                    status = 'future'
                elif count == len(c['tasks']):
                    status = 'complete'; full += 1
                elif count:
                    status = 'partial'; partial += 1
                elif day == now:
                    status = 'pending'
                else:
                    status = 'missed'; missed += 1
                calendar.append({'date':day.isoformat(),'status':status,'done':count,'total':len(c['tasks'])})
            totals = [{'task':t['id'],'title':t['title'],'unit':t['unit'],'amount':sum(r['amount'] for r in rows if r['task']==t['id'])} for t in c['tasks']]
            return {'calendar':calendar,'results':rows,'complete':full,'partial':partial,'missed':missed,'workouts':len(rows),'totals':totals,'joined':joined}
    def state(self, uid, admin=False):
        with self.lock:
            user = dict(self.db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone())
            c = self.current()
            response = {'user':{'name':user['name'],'public_id':user['public_id'],'subscribed':bool(user['subscribed']),'admin':admin},'today':self.now().date().isoformat(),'timezone':str(self.tz),'challenge':None,'joined':False,'team':[]}
            if not c:
                return response
            member = self.is_member(uid,c['id'])
            response['joined'] = member
            response['challenge'] = {k:v for k,v in c.items() if k!='invite'}
            if admin:
                response['challenge']['invite'] = c['invite']
            if member:
                response['progress'] = self.summary(uid,c)
            if member or admin:
                for row in self.db.execute('SELECT users.* FROM members JOIN users ON users.id=members.user WHERE members.challenge=? ORDER BY name',(c['id'],)).fetchall():
                    response['team'].append({'name':row['name'],'public_id':row['public_id'],'progress':self.summary(row['id'],c)})
            return response
    def pause(self, cid, paused):
        if type(paused) is not bool:
            raise Problem('Некорректное состояние.')
        with self.lock, self.db:
            c = self.current()
            if not c or cid != c['id']:
                raise Problem('Челлендж не найден.')
            self.db.execute('UPDATE challenges SET paused=? WHERE id=?',(int(paused),cid))
    def set_reminder_time(self, cid, hour):
        try:
            hour=datetime.strptime(hour,'%H:%M').strftime('%H:%M')
        except (ValueError,TypeError):
            raise Problem('Укажите время общей рассылки.')
        with self.lock,self.db:
            c=self.current()
            if not c or cid != c['id']:
                raise Problem('Челлендж не найден.')
            self.db.execute('UPDATE tasks SET hour=? WHERE challenge=?',(hour,cid))

    def due(self):
        """Lease deliveries so restart/retry does not normally duplicate messages."""
        with self.lock, self.db:
            c = self.current()
            now = self.now()
            if not c or c['paused'] or not (date.fromisoformat(c['start']) <= now.date() < date.fromisoformat(c['start'])+timedelta(days=c['days'])):
                return []
            jobs=[]
            if not c['tasks'] or min(t['time'] for t in c['tasks']) > now.strftime('%H:%M'):
                return []
            users=self.db.execute('SELECT u.id FROM users u JOIN members m ON m.user=u.id WHERE m.challenge=? AND u.subscribed=1',(c['id'],)).fetchall()
            for u in users:
                key=(c['id'],u['id'],now.date().isoformat(),0)
                # Old per-exercise sends also count, avoiding an extra send on upgrade day.
                if self.db.execute("SELECT 1 FROM deliveries WHERE challenge=? AND user=? AND day=? AND state='sent'",key[:3]).fetchone():
                    continue
                done=self.db.execute('SELECT COUNT(*) FROM results WHERE challenge=? AND user=? AND day=?',key[:3]).fetchone()[0]
                if done == len(c['tasks']):
                    continue
                sent=self.db.execute('SELECT state,claimed FROM deliveries WHERE challenge=? AND user=? AND day=? AND task=?',key).fetchone()
                if sent and now.timestamp()-sent['claimed'] < 300:
                    continue
                self.db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?) ON CONFLICT(challenge,user,day,task) DO UPDATE SET state=excluded.state,claimed=excluded.claimed',(*key,'sending',now.timestamp()))
                jobs.append({'key':key,'challenge':c})
            return jobs
    def delivered(self, key):
        with self.lock, self.db:
            self.db.execute('UPDATE deliveries SET state=? WHERE challenge=? AND user=? AND day=? AND task=?',('sent',*key))
