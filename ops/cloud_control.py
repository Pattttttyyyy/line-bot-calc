"""Image-only cloud release. No Secret reads, DB queries, migrations or LINE calls."""
import copy,json,os,re,subprocess,sys,urllib.request

class ControlError(RuntimeError):pass

def run(args):
    p=subprocess.run(args,capture_output=True,text=True)
    if p.returncode:raise ControlError('Command failed: '+args[0]+' (details withheld from public logs)')
    return p.stdout

class Control:
    def __init__(self,project,region,service,repository):
        for value in (project,region,service,repository):
            if not re.fullmatch('[a-z][a-z0-9-]{0,62}',value):raise ControlError('Invalid resource name')
        if region!='asia-east1' or service!='line-bot-calc':raise ControlError('Unexpected target')
        self.project=project;self.region=region;self.service=service;self.repository=repository
    def g(self,*args):return json.loads(run(['gcloud',*args,'--project='+self.project,'--format=json','--quiet']))
    def service_state(self):return self.g('run','services','describe',self.service,'--region='+self.region)
    @staticmethod
    def active(s):
        traffic=s['status'].get('traffic',[])
        full=[x.get('revisionName') for x in traffic if x.get('percent')==100]
        if len(full)!=1:raise ControlError('Expected one revision receiving 100% traffic')
        return full[0]
    @staticmethod
    def assert_limits(s):
        t=s['spec']['template'];a=t['metadata'].get('annotations',{})
        if t['spec']['containers'][0]['resources']['limits']!={'cpu':'1','memory':'512Mi'}:raise ControlError('Resource limits changed')
        if a.get('autoscaling.knative.dev/minScale','0')!='0' or a.get('autoscaling.knative.dev/maxScale')!='1':raise ControlError('Scaling limits changed')
        if a.get('run.googleapis.com/cpu-throttling','true')!='true':raise ControlError('Billing mode changed')
        if a.get('run.googleapis.com/startup-cpu-boost','false')!='false':raise ControlError('Startup CPU boost changed')
    @staticmethod
    def template_without_image(s):
        spec=copy.deepcopy(s['spec']['template']['spec'])
        for c in spec['containers']:c.pop('image',None)
        return spec
    def health(self,s):
        url=s['status']['url']
        if not re.fullmatch(r'https://[a-z0-9-]+\.a\.run\.app',url):raise ControlError('Unexpected health URL')
        with urllib.request.urlopen(url+'/',timeout=45) as response:
            if response.status!=200:raise ControlError('Health check failed')
        return 200
    def snapshot(self):
        s=self.service_state();self.assert_limits(s)
        return dict(active_revision=self.active(s),latest_ready_revision=s['status']['latestReadyRevisionName'],
                    image=s['spec']['template']['spec']['containers'][0]['image'],health=self.health(s),
                    cpu=1,memory='512Mi',min_instances=0,max_instances=1)
    def deploy(self,sha,expected,run_id):
        if not re.fullmatch('[0-9a-f]{40}',sha):raise ControlError('Invalid source SHA')
        if not re.fullmatch('[0-9]+-[0-9]+',run_id):raise ControlError('Invalid run ID')
        old=self.service_state();self.assert_limits(old)
        if self.active(old)!=expected or old['status']['latestReadyRevisionName']!=expected:
            raise ControlError('Production version changed; obtain a new approval')
        self.health(old)
        before=self.template_without_image(old)
        host=self.region+'-docker.pkg.dev'
        tag=host+'/'+self.project+'/'+self.repository+'/line-bot:git-'+sha
        run(['gcloud','auth','configure-docker',host,'--quiet'])
        run(['docker','build','--label','org.opencontainers.image.revision='+sha,'--tag',tag,'.'])
        run(['docker','push',tag])
        artifact=self.g('artifacts','docker','images','describe',tag)
        digest=artifact['image_summary']['digest']
        if not re.fullmatch('sha256:[0-9a-f]{64}',digest):raise ControlError('Invalid image digest')
        image=tag.split(':')[0]+'@'+digest
        # Fail if another maintainer changed settings while the build ran.
        now=self.service_state()
        if self.active(now)!=expected or self.template_without_image(now)!=before:
            raise ControlError('Production settings changed during build')
        suffix='gh-'+run_id;revision=self.service+'-'+suffix
        run(['gcloud','run','deploy',self.service,'--project='+self.project,'--region='+self.region,
             '--image='+image,'--revision-suffix='+suffix,'--no-traffic','--quiet'])
        staged=self.service_state();self.assert_limits(staged)
        if self.active(staged)!=expected or staged['status']['latestReadyRevisionName']!=revision:
            raise ControlError('Staging did not preserve previous traffic')
        if self.template_without_image(staged)!=before:raise ControlError('Runtime configuration changed; traffic not switched')
        if staged['spec']['template']['spec']['containers'][0]['image']!=image:raise ControlError('Image mismatch')
        run(['gcloud','run','services','update-traffic',self.service,'--project='+self.project,
             '--region='+self.region,'--to-revisions='+revision+'=100','--quiet'])
        try:
            after=self.service_state();self.assert_limits(after)
            if self.active(after)!=revision or self.template_without_image(after)!=before:raise ControlError('Post-deploy mismatch')
            self.health(after)
        except Exception:
            run(['gcloud','run','services','update-traffic',self.service,'--project='+self.project,
                 '--region='+self.region,'--to-revisions='+expected+'=100','--quiet'])
            raise ControlError('Verification failed; previous traffic restored') from None
        return dict(active_revision=revision,image=image,source_sha=sha,health=200,previous_revision=expected)

def main():
    c=Control(os.environ['GCP_PROJECT'],os.environ.get('GCP_REGION','asia-east1'),
              os.environ.get('RUN_SERVICE','line-bot-calc'),os.environ.get('ARTIFACT_REPOSITORY','line-bot'))
    mode=sys.argv[1]
    result=c.snapshot() if mode=='check' else c.deploy(os.environ['SOURCE_SHA'],os.environ['EXPECTED_REVISION'],os.environ['GITHUB_RUN_ID']+'-'+os.environ['GITHUB_RUN_ATTEMPT']) if mode=='deploy' else None
    if result is None:raise ControlError('Unknown mode')
    safe=json.dumps(result,ensure_ascii=False,indent=2)
    print(safe)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as f:f.write('```json\n'+safe+'\n```\n')

if __name__=='__main__':
    try:main()
    except Exception as e:
        print('CLOUD_OPERATION_STOPPED',type(e).__name__)
        sys.exit(1)
