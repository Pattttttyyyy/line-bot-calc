"""Disposable PostgreSQL Unix socket only; cannot use any production DSN."""
import os,subprocess,sys,tempfile
from pathlib import Path
root=Path(__file__).parents[1]
temp=Path(tempfile.mkdtemp(prefix='mycard-isolated-'))
pg=Path('/usr/lib/postgresql/16/bin');data=temp/'data'
subprocess.run([str(pg/'initdb'),'-D',str(data),'-A','trust','--no-locale','-E','UTF8'],check=True,stdout=subprocess.DEVNULL)
subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(temp/'log'),'-o',f"-k {temp} -p 55441 -c listen_addresses=''",'-w','start'],check=True,stdout=subprocess.DEVNULL)
try:
    subprocess.run([str(pg/'createdb'),'-h',str(temp),'-p','55441','mycard_isolated'],check=True)
    result=subprocess.run([sys.executable,'-m','mycard_draft.test_journal_postgres'],cwd=root,
        env=dict(os.environ,MYCARD_ISOLATED_DSN=f'host={temp} port=55441 dbname=mycard_isolated'))
finally:
    subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
sys.exit(result.returncode)
