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
    def test_diagnose_is_valid_readonly_request(self):
        self.assertEqual(validate({'mode':'diagnose'},SHA)['mode'],'diagnose')
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
    def test_diagnostics_permission_denial_does_not_expose_error_or_change_access(self):
        with patch.object(self.c,'service_state',return_value=state()),patch.object(
                self.c,'g',side_effect=ControlError('gcloud logging read: permission_denied')) as g:
            result=self.c.diagnose()
        self.assertEqual(result['log_access'],'permission_denied')
        self.assertEqual(g.call_args.args[:2],('logging','read'))
        self.assertNotIn('PRIVATE',json.dumps(result))
    def test_diagnostics_filter_is_fixed_and_never_deploys_or_reads_secrets(self):
        with patch.object(self.c,'service_state',return_value=state()),patch.object(self.c,'g',return_value=[]) as g:
            result=self.c.diagnose()
        self.assertEqual(result['log_access'],'available')
        self.assertIn('service_name="line-bot-calc"',g.call_args.args[2])
        self.assertIn('timestamp>="2026-11-17T15:05:00Z"',g.call_args.args[2])
        self.assertIn('--limit=500',g.call_args.args)
        self.assertIn('--resource-names=projects/example-project/locations/global/buckets/_Default/views/line_bot_calc_latency',g.call_args.args)
        self.assertIn('timestamp>="2026-11-17T13:20:00Z"',g.call_args.args[2])
    def test_diagnostics_view_location_cannot_inject_command_or_project(self):
        with patch.object(self.c,'service_state',return_value=state()),patch.dict(
                'os.environ',{'GCP_LOG_VIEW_LOCATION':'global/other-project'}),patch.object(self.c,'g') as g:
            with self.assertRaises(ControlError):self.c.diagnose()
        g.assert_not_called()
    def test_diagnostics_whitelist_discards_payload_and_identity(self):
        stamp='2026-11-17T10:30:21.123Z'
        entries=[dict(timestamp=stamp,textPayload='FAKE_CARD_AND_SECRET'),
                 dict(timestamp=stamp,textPayload='GROUP_SALE_EVENT_LAG_MS 123'),
                 dict(timestamp=stamp,textPayload='GROUP_SALE_EVENT_LAG_MS 123 FAKE_CARD'),
                 dict(timestamp=stamp,httpRequest=dict(latency='360.123s',status=200,requestUrl='FAKE_PRIVATE_URL',remoteIp='FAKE_PRIVATE_IP')),
                 dict(timestamp=stamp,textPayload='Starting new instance. Reason: AUTOSCALING - FAKE_PRIVATE_DETAIL'),
                 dict(timestamp=stamp,textPayload='Default STARTUP TCP probe succeeded FAKE_PRIVATE_DETAIL')]
        result=self.c.safe_timings(entries)
        self.assertEqual(len(result),4)
        self.assertNotIn('FAKE_',json.dumps(result))
        self.assertEqual(result[1]['seconds'],360.123)
    def test_request_classification_keeps_only_known_routes_and_valid_receive_time(self):
        stamp='2026-11-17T10:24:44.998Z'
        entries=[dict(timestamp=stamp,receiveTimestamp='2026-11-17T10:24:46.800Z',httpRequest=dict(
            latency='1.8s',status=200,requestUrl='https://private.invalid/callback?secret=FAKE_SECRET')),
            dict(timestamp=stamp,receiveTimestamp='FAKE_SECRET',httpRequest=dict(
            latency='0.1s',status=200,requestUrl='https://private.invalid/')),
            dict(timestamp=stamp,httpRequest=dict(latency='0.1s',status=404,
            requestUrl='https://private.invalid/FAKE_CARD'))]
        result=self.c.safe_timings(entries)
        self.assertEqual([r['route'] for r in result],['callback','health','other'])
        self.assertEqual(result[0]['received_at'],'2026-11-17T10:24:46.800Z')
        self.assertNotIn('received_at',result[1])
        self.assertNotIn('FAKE_',json.dumps(result))
        self.assertNotIn('private.invalid',json.dumps(result))
    def test_startup_marker_is_numeric_only(self):
        stamp='2026-11-17T13:32:00.123Z'
        result=self.c.safe_timings([dict(timestamp=stamp,textPayload='BOT_APPLICATION_READY_MS 875'),
            dict(timestamp=stamp,textPayload='BOT_APPLICATION_READY_MS 875 PRIVATE_VALUE')])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['timing'],'BOT_APPLICATION_READY_MS 875')
    def test_incident_errors_export_only_type_and_validated_revision(self):
        stamp='2026-11-17T15:14:00.123Z'
        entries=[dict(timestamp=stamp,textPayload='gspread.exceptions.APIError: PRIVATE_RESPONSE',
            resource=dict(labels=dict(revision_name='line-bot-calc-gh-123-1'))),
            dict(timestamp=stamp,textPayload='[2026] [1] [CRITICAL] WORKER TIMEOUT (pid: PRIVATE)',
            resource=dict(labels=dict(revision_name='PRIVATE_VALUE'))),
            dict(timestamp=stamp,textPayload='PRIVATE_FAILURE',severity='ERROR')]
        result=self.c.safe_timings(entries)
        self.assertEqual([r['code'] for r in result],['APIError','worker_timeout','unclassified'])
        self.assertEqual(result[0]['revision'],'line-bot-calc-gh-123-1')
        self.assertNotIn('revision',result[1])
        self.assertNotIn('PRIVATE',json.dumps(result))
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
    def test_entrypoint_override_stops_image_change_before_build(self):
        s=state()
        s['spec']['template']['spec']['containers'][0]['args']=['old-startup']
        with patch.object(self.c,'service_state',return_value=s),patch('ops.cloud_control.run') as run:
            with self.assertRaisesRegex(ControlError,'entrypoint overrides'):
                self.c.deploy(SHA,OLD,'123-1')
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
        with patch.object(self.c,'report_state',return_value=report_job()),patch.object(
                self.c,'report_can_act_as',return_value=False):
            result=self.c.report_snapshot()
        self.assertNotIn('FAKE_PRIVATE_VALUE',json.dumps(result))
        self.assertNotIn('serviceAccountName',json.dumps(result))
        self.assertFalse(result['can_act_as_runtime'])
    def test_changed_report_stops_before_build(self):
        with patch.object(self.c,'report_state',return_value=report_job()),patch('ops.cloud_control.run') as run:
            with self.assertRaises(ControlError):
                self.c.deploy_report(SHA,'report-generation-2')
        run.assert_not_called()
    def test_runtime_permission_check_reports_absent_permission_without_token(self):
        import io
        job=report_job()
        job['spec']['template']['spec']['template']['spec']['serviceAccountName']='runtime@example-project.iam.gserviceaccount.com'
        with patch('ops.cloud_control.run',return_value='FAKE_ACCESS_TOKEN'),patch(
                'ops.cloud_control.urllib.request.urlopen',return_value=io.BytesIO(b'{"permissions":[]}')) as urlopen:
            self.assertFalse(self.c.report_can_act_as(job))
        request=urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data),{'permissions':['iam.serviceAccounts.actAs']})
        self.assertTrue(request.full_url.endswith('/runtime@example-project.iam.gserviceaccount.com:testIamPermissions'))
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
