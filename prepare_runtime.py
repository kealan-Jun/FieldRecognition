"""Offline preparation. Preserve evidence and issue bootstrap credentials once."""
import argparse
import json
import os
import secrets
from pathlib import Path
from datetime import datetime,timezone
from database import Database,apply_migrations,get_migration_status
from archive_manager import ArchiveManager
from auth import AuthService
from camera_registry import CameraRegistry


def prepare(data, username, display_name, camera_id, receiver_url=None, photo_root=None):
    data=Path(data).resolve();data.mkdir(parents=True,exist_ok=True)
    db=Database(data/'Demo.sqlite3')
    if db.path.exists() and get_migration_status(db)['pending']:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        ArchiveManager(db,data/'unused').backup_database(data/'Backups'/('BeforeRuntime-'+stamp+'.sqlite3'))
    apply_migrations(db)
    credentials=None
    with db.connection() as conn:
        users=conn.execute('SELECT id FROM users WHERE disabled=0 AND role=\'admin\'').fetchall()
    if not users:
        if (data/'Access'/'InitialAccess.json').exists():
            raise ValueError('An existing credential delivery file must be resolved before creating an administrator')
        secret=secrets.token_urlsafe(24)
        user=AuthService(db).create_initial_admin(username,display_name,secret)
        credentials={'username':username,'password':secret,'login_url':'http://127.0.0.1:8188/login'}
    else:
        with db.connection() as conn:row=conn.execute('SELECT * FROM users WHERE username=? AND disabled=0',(username,)).fetchone()
        if not row:raise ValueError('Existing users require an explicit, enabled account for camera assignment')
        from types import SimpleNamespace
        user=SimpleNamespace(**dict(row))
    registry=CameraRegistry(db)
    if camera_id and not registry.get_camera(camera_id):
        registry.register_camera(camera_id,camera_id,receiver_url=receiver_url,nas_photo_root=photo_root)
    if camera_id:
        with db.transaction('IMMEDIATE') as conn:
            owner=conn.execute('SELECT user_id FROM camera_users WHERE camera_id=?',(camera_id,)).fetchone()
            if owner and owner[0]!=user.id:raise ValueError('Existing camera ownership must use explicit reassignment')
            active=conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL',(camera_id,)).fetchall()
            if any(json.loads(r[0]).get('operator')!=display_name for r in active):raise ValueError('Camera is in use by a different operator')
            conn.execute('INSERT OR IGNORE INTO camera_users VALUES(?,?)',(camera_id,user.id))
    if credentials:
        access=data/'Access';access.mkdir(mode=0o700,exist_ok=True);os.chmod(access,0o700)
        path=access/'InitialAccess.json'
        with open(path,'x',opener=lambda path,flags:os.open(path,flags,0o600)) as f:json.dump(credentials,f,ensure_ascii=False,indent=2)
        print('Initial credentials saved privately to '+str(path))
    return {'database':str(db.path),'camera_id':camera_id,'user_id':user.id,'migrations':get_migration_status(db)['current_version']}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',default='Data');p.add_argument('--username',required=True);p.add_argument('--display-name',required=True);p.add_argument('--camera');p.add_argument('--receiver-url');p.add_argument('--photo-root')
    args=p.parse_args();print(json.dumps(prepare(args.data,args.username,args.display_name,args.camera,args.receiver_url,args.photo_root),ensure_ascii=False))
