"""Optional one-time import of subscriptions; never fabricate per-exercise history."""
import argparse
import sqlite3
from pathlib import Path
from urllib.parse import quote
from domain import Store

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Import old bot subscriptions. Stop both bots first.')
    parser.add_argument('old_database')
    parser.add_argument('--new-database',default=str(Path(__file__).with_name('miniapp.sqlite3')))
    args=parser.parse_args()
    source=sqlite3.connect('file:'+quote(str(Path(args.old_database).resolve()))+'?mode=ro',uri=True)
    rows=source.execute('SELECT id,name,active FROM users').fetchall()
    store=Store(args.new_database)
    for uid,name,active in rows:
        with store.lock:
            existing=store.db.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone()
        if not existing:
            store.user({'id':uid,'name':name},bool(active))
    source.close();store.db.close()
    print(f'Проверено пользователей: {len(rows)}. Участники должны вступить в новый челлендж сами.')
