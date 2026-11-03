"""Temporary local PostgreSQL only; never reads a production DSN."""
import json,os,re,subprocess,sys,tempfile
from pathlib import Path
root=Path(__file__).parent
temp=Path(tempfile.mkdtemp(prefix='group-credit-pg-'))
data=temp/'data'
pg=Path('/usr/lib/postgresql/16/bin')
subprocess.run([str(pg/'initdb'),'-D',str(data),'-A','trust','--no-locale','-E','UTF8'],check=True,stdout=subprocess.DEVNULL)
subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(temp/'log'),'-o',f"-k {temp} -p 55439 -c listen_addresses=''",'-w','start'],check=True)
try:
    subprocess.run([str(pg/'createdb'),'-h',str(temp),'-p','55439','group_credit_isolated'],check=True)
    result=subprocess.run([sys.executable,str(root/'test_postgres.py')],
        env=dict(os.environ,GROUP_CREDIT_TEST_DSN=f'host={temp} port=55439 dbname=group_credit_isolated'),
        capture_output=True,text=True)
    log=result.stdout+result.stderr
    (root/'postgres-test.log').write_text(log)
    match=re.search(r'Ran (\d+) tests?',log)
    (root/'postgres-test-result.json').write_text(json.dumps(dict(exit_code=result.returncode,tests=int(match[1]) if match else None)))
    print(log[-1400:])
    print('ISOLATED_POSTGRES_TEST_EXIT',result.returncode)
finally:
    subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True)
sys.exit(result.returncode)
