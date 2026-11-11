"""Credential-free tests of cloud request guards and release behavior."""
import copy,json,unittest
from pathlib import Path
from unittest.mock import patch
from ops.cloud_control import Control,ControlError
from ops.cloud_request import validate

SHA='a'*40
OLD='line-bot-calc-old'
NEW='line-bot-calc-gh-123-1'
DIGEST='sha256:'+'b'*64
IMAGE='asia-east1-docker.pkg.dev/example-project/line-bot/line-bot@'+DIGEST

def state(active=OLD,latest=OLD,image='previous-image',created=None):
    return {'spec':{'template':{'metadata':{'annotations':{'autoscaling.knative.dev/maxScale':'1'}},
                'spec':{'serviceAccountName':'runtime@example.invalid','containers':[{'image':image,
                'resources':{'limits':{'cpu':'1','memory':'512Mi'}},'env':[{'name':'PRIVATE','value':'FAKE_PRIVATE_VALUE'}]}]}}},
            'status':{'traffic':[{'revisionName':active,'percent':100}],'latestReadyRevisionName':latest,'latestCreatedRevisionName':created or latest,'url':'https://example.a.run.app'}}

def cloud_result():
    return {'image_summary':{'digest':DIGEST},'status':{'conditions':[{'type':'Ready','status':'True','reason':'Retired'}]}}

class RequestTests(unittest.TestCase):
    def test_default_check(self):self.assertEqual(validate({},SHA)['mode'],'check')
    def test_deploy_needs_sha(self):
        with self.assertRaises(ValueError):validate({'mode':'deploy','expected_revision':OLD},SHA)
    def test_deploy_needs_previous_revision(self):
        with self.assertRaises(ValueError):validate({'mode':'deploy','source_sha':SHA},SHA)
    def test_reject_shell_sha(self):
        with self.assertRaises(ValueError):validate({'source_sha':'a;echo bad'},SHA)
    def test_reject_unknown_mode(self):
        with self.assertRaises(ValueError):validate({'mode':'erase','source_sha':SHA},SHA)
    def test_approved_request(self):self.assertEqual(validate({'mode':'deploy','source_sha':SHA,'expected_revision':OLD},SHA)['source_sha'],SHA)
    def test_report_deploy_requires_job_generation(self):
        with self.assertRaises(ValueError):
            validate({'mode':'report-deploy','source_sha':SHA,'expected_revision':OLD},SHA)
        self.assertEqual(validate({'mode':'report-deploy','source_sha':SHA,
                                  'expected_revision':'report-generation-3'},SHA)['mode'],'report-deploy')

class ControlTests(unittest.TestCase):
    def setUp(self):self.c=Control('example-project','asia-east1','line-bot-calc','line-bot')
    def test_resource_injection_denied(self):
        with self.assertRaises(ControlError):Control('example;bad','asia-east1','line-bot-calc','line-bot')
    def test_different_region_denied(self):
        with self.assertRaises(ControlError):Control('example-project','us-central1','line-bot-calc','line-bot')
    def test_split_traffic_denied(self):
        s=state();s['status']['traffic'][0]['percent']=50
        with self.assertRaises(ControlError):self.c.active(s)
    def test_scale_changed_denied(self):
        s=state();s['spec']['template']['metadata']['annotations']['autoscaling.knative.dev/maxScale']='2'
        with self.assertRaises(ControlError):self.c.assert_limits(s)
    def test_check_summary_hides_environment(self):
        with patch.object(self.c,'service_state',return_value=state()),patch.object(self.c,'health',return_value=200):out=self.c.snapshot()
        self.assertNotIn('FAKE_PRIVATE_VALUE',json.dumps(out));self.assertNotIn('serviceAccountName',json.dumps(out))
    def test_stale_revision_prevents_build(self):
        with patch.object(self.c,'service_state',return_value=state()),patch('ops.cloud_control.run') as run:
            with self.assertRaises(ControlError):self.c.deploy(SHA,'line-bot-calc-other','123-1')
            run.assert_not_called()
    def test_image_only_release_and_no_traffic_staging(self):
        states=[state(),state(),state(latest=OLD,created=NEW,image=IMAGE),state(active=NEW,latest=NEW,image=IMAGE)]
        with patch.object(self.c,'service_state',side_effect=states),patch.object(self.c,'health',return_value=200),patch.object(self.c,'g',return_value=cloud_result()),patch('ops.cloud_control.run',return_value='') as run:
            out=self.c.deploy(SHA,OLD,'123-1')
        self.assertEqual(out['active_revision'],NEW)
        commands=[x.args[0] for x in run.call_args_list]
        deploy=next(a for a in commands if a[:3]==['gcloud','run','deploy'])
        self.assertIn('--no-traffic',deploy)
        self.assertFalse(any('secrets' in a or 'jobs' in a for a in commands))
        self.assertNotIn('FAKE_PRIVATE_VALUE',json.dumps(out))
    def test_runtime_change_stops_before_switch(self):
        staged=state(latest=OLD,created=NEW,image=IMAGE);staged['spec']['template']['spec']['containers'][0]['env']=[]
        with patch.object(self.c,'service_state',side_effect=[state(),state(),staged]),patch.object(self.c,'health',return_value=200),patch.object(self.c,'g',return_value=cloud_result()),patch('ops.cloud_control.run',return_value='') as run:
            with self.assertRaises(ControlError):self.c.deploy(SHA,OLD,'123-1')
        self.assertFalse(any('update-traffic' in x.args[0] for x in run.call_args_list))
    def test_failed_health_restores_previous_traffic(self):
        with patch.object(self.c,'service_state',side_effect=[state(),state(),state(latest=OLD,created=NEW,image=IMAGE),state(active=NEW,latest=NEW,image=IMAGE)]),patch.object(self.c,'health',side_effect=[200,ControlError('fake')]),patch.object(self.c,'g',return_value=cloud_result()),patch('ops.cloud_control.run',return_value='') as run:
            with self.assertRaises(ControlError):self.c.deploy(SHA,OLD,'123-1')
        self.assertIn('--to-revisions='+OLD+'=100',run.call_args_list[-1].args[0])
    def test_unready_staged_revision_never_receives_traffic(self):
        cloud=cloud_result();cloud['status']['conditions'][0]['status']='False'
        with patch.object(self.c,'service_state',side_effect=[state(),state(),state(created=NEW,image=IMAGE)]),patch.object(self.c,'health',return_value=200),patch.object(self.c,'g',return_value=cloud),patch('ops.cloud_control.run',return_value='') as run:
            with self.assertRaises(ControlError):self.c.deploy(SHA,OLD,'123-1')
        self.assertFalse(any('update-traffic' in x.args[0] for x in run.call_args_list))
    def test_workflow_is_pinned_and_has_no_credentials(self):
        text=(Path(__file__).parent/'.github/workflows/cloud-control.yml').read_text(encoding='utf-8')
        self.assertNotIn('secrets.',text)
        self.assertIn('persist-credentials: false',text)
        self.assertIn('ops/cloud-request.json',text)
        self.assertIn("github.ref == 'refs/heads/main'",text)
        import re
        for ref in re.findall(r'uses: (\S+)',text):self.assertRegex(ref,r'@[a-f0-9]{40}$')


def report_job(generation=3,image='previous-report-image'):
    return {'metadata':{'generation':generation},
            'spec':{'template':{'spec':{'taskCount':1,'template':{'spec':{
                'serviceAccountName':'runtime@example.invalid',
                'containers':[{'image':image,'command':['python'],'args':['daily_report.py'],
                               'env':[{'name':'PRIVATE','value':'FAKE_PRIVATE_VALUE'}]}]}}}}},
            'status':{'conditions':[{'type':'Ready','status':'True'}]}}


class ReportControlTests(unittest.TestCase):
    def setUp(self):
        self.c=Control('example-project','asia-east1','line-bot-calc','line-bot')
    def test_report_summary_hides_credentials_and_recipient(self):
        with patch.object(self.c,'report_state',return_value=report_job()):
            result=self.c.report_snapshot()
        self.assertNotIn('FAKE_PRIVATE_VALUE',json.dumps(result))
        self.assertNotIn('serviceAccountName',json.dumps(result))
    def test_changed_report_stops_before_build(self):
        with patch.object(self.c,'report_state',return_value=report_job()),patch('ops.cloud_control.run') as run:
            with self.assertRaises(ControlError):
                self.c.deploy_report(SHA,'report-generation-2')
        run.assert_not_called()
    def test_report_deployment_changes_only_image_and_never_executes(self):
        old=report_job()
        new=report_job(4,IMAGE)
        with patch.object(self.c,'report_state',side_effect=[old,old,new]),patch.object(
                self.c,'g',return_value=cloud_result()),patch('ops.cloud_control.run',return_value='') as run:
            result=self.c.deploy_report(SHA,'report-generation-3')
        self.assertTrue(result['ready'])
        commands=[x.args[0] for x in run.call_args_list]
        update=next(c for c in commands if c[:4]==['gcloud','run','jobs','update'])
        self.assertIn('line-bot-daily-report',update)
        self.assertIn('--image='+IMAGE,update)
        self.assertFalse(any('execute' in c or '--execute-now' in c or 'services' in c for c in commands))
    def test_report_environment_change_stops_before_update(self):
        old=report_job()
        changed=report_job()
        changed['spec']['template']['spec']['taskCount']=2
        with patch.object(self.c,'report_state',side_effect=[old,changed]),patch.object(
                self.c,'g',return_value=cloud_result()),patch('ops.cloud_control.run',return_value='') as run:
            with self.assertRaises(ControlError):
                self.c.deploy_report(SHA,'report-generation-3')
        self.assertFalse(any(c.args[0][:4]==['gcloud','run','jobs','update'] for c in run.call_args_list))

if __name__=='__main__':unittest.main()
