"""Fake gcloud only. These tests cannot contact Google or change IAM."""
import copy
import unittest
from unittest.mock import Mock
from ops.grant_service_log_view import configure, run_gcloud, FILTER, PROJECT, MEMBER, ROLE, VIEW


class LogViewAccessTests(unittest.TestCase):
    def test_cli_permission_error_never_exposes_raw_output(self):
        from unittest.mock import patch
        fake=Mock(returncode=1,stdout='FAKE_PRIVATE_OUTPUT',stderr='PERMISSION_DENIED FAKE_PRIVATE_DETAIL')
        with patch('ops.grant_service_log_view.subprocess.run',return_value=fake):
            with self.assertRaises(RuntimeError) as raised:run_gcloud(['logging','sinks','describe','_Default'])
        self.assertNotIn('FAKE_PRIVATE',str(raised.exception))

    def fixture(self,location='global',missing=False):
        resource='projects/'+PROJECT+'/locations/'+location+'/buckets/_Default/views/'+VIEW
        view=dict(name=resource,filter=FILTER)
        responses=[dict(destination='logging.googleapis.com/projects/'+PROJECT+'/locations/'+location+'/buckets/_Default'),
                   None if missing else view]
        if missing:responses.extend([{},view])
        responses.extend([{},dict(bindings=[dict(role=ROLE,members=[MEMBER])]),view])
        return Mock(side_effect=copy.deepcopy(responses)), resource

    def test_new_service_only_view_is_verified_before_binding(self):
        run,resource=self.fixture(missing=True)
        result=configure(run)
        self.assertEqual(result['view'],resource)
        calls=[c.args[0] for c in run.call_args_list]
        self.assertIn('--log-filter='+FILTER,calls[2])
        self.assertEqual(calls[3][:3],['logging','views','describe'])
        self.assertEqual(calls[4][:3],['logging','views','add-iam-policy-binding'])
        self.assertIn('--member='+MEMBER,calls[4]);self.assertIn('--role='+ROLE,calls[4])
        self.assertFalse(any('projects' in c or 'secrets' in c or 'run' in c or 'buckets' in c for c in calls))

    def test_existing_exact_view_is_reused_without_update(self):
        run,_=self.fixture()
        self.assertEqual(configure(run)['result'],'LOG_VIEW_ACCESS_VERIFIED')
        self.assertFalse(any('create' in c.args[0] or 'update' in c.args[0] for c in run.call_args_list))

    def test_actual_existing_bucket_location_is_discovered(self):
        run,resource=self.fixture('asia-east1')
        self.assertEqual(configure(run)['view'],resource)
        self.assertIn('--location=asia-east1',run.call_args_list[1].args[0])

    def test_broader_existing_view_stops_before_grant(self):
        run,_=self.fixture()
        run.side_effect=[dict(destination='logging.googleapis.com/projects/'+PROJECT+'/locations/global/buckets/_Default'),
                         dict(name='projects/'+PROJECT+'/locations/global/buckets/_Default/views/'+VIEW,
                              filter='resource.type="cloud_run_revision"')]
        with self.assertRaises(RuntimeError):configure(run)
        self.assertEqual(run.call_count,2)

    def test_another_project_bucket_is_never_modified(self):
        run=Mock(return_value=dict(destination='logging.googleapis.com/projects/another-project/locations/global/buckets/_Default'))
        with self.assertRaises(RuntimeError):configure(run)
        self.assertEqual(run.call_count,1)

    def test_disabled_sink_is_never_enabled(self):
        run=Mock(return_value=dict(disabled=True,destination='logging.googleapis.com/projects/'+PROJECT+'/locations/global/buckets/_Default'))
        with self.assertRaises(RuntimeError):configure(run)
        self.assertEqual(run.call_count,1)

    def test_failed_binding_verification_never_claims_success(self):
        run,resource=self.fixture()
        view=dict(name=resource,filter=FILTER)
        run.side_effect=[dict(destination='logging.googleapis.com/projects/'+PROJECT+'/locations/global/buckets/_Default'),view,{},dict(bindings=[])]
        with self.assertRaises(RuntimeError):configure(run)

    def test_view_filter_change_after_grant_is_detected(self):
        run,resource=self.fixture()
        view=dict(name=resource,filter=FILTER)
        run.side_effect=[dict(destination='logging.googleapis.com/projects/'+PROJECT+'/locations/global/buckets/_Default'),view,{},dict(bindings=[dict(role=ROLE,members=[MEMBER])]),dict(name=resource,filter='')]
        with self.assertRaises(RuntimeError):configure(run)
