"""Install local user units after database and camera registration preparation."""
import os
import argparse
from pathlib import Path
import shutil
import shlex
import json
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import Database,get_migration_status

root=Path(__file__).resolve().parents[1]
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-start',action='store_true',help='Install units without activating writers')
    args=parser.parse_args()
    data=Path(os.environ.get('FIELD_DEMO_DATA',root/'Data'))
    db=Database(os.environ.get('FIELD_DATABASE_PATH',data/'Demo.sqlite3'))
    if get_migration_status(db)['pending']:raise SystemExit('Run prepare_runtime.py first')
    # Preserve GPU/camera settings from the original unit's drop-ins, without
    # printing credentials or making any environment change in other projects.
    runtime=data/'Runtime';runtime.mkdir(parents=True,exist_ok=True,mode=0o700)
    environment=runtime/'ServiceEnvironment.env'
    if not environment.exists():
        result=subprocess.run(['systemctl','--user','show','field-recognition-demo.service','--property=Environment','--value'],capture_output=True,text=True,check=True)
        lines=[]
        for token in shlex.split(result.stdout):
            key,separator,value=token.partition('=')
            if separator and key not in {'FIELD_PRODUCTION_ENABLED','FIELD_SERVICE_ROLE'}:
                lines.append(key+'='+json.dumps(value,ensure_ascii=False))
        with open(environment,'x',opener=lambda path,flags:os.open(path,flags,0o600)) as handle:handle.write('\n'.join(lines)+'\n')
    units=Path.home()/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
    sources=[*sorted((root/'deployment').glob('field-recognition-*.service')),root/'deployment/field-recognition.target']
    sources=[p for p in sources if p.name!='field-recognition-demo.service']
    for source in sources:
        content=source.read_text().replace('%h/Projects/FieldRecognition',str(root))
        content=content.replace(str(root/'Data/Runtime/ServiceEnvironment.env'),str(environment.resolve()))
        (units/source.name).write_text(content)
    if not args.no_start:
        subprocess.run(['systemctl','--user','disable','--now','field-recognition-demo.service'],check=True)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable',*([] if args.no_start else ['--now']),'field-recognition.target'],check=True)
    print('Managed local units installed'+('' if args.no_start else ' and started'))

if __name__=='__main__':main()
