import hashlib
import hmac
import json
import threading
import time
import unittest
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo
from domain import Store, Problem, telegram_identity

TOKEN='test-token-no-real-credential'
def signed(uid=1, stamp=None):
    d={'auth_date':str(int(time.time()) if stamp is None else stamp),'user':json.dumps({'id':uid,'first_name':f'User {uid}'},separators=(',',':'))}
    key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    d['hash']=hmac.new(key,'\n'.join(f'{k}={v}' for k,v in sorted(d.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(d)

def plan():
    return {'name':'Test challenge','start':'2026-10-06','days':3,'tasks':[{'title':'Pushups','description':'Choose','time':'09:00','unit':'раз','options':[10,20,30]},{'title':'Stretch','description':'Choose','time':'19:00','unit':'мин','options':[5,10]}]}

class DomainTests(unittest.TestCase):
    def setUp(self):
        self.s=Store(':memory:')
        self.clock=datetime(2026,10,6,10,0,tzinfo=ZoneInfo('Europe/Moscow'))
        self.s.now=lambda:self.clock
        self.s.user({'id':1,'name':'One'},True)
        self.c=self.s.create(plan())
        self.s.join(1,self.c['id'],self.c['invite'],True)
    def tearDown(self):
        self.s.db.close()
    def save(self,tid,amount):
        self.s.save_result(1,self.c['id'],tid,self.clock.date().isoformat(),amount)
    def test_partial_full_and_replacement(self):
        a,b=self.c['tasks']
        self.save(a['id'],10)
        self.save(a['id'],30)
        p=self.s.summary(1,self.c)
        self.assertEqual(p['workouts'],1)
        self.assertEqual(p['totals'][0]['amount'],30)
        self.assertEqual(p['calendar'][0]['status'],'partial')
        self.save(b['id'],5)
        self.assertEqual(self.s.summary(1,self.c)['calendar'][0]['status'],'complete')
        self.s.clear_result(1,self.c['id'],b['id'],'2026-10-06')
        self.assertEqual(self.s.summary(1,self.c)['calendar'][0]['status'],'partial')
    def test_day_rollover_and_future(self):
        p=self.s.summary(1,self.c)
        self.assertEqual([x['status'] for x in p['calendar']],['pending','future','future'])
        self.clock=datetime(2026,10,7,0,1,tzinfo=self.s.tz)
        self.assertEqual([x['status'] for x in self.s.summary(1,self.c)['calendar']],['missed','pending','future'])
    def test_late_join(self):
        self.clock=datetime(2026,10,7,10,0,tzinfo=self.s.tz)
        self.s.user({'id':2,'name':'Two'},True)
        self.s.join(2,self.c['id'],self.c['invite'],True)
        self.assertEqual(self.s.summary(2,self.c)['calendar'][0]['status'],'before_join')
    def test_membership_and_consent(self):
        self.s.user({'id':2,'name':'Two'},True)
        self.assertEqual(self.s.state(2)['team'],[])
        self.assertNotIn('invite',self.s.state(2)['challenge'])
        for code,consent in [('wrong',True),(self.c['invite'],False)]:
            with self.assertRaises(Problem):self.s.join(2,self.c['id'],code,consent)
        with self.assertRaises(Problem):self.s.save_result(2,self.c['id'],self.c['tasks'][0]['id'],'2026-10-06',10)
    def test_invalid_amount_day_and_old_challenge(self):
        for day,amount in [('2026-10-05',10),('2026-10-07',10),('2026-10-06',99),('2026-10-06',True)]:
            with self.assertRaises(Problem):self.s.save_result(1,self.c['id'],self.c['tasks'][0]['id'],day,amount)
        self.s.create(plan())
        with self.assertRaises(Problem):self.save(self.c['tasks'][0]['id'],10)
    def test_reminders_time_and_dedup(self):
        jobs=self.s.due()
        self.assertEqual(len(jobs),1)
        self.assertEqual(len(jobs[0]['challenge']['tasks']),2)
        self.assertEqual(self.s.due(),[])
        self.s.delivered(jobs[0]['key'])
        self.clock=datetime(2026,10,6,20,0,tzinfo=self.s.tz)
        jobs=self.s.due()
        self.assertEqual(jobs,[])
    def test_daily_time_change_and_next_day(self):
        self.s.set_reminder_time(self.c['id'],'12:00')
        self.assertEqual(self.s.due(),[])
        self.clock=datetime(2026,10,6,12,0,tzinfo=self.s.tz)
        job=self.s.due()[0]
        self.s.delivered(job['key'])
        self.clock=datetime(2026,10,7,12,0,tzinfo=self.s.tz)
        self.assertEqual(len(self.s.due()),1)
    def test_old_per_exercise_delivery_counts_for_day(self):
        with self.s.db:
            self.s.db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',(self.c['id'],1,'2026-10-06',self.c['tasks'][0]['id'],'sent',0))
        self.assertEqual(self.s.due(),[])

    def test_retry_and_pause(self):
        self.s.due()
        self.clock=datetime(2026,10,6,10,6,tzinfo=self.s.tz)
        self.assertEqual(len(self.s.due()),1)
        self.s.pause(self.c['id'],True)
        self.assertEqual(self.s.due(),[])
        with self.assertRaises(Problem):self.save(self.c['tasks'][0]['id'],10)
    def test_completed_task_and_unsubscribed_skip(self):
        self.save(self.c['tasks'][0]['id'],10)
        self.save(self.c['tasks'][1]['id'],5)
        self.assertEqual(self.s.due(),[])
        self.clock=datetime(2026,10,6,20,0,tzinfo=self.s.tz)
        self.s.user({'id':1,'name':'One'},False)
        self.assertEqual(self.s.due(),[])
    def test_auth_signature_expiry_and_duplicate_fields(self):
        self.assertEqual(telegram_identity(signed(),TOKEN)['id'],1)
        for data in [signed().replace('User','Other'),signed(stamp=int(time.time())-90000),signed()+'&auth_date=1']:
            with self.assertRaises(Problem):telegram_identity(data,TOKEN)

class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server_module=server
        server.config={'token':TOKEN,'admins':{1},'url':'https://example.invalid'}
        server.store=Store(':memory:')
        server.store.now=lambda:datetime(2026,10,6,10,0,tzinfo=ZoneInfo('Europe/Moscow'))
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True)
        cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.http.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.server_module.store.db.close()
    def request(self,path,uid=None,body=None,raw=None):
        headers={}
        if uid:headers['Authorization']='tma '+signed(uid)
        data=json.dumps(body).encode() if body is not None else None
        if raw is not None:data=raw
        try:
            with urlopen(Request(self.url+path,data=data,headers=headers),timeout=5) as r:return r.status,json.loads(r.read())
        except HTTPError as r:return r.code,json.loads(r.read())
    def test_end_to_end_authorization_and_results(self):
        self.assertEqual(self.request('/api/state')[0],401)
        self.assertEqual(self.request('/api/admin/create',2,plan())[0],403)
        status,state=self.request('/api/admin/create',1,plan())
        self.assertEqual(status,200)
        c=state['challenge']
        self.assertEqual(self.request('/api/join',2,{'challenge':c['id'],'invite':'wrong','consent':True})[0],403)
        status,state=self.request('/api/join',2,{'challenge':c['id'],'invite':c['invite'],'consent':True})
        self.assertEqual(status,200)
        self.assertEqual(len(state['team']),1)
        status,state=self.request('/api/result',2,{'challenge':c['id'],'task':c['tasks'][0]['id'],'day':'2026-10-06','amount':30})
        self.assertEqual(status,200)
        self.assertEqual(state['progress']['calendar'][0]['status'],'partial')
        self.assertNotIn('invite',state['challenge'])
        self.assertNotIn('id',state['team'][0])
    def test_malformed_request(self):
        self.assertEqual(self.request('/api/join',1,raw=b'bad JSON')[0],400)

class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import server
        self.module=server
        self.s=Store(':memory:')
        self.s.now=lambda:datetime(2026,10,6,10,0,tzinfo=self.s.tz)
        self.s.user({'id':1,'name':'One'},True)
        self.c=self.s.create(plan())
        self.s.join(1,self.c['id'],self.c['invite'],True)
        server.store=self.s
        server.config={'token':TOKEN,'admins':{1},'url':'https://example.invalid'}
    def tearDown(self):
        self.s.db.close()
    async def test_notification_contains_quantity_buttons_and_app(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        bot=SimpleNamespace(send_message=AsyncMock())
        await self.module.reminders(SimpleNamespace(bot=bot))
        bot.send_message.assert_awaited_once()
        call=bot.send_message.call_args
        self.assertEqual(call.args[0],1)
        keyboard=call.kwargs['reply_markup'].inline_keyboard
        self.assertIn('Pushups',call.args[1])
        self.assertIn('Stretch',call.args[1])
        self.assertEqual(keyboard[0][0].web_app.url,'https://example.invalid')
        await self.module.reminders(SimpleNamespace(bot=bot))
        bot.send_message.assert_awaited_once()
    async def test_telegram_callback_updates_same_database(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        q=SimpleNamespace(data=f"r:{self.c['id']}:{self.c['tasks'][0]['id']}:2026-10-06:30",from_user=SimpleNamespace(id=1),answer=AsyncMock())
        await self.module.callback(SimpleNamespace(callback_query=q),None)
        self.assertEqual(self.s.state(1)['progress']['totals'][0]['amount'],30)
        q.data=f"r:{self.c['id']}:{self.c['tasks'][0]['id']}:2026-10-05:20"
        await self.module.callback(SimpleNamespace(callback_query=q),None)
        self.assertEqual(self.s.state(1)['progress']['totals'][0]['amount'],30)

if __name__=='__main__':unittest.main()
