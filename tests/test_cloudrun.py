import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloudrun as cr


class CloudRun(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cloud = dict(project='example-project', region='us-central1')
        self.policy = dict(service_account='worker@example-project.iam.gserviceaccount.com',
            network='tailworker', subnet='tailworker', invoke_as='caller@example-project.iam.gserviceaccount.com',
            invokers=['serviceAccount:caller@example-project.iam.gserviceaccount.com'])
        self.args = argparse.Namespace(kind='service', action='deploy', name='hello', file=None,
            image='us-docker.pkg.dev/cloudrun/container/hello', cpu='1', memory='512Mi', dry_run=False)
        self.calls, self.resources = [], []
        self.authorization = dict(bindings=[])
        self.executions = []
        self.enterContext(mock.patch.object(cr.subprocess, 'run', side_effect=self.sdk))

    def sdk(self, command, **options):
        self.calls.append(command)
        self.assertEqual(options.get('env', {}).get('CLOUDSDK_CORE_LOG_HTTP'), 'false')
        if 'auth' in command:
            return subprocess.CompletedProcess(command, 0, stdout=b'fixture.access.token')
        operation = command[5:-1]
        if operation[1] == 'list':
            result = self.resources
        elif operation[1] == 'describe':
            result = self.resources[0]
        elif operation[1] == 'replace':
            resource = json.loads(Path(operation[2]).read_text())
            self.assertEqual(Path(operation[2]).stat().st_mode & 0o777, 0o600)
            resource['status'] = dict(conditions=[dict(type='Ready', status='True')])
            if operation[0] == 'services':
                resource['status']['url'] = 'https://tw-hello-123.us-central1.run.app'
            self.resources = [resource]
            result = resource
        elif operation[1] == 'get-iam-policy':
            result = self.authorization
        elif operation[1] == 'set-iam-policy':
            self.authorization = json.loads(Path(operation[3]).read_text())
            result = self.authorization
        elif operation[1] == 'executions':
            result = self.executions if operation[2] == 'list' else dict(metadata=dict(name='tw-hello-ab123'),
                status=dict(conditions=[dict(type='Completed', status='True')]))
        elif operation[1] == 'logs':
            result = [dict(textPayload='work complete')]
        elif operation[1] == 'delete':
            self.resources = []
            result = None
        else:
            self.fail(str(operation))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(result).encode())

    def test_service_deployment_discovery_and_cleanup_use_provider_records(self):
        target = cr.deploy(self.cloud, self.policy, self.args)
        self.assertEqual(target['id'], 'projects/example-project/locations/us-central1/services/tw-hello')
        self.assertEqual(target['audience'], target['url'])
        self.assertEqual(self.authorization['bindings'], [dict(role='roles/run.invoker', members=self.policy['invokers'])])
        self.args.action = 'ls'
        self.assertEqual(cr.execute(self.cloud, self.policy, self.args), [target])
        self.args.action = 'describe'
        self.assertEqual(cr.execute(self.cloud, self.policy, self.args), target)
        self.args.action = 'delete'
        self.assertEqual(cr.execute(self.cloud, self.policy, self.args), dict(deleted=target['id']))
        self.assertEqual(self.resources, [])

    def test_job_execution_ids_logs_and_cancel_are_scoped_to_job(self):
        self.args.kind = 'job'
        target = cr.deploy(self.cloud, self.policy, self.args)
        self.assertEqual(self.authorization['bindings'][0]['role'], 'roles/run.jobsExecutor')
        self.args.action, self.args.wait = 'run', True
        initial = dict(metadata=dict(name='tw-hello-ab123'), status=dict(conditions=[dict(type='Completed', status='Unknown')]))
        with mock.patch.object(cr, 'http', return_value=(200, json.dumps(initial).encode())) as request, mock.patch.object(cr.time, 'sleep'):
            result = cr.execute(self.cloud, self.policy, self.args)
        self.assertEqual(result['job'], target['id'])
        self.assertEqual(result['execution'], 'tw-hello-ab123')
        self.assertTrue(result['succeeded'])
        request.assert_called_once_with('https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/example-project/jobs/tw-hello:run', 'fixture.access.token', b'{}')
        self.executions = [dict(metadata=dict(name=result['execution']), status=dict(runningCount=1))]
        self.args.action = 'executions'
        self.assertEqual(cr.execute(self.cloud, self.policy, self.args)[0]['id'], result['execution'])
        self.args.action, self.args.execution = 'cancel', result['execution']
        self.assertEqual(cr.execute(self.cloud, self.policy, self.args), dict(cancelled=result['execution']))
        self.args.action = 'logs'
        self.assertEqual(cr.execute(self.cloud, self.policy, self.args)[0]['textPayload'], 'work complete')

    def test_dry_run_and_native_multi_container_gpu_spec_need_no_sdk(self):
        self.args.kind, self.args.dry_run = 'job', True
        self.args.file = self.root / 'workload.json'
        spec = dict(containers=[dict(image='registry.example/compute', command=['python', 'main.py'],
            resources=dict(limits={'cpu': '8', 'memory': '32Gi', 'nvidia.com/gpu': '1'})),
            dict(image='registry.example/sidecar')], nodeSelector={'run.googleapis.com/accelerator': 'nvidia-l4'},
            volumes=[dict(name='work', emptyDir=dict(medium='Memory', sizeLimit='1Gi'))], timeoutSeconds=3600)
        self.args.file.write_text(json.dumps(spec))
        result = cr.deploy(self.cloud, self.policy, self.args)
        self.assertEqual(result['images'], ['registry.example/compute', 'registry.example/sidecar'])
        self.assertEqual(self.calls, [])
        resource = cr.manifest(self.args, self.policy)
        self.assertEqual(json.loads(resource['spec']['template']['metadata']['annotations']['run.googleapis.com/network-interfaces']),
                         [dict(network='tailworker', subnetwork='tailworker')])
        task = resource['spec']['template']['spec']
        self.assertEqual((task['parallelism'], task['taskCount'], task['template']['spec']['maxRetries']), (1, 1, 0))
        self.assertEqual(task['template']['spec']['containers'], spec['containers'])

    def test_authenticated_invocation_uses_discovered_url_and_relative_path(self):
        target = cr.deploy(self.cloud, self.policy, self.args)
        self.args.action, self.args.path, self.args.content_type = 'invoke', '/infer', 'application/json'
        self.args.data = self.root / 'input.json'
        self.args.data.write_text('{"input": 2}')
        response = io.BytesIO(b'{"result": 4}')
        response.status = 200
        with mock.patch.object(cr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=b'fixture.identity.token')) as auth, mock.patch.object(cr.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            result = cr.invoke(self.cloud, self.policy, self.args, target)
            request = opener.return_value.open.call_args.args[0]
        self.assertIn('--audiences=' + target['url'], auth.call_args.args[0])
        self.assertIn('--impersonate-service-account=' + self.policy['invoke_as'], auth.call_args.args[0])
        self.assertEqual(request.full_url, target['url'] + '/infer')
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture.identity.token')
        self.assertEqual(request.data, self.args.data.read_bytes())
        self.assertEqual(result, dict(status=200, body='{"result": 4}'))
        self.assertNotIn('fixture.identity.token', json.dumps(result))

    def test_existing_iam_conditions_bindings_and_etag_are_preserved(self):
        self.authorization = dict(etag='existing-version', bindings=[dict(role='roles/run.viewer', members=['user:admin@example.test']),
            dict(role='roles/run.invoker', members=self.policy['invokers'], condition=dict(title='bounded', expression='true'))])
        before = json.loads(json.dumps(self.authorization))
        cr.deploy(self.cloud, self.policy, self.args)
        self.assertEqual(self.authorization, before)

    def test_failed_submission_is_not_repeated(self):
        self.args.kind, self.args.wait = 'job', False
        with mock.patch.object(cr, 'http', side_effect=OSError('connection lost')) as request:
            with self.assertRaises(OSError):
                cr.run_job(self.cloud, self.args, 'tw-hello')
        self.assertEqual(request.call_count, 1)

    def test_public_cli_dry_run_works_without_linux_worker_dependencies(self):
        config = self.root / 'tailworker'
        config.mkdir()
        (config / 'config.toml').write_text('[gcp]\nproject="example-project"\nregion="us-central1"\n'
            '[cloud_run]\nservice_account="worker@example-project.iam.gserviceaccount.com"\n'
            'network="tailworker"\nsubnet="tailworker"\ninvokers=["user:owner@example.test"]\n')
        with mock.patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root)), contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(cr.main(['service', 'deploy', 'hello', '--image', self.args.image, '--dry-run']), 0)
        self.assertEqual(json.loads(captured.getvalue())['action'], 'deploy')
        self.assertEqual(self.calls, [])
        with subprocess.Popen([sys.executable, str(Path(cr.__file__).with_name('tailworker')),
                'job', 'deploy', 'hello', '--image', self.args.image, '--dry-run'],
                env=dict(os.environ, XDG_CONFIG_HOME=str(self.root)), stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            stdout, stderr = process.communicate()
        self.assertEqual((process.returncode, stderr), (0, b''))
        self.assertEqual(json.loads(stdout)['id'], 'projects/example-project/locations/us-central1/jobs/tw-hello')
