"""One process runs Telegram polling, reminder jobs and a same-origin Mini App API."""
import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, filters
from telegram.request import HTTPXRequest
import httpx
from domain import Store, Problem, telegram_identity

ROOT = Path(__file__).resolve().parent
store = None
config = None

class Handler(BaseHTTPRequestHandler):
    server_version = 'Challenge'
    def setup(self):
        super().setup()
        self.connection.settimeout(15)
    def log_message(self, *args):
        pass  # Never log initData, tokens, invitation codes or request bodies.
    def send(self, status, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org")
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self.dispatch()
    def do_POST(self):
        self.dispatch()
    def dispatch(self):
        path = urlparse(self.path).path
        try:
            if path == '/health' and self.command == 'GET':
                self.send(200,{'ok':True}); return
            if not path.startswith('/api/'):
                files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','application/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),'/favicon.svg':('favicon.svg','image/svg+xml')}
                for name in ('squats','knee-pushups','plank'):
                    files[f'/instructions/{name}.gif']=(f'instructions/{name}.gif','image/gif')
                if self.command != 'GET' or path not in files:
                    self.send(404,{'error':'Страница не найдена.'}); return
                filename, content_type=files[path]
                self.send(200,(ROOT/'static'/filename).read_bytes(),content_type); return
            raw=self.headers.get('Authorization','')
            if not raw.startswith('tma '):
                raise Problem('Откройте приложение из Telegram.',401)
            identity=telegram_identity(raw[4:],config['token'])
            user=store.user(identity)
            uid=user['id']
            admin=uid in config['admins']
            if path == '/api/state' and self.command == 'GET':
                self.send(200,store.state(uid,admin)); return
            if self.command != 'POST':
                raise Problem('Метод не поддерживается.',405)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 16000:
                    raise ValueError()
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):
                    raise ValueError()
            except (ValueError,json.JSONDecodeError):
                raise Problem('Некорректный запрос.')
            if path.startswith('/api/admin/') and not admin:
                raise Problem('Эта функция доступна организатору.',403)
            if path == '/api/join':
                store.join(uid,payload.get('challenge'),payload.get('invite',''),payload.get('consent'))
            elif path == '/api/result':
                store.save_result(uid,payload.get('challenge'),payload.get('task'),payload.get('day'),payload.get('amount'))
            elif path == '/api/result/clear':
                store.clear_result(uid,payload.get('challenge'),payload.get('task'),payload.get('day'))
            elif path == '/api/admin/create':
                store.create(payload)
            elif path == '/api/admin/reminder':
                store.set_reminder_time(payload.get('challenge'),payload.get('time'))
            elif path == '/api/admin/pause':
                store.pause(payload.get('challenge'),payload.get('paused'))
            else:
                raise Problem('Команда не найдена.',404)
            self.send(200,store.state(uid,admin))
        except Problem as exc:
            self.send(exc.status,{'error':str(exc)})
        except Exception as exc:
            logging.error('API error: %s',type(exc).__name__)
            self.send(500,{'error':'Не удалось сохранить или загрузить данные. Попробуйте ещё раз.'})

def app_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton('Открыть челлендж',web_app=WebAppInfo(config['url']))]])

async def command(update, context):
    uid=update.effective_user.id
    cmd=update.effective_message.text.split()[0].split('@')[0]
    logging.warning('Command received: %s', cmd)
    identity={'id':uid,'name':update.effective_user.full_name}
    if cmd=='/id':
        await update.effective_message.reply_text(f'Ваш ID: {uid}')
    elif cmd=='/stop':
        store.user(identity,subscribe=False)
        await update.effective_message.reply_text('Напоминания отключены. Результаты сохраняются. /start — включить снова.')
    elif cmd=='/start':
        store.user(identity,subscribe=True)
        await update.effective_message.reply_text('Напоминания включены. Откройте приложение и вступите в челлендж по коду организатора. При вступлении ваши результаты будут доступны его участникам.',reply_markup=app_button())
    elif cmd=='/help':
        await update.effective_message.reply_text('/start — включить уведомления\n/stop — отключить уведомления\n/app — открыть приложение\n/id — мой ID\n\nУпражнения, календарь и прогресс команды — в приложении.',reply_markup=app_button())
    else:
        store.user(identity)
        await update.effective_message.reply_text('Задания и результаты челленджа:',reply_markup=app_button())

    logging.warning('Command reply sent: %s', cmd)

async def callback(update, context):
    q=update.callback_query
    try:
        _,cid,tid,day,amount=q.data.split(':')
        store.save_result(q.from_user.id,int(cid),int(tid),day,int(amount))
        await q.answer('Сохранено ✅ Откройте приложение, чтобы увидеть прогресс.',show_alert=True)
        # Keep all options so an accidental selection can be corrected.
    except Problem as exc:
        await q.answer(str(exc),show_alert=True)

async def reminders(context):
    jobs=store.due()
    for job in jobs:
        cid,uid,day,tid=job['key']
        tasks=job['challenge']['tasks']
        buttons=[[InlineKeyboardButton('Упражнения и инструкции',web_app=WebAppInfo(config['url']))]]
        exercises='\n\n'.join(f"{i+1}. {t['title']}\n{t['description'][:150]}\nВарианты: {', '.join(str(n) for n in t['options'])} {t['unit']}" for i,t in enumerate(tasks))
        text=f"{job['challenge']['name']}\n\nВсе упражнения на сегодня:\n\n{exercises}\n\nВыполняйте в удобное время. Инструкции и отметки результатов — в приложении."
        try:
            await context.bot.send_message(uid,text,reply_markup=InlineKeyboardMarkup(buttons))
            store.delivered(job['key'])
        except Forbidden:
            with store.lock,store.db:
                store.db.execute('UPDATE users SET subscribed=0 WHERE id=?',(uid,))
        except TelegramError:
            logging.warning('Reminder delivery failed; retry in five minutes')
        await asyncio.sleep(0.05)

async def error(update, context):
    logging.error('Telegram error: %s',type(context.error).__name__)

async def run():
    global store,config
    load_dotenv(ROOT/'.env')
    token=os.getenv('BOT_TOKEN','')
    url=os.getenv('MINIAPP_URL','').rstrip('/')
    if not token or not url.startswith('https://'):
        raise SystemExit('Заполните BOT_TOKEN и MINIAPP_URL=https://... в .env. См. README.md.')
    config={'token':token,'url':url,'admins':{int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip()}}
    store=Store(os.getenv('DB_PATH',str(ROOT/'miniapp.sqlite3')),os.getenv('TIMEZONE','Europe/Moscow'))
    def telegram_request():
        return HTTPXRequest(connect_timeout=20, read_timeout=35, write_timeout=20, pool_timeout=20, httpx_kwargs={"transport": httpx.AsyncHTTPTransport(retries=3, local_address="0.0.0.0")})
    app=Application.builder().token(token).request(telegram_request()).get_updates_request(telegram_request()).build()
    app.add_handler(CommandHandler(['start','stop','app','help','id'],command,filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(callback,pattern=r'^r:\d+:\d+:\d{4}-\d{2}-\d{2}:\d+$'))
    app.add_error_handler(error)
    http=ThreadingHTTPServer((os.getenv('HOST','127.0.0.1'),int(os.getenv('PORT','8080'))),Handler)
    http.daemon_threads=True
    app.job_queue.run_repeating(reminders, interval=60,first=5)
    async with app:
        await app.bot.set_my_commands([BotCommand(k,v) for k,v in [('start','Включить уведомления'),('app','Открыть приложение'),('stop','Выключить уведомления'),('id','Мой ID'),('help','Помощь')]])
        await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text='Челлендж',web_app=WebAppInfo(url)))
        await app.updater.start_polling(allowed_updates=['message','callback_query'])
        await app.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        print('Бот и Mini App запущены. Ctrl+C — остановить.')
        try:
            await asyncio.Event().wait()
        finally:
            http.shutdown()
            await app.updater.stop()
            await app.stop()
            with store.lock:
                store.db.close()

if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
