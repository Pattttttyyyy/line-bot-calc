"""Owner-run, user-approved read access to ONE service's existing log bucket.

No secret/credential values, log payloads, inventory, deployments or new storage.
Importing this module makes no calls. Run in the owner's logged-in Cloud Shell.
"""
import json
import re
import subprocess

PROJECT = 'line-bot-sheet-510416'
SERVICE = 'line-bot-calc'
VIEW = 'line_bot_calc_latency'
MEMBER = 'serviceAccount:line-bot-ci-deploy@'+PROJECT+'.iam.gserviceaccount.com'
ROLE = 'roles/logging.viewAccessor'
FILTER = ('source("projects/'+PROJECT+'") AND resource.type="cloud_run_revision" '
          'AND resource.labels.service_name="'+SERVICE+'" '
          'AND resource.labels.location="asia-east1"')


def run_gcloud(args, allow_missing=False):
    result = subprocess.run(['gcloud', *args, '--project='+PROJECT, '--format=json', '--quiet'],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        # Do not print CLI output, which may contain unrelated IAM information.
        detail = (result.stdout+result.stderr).lower()
        if allow_missing and ('not_found' in detail or 'does not exist' in detail):
            return None
        if 'permission_denied' in detail or 'permission denied' in detail or '403' in detail:
            raise RuntimeError('permission_denied: please use the project owner Cloud Shell')
        raise RuntimeError('gcloud_operation_failed')
    return json.loads(result.stdout) if result.stdout.strip() else {}


def validate_view(view, resource):
    if view.get('name') != resource or ''.join(view.get('filter','').split()) != ''.join(FILTER.split()):
        raise RuntimeError('Existing view differs from the approved service-only scope; stopped')


def configure(run=run_gcloud):
    sink = run(['logging', 'sinks', 'describe', '_Default'])
    expected = r'logging\.googleapis\.com/projects/'+re.escape(PROJECT)+r'/locations/([a-z][a-z0-9-]{0,62})/buckets/_Default'
    match = re.fullmatch(expected, sink.get('destination',''))
    if sink.get('disabled') or not match:
        raise RuntimeError('Unexpected or disabled default log sink; stopped without creating storage')
    location = match[1]
    resource = 'projects/'+PROJECT+'/locations/'+location+'/buckets/_Default/views/'+VIEW
    scope = ['--bucket=_Default', '--location='+location]
    view = run(['logging', 'views', 'describe', VIEW, *scope], allow_missing=True)
    if view is None:
        run(['logging', 'views', 'create', VIEW, *scope, '--log-filter='+FILTER,
             '--description=Approved read-only diagnostics for line-bot-calc only'])
        view = run(['logging', 'views', 'describe', VIEW, *scope])
    # Verify the server-stored scope BEFORE granting anyone access.
    validate_view(view, resource)
    run(['logging', 'views', 'add-iam-policy-binding', VIEW, *scope,
         '--member='+MEMBER, '--role='+ROLE, '--condition=None'])
    policy = run(['logging', 'views', 'get-iam-policy', VIEW, *scope])
    if not any(binding.get('role') == ROLE and MEMBER in binding.get('members',[])
               and not binding.get('condition') for binding in policy.get('bindings',[])):
        raise RuntimeError('Read-only view binding could not be verified')
    validate_view(run(['logging', 'views', 'describe', VIEW, *scope]), resource)
    return dict(result='LOG_VIEW_ACCESS_VERIFIED', location=location, view=resource,
                member=MEMBER, role=ROLE)


if __name__ == '__main__':
    try:
        print(json.dumps(configure(), ensure_ascii=False, indent=2))
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print('LOG_VIEW_SETUP_STOPPED', str(exc) if isinstance(exc,RuntimeError) else 'timeout')
        raise SystemExit(1) from None
