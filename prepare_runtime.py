"""Prepare local personnel and cameras; login credentials are opt-in."""
import argparse
import json
import os
import secrets
import uuid
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime,timezone
from database import Database,apply_migrations,get_migration_status
from archive_manager import ArchiveManager
from auth import AuthService
from camera_registry import CameraRegistry


def prepare(data, username, display_name, camera_id, receiver_url=None, photo_root=None, *, auth_enabled=False):
    if not username.strip() or not display_name.strip():
        raise ValueError('Personnel identifier and display name are required')
    data=Path(data).resolve();data.mkdir(parents=True,exist_ok=True)
    db=Database(data/'Demo.sqlite3')
    if db.path.exists() and get_migration_status(db)['pending']:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        ArchiveManager(db,data/'unused').backup_database(data/'Backups'/('BeforeRuntime-'+stamp+'.sqlite3'))
    apply_migrations(db)
    credentials=None
    with db.connection() as conn:
        users=conn.execute('SELECT id FROM users WHERE disabled=0 AND role=\'admin\'').fetchall()
    if not auth_enabled:
        # A stable wearer ID is still needed for capture ownership, even when
        # opening the workbench does not require an authenticated account.
        with db.transaction('IMMEDIATE') as conn:
            row=conn.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
            if row and (row['disabled'] or row['display_name']!=display_name):
                raise ValueError('Existing personnel registration requires explicit correction')
            if not row:
                conn.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()),username,display_name,'operator',None,datetime.now(timezone.utc).isoformat(),0))
                row=conn.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
            user=SimpleNamespace(**dict(row))
    elif not users:
        if (data/'Access'/'InitialAccess.json').exists():
            raise ValueError('An existing credential delivery file must be resolved before creating an administrator')
        secret=secrets.token_urlsafe(24)
        user=AuthService(db).create_initial_admin(username,display_name,secret)
        credentials={'username':username,'password':secret,'login_url':'http://127.0.0.1:8188/login'}
    else:
        with db.connection() as conn:row=conn.execute('SELECT * FROM users WHERE username=? AND disabled=0',(username,)).fetchone()
        if not row:raise ValueError('Existing users require an explicit, enabled account for camera assignment')
        user=SimpleNamespace(**dict(row))
    registry=CameraRegistry(db)
    if camera_id and not registry.get_camera(camera_id):
        registry.register_camera(camera_id,camera_id,receiver_url=receiver_url,nas_photo_root=photo_root)
    if camera_id:
        with db.transaction('IMMEDIATE') as conn:
            from binding_operator import activate_member
            activate_member(conn, camera_id, user.id, datetime.now(timezone.utc).isoformat())
    if credentials:
        access=data/'Access';access.mkdir(mode=0o700,exist_ok=True);os.chmod(access,0o700)
        path=access/'InitialAccess.json'
        with open(path,'x',opener=lambda path,flags:os.open(path,flags,0o600)) as f:json.dump(credentials,f,ensure_ascii=False,indent=2)
        print('Initial credentials saved privately to '+str(path))
    return {'database':str(db.path),'camera_id':camera_id,'user_id':user.id,'migrations':get_migration_status(db)['current_version']}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',default='Data');p.add_argument('--username',required=True);p.add_argument('--display-name',required=True);p.add_argument('--camera');p.add_argument('--receiver-url');p.add_argument('--photo-root')
    p.add_argument('--enable-login',action='store_true',default=os.environ.get('FIELD_AUTH_ENABLED','0')=='1')
    args=p.parse_args();print(json.dumps(prepare(args.data,args.username,args.display_name,args.camera,args.receiver_url,args.photo_root,auth_enabled=args.enable_login),ensure_ascii=False))
