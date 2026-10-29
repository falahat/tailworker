"""Native Cloud Run resources; Google owns discovery and execution history."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request

JOB_EXECUTION = dict(taskCount=1, parallelism=1)
JOB_TASK = dict(maxRetries=0)
SERVICE_ANNOTATIONS = {'run.googleapis.com/ingress': 'internal', 'run.googleapis.com/invoker-iam-disabled': 'false',
                       'run.googleapis.com/maxScale': '1', 'run.googleapis.com/minScale': '0'}
REVISION_ANNOTATIONS = {'autoscaling.knative.dev/maxScale': '1', 'autoscaling.knative.dev/minScale': '0'}


def configuration():
    root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'tailworker'
    config = tomllib.loads((root / 'config.toml').read_text())
    cloud, policy = config['gcp'], config['cloud_run']
    for key in ('project', 'region'):
        if not re.fullmatch(r'[a-z][a-z0-9-]+', cloud[key]):
            raise ValueError('configure gcp.' + key)
    for key in ('network', 'subnet'):
        if not re.fullmatch(r'[a-z][a-z0-9-]+', policy[key]):
            raise ValueError('configure cloud_run.' + key)
    if not re.fullmatch(r'[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com', policy['service_account']):
        raise ValueError('configure a dedicated cloud_run.service_account')
    if not policy['invokers'] or any(not re.fullmatch(r'(user|serviceAccount):[^\s@]+@[^\s@]+', member) for member in policy['invokers']):
        raise ValueError('configure explicit cloud_run.invokers')
    return cloud, policy


def sdk(cloud, *arguments):
    command = ['gcloud', '--quiet', '--project=' + cloud['project'], '--format=json',
               'run', *arguments, '--region=' + cloud['region']]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=dict(os.environ, CLOUDSDK_CORE_LOG_HTTP='false'))
    if result.returncode:
        raise ValueError('Google operation failed; inspect the named resource before repeating a mutation')
    return json.loads(result.stdout) if result.stdout.strip() else None


def name(value):
    if not re.fullmatch(r'[a-z](?:[a-z0-9-]{0,44}[a-z0-9])?', value):
        raise ValueError('provide a resource name, not a URL or identifier')
    return 'tw-' + value


def reference(cloud, kind, value):
    return 'projects/' + cloud['project'] + '/locations/' + cloud['region'] + '/' + kind + 's/' + value


def owned(resource, expected=None):
    metadata = resource['metadata']
    value = metadata['name']
    if metadata.get('labels', {}).get('tailworker') != '1' or not value.startswith('tw-') or (expected and value != expected):
        raise ValueError('resource is not owned by tailworker')
    name(value[3:])
    return resource


def discover(cloud, kind, value=None):
    resources = [sdk(cloud, kind + 's', 'describe', value)] if value else sdk(cloud, kind + 's', 'list', '--filter=metadata.labels.tailworker=1')
    return [owned(resource, value) for resource in resources]


def descriptor(cloud, kind, resource):
    value = resource['metadata']['name']
    status = resource.get('status', {})
    ready = next((item['status'] for item in status.get('conditions', []) if item['type'] == 'Ready'), 'Unknown')
    result = dict(kind=kind, name=value[3:], id=reference(cloud, kind, value), ready=ready)
    if kind == 'service':
        url = status.get('url')
        if url and not re.fullmatch(r'https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.run\.app', url):
            raise ValueError('unexpected Cloud Run service URL')
        result.update(url=url, audience=url, authentication='google-id-token',
                      ingress=resource['metadata'].get('annotations', {}).get('run.googleapis.com/ingress', 'unknown'))
    else:
        result['latest_execution'] = status.get('latestCreatedExecution', {}).get('name')
    return result


def manifest(args, policy):
    spec = json.loads(args.file.read_text()) if args.file else dict(containers=[dict(
        image=args.image, resources=dict(limits=dict(cpu=args.cpu, memory=args.memory)))])
    if not isinstance(spec, dict) or set(spec) - {'containers', 'volumes', 'nodeSelector', 'timeoutSeconds', 'containerConcurrency'}:
        raise ValueError('file must contain a native container specification, not a whole resource')
    if not spec.get('containers') or any(not container.get('image') for container in spec['containers']):
        raise ValueError('provide container images')
    spec['serviceAccountName'] = policy['service_account']
    annotations = {'run.googleapis.com/network-interfaces': json.dumps([
        dict(network=policy['network'], subnetwork=policy['subnet'])]),
        'run.googleapis.com/vpc-access-egress': 'all-traffic'}
    metadata = dict(labels=dict(tailworker='1'), annotations=annotations)
    resource = dict(apiVersion='run.googleapis.com/v1', kind='Job',
                    metadata=dict(name=name(args.name), labels=dict(tailworker='1')))
    if args.kind == 'job':
        spec.update(JOB_TASK)
        resource['spec'] = dict(template=dict(metadata=metadata,
            spec=dict(**JOB_EXECUTION, template=dict(spec=spec))))
    else:
        resource.update(apiVersion='serving.knative.dev/v1', kind='Service')
        resource['metadata']['annotations'] = dict(SERVICE_ANNOTATIONS)
        annotations.update(REVISION_ANNOTATIONS)
        resource['spec'] = dict(template=dict(metadata=metadata, spec=spec))
    return resource


def iam(cloud, kind, value, invokers):
    policy = sdk(cloud, kind + 's', 'get-iam-policy', value)
    if any(member in ('allUsers', 'allAuthenticatedUsers') for binding in policy.get('bindings', []) for member in binding['members']):
        raise ValueError('resource has a public IAM grant')
    roles = {'roles/run.invoker', 'roles/run.servicesInvoker', 'roles/run.jobsExecutor', 'roles/run.jobsExecutorWithOverrides'}
    for binding in policy.get('bindings', []):
        if binding['role'] in roles and (binding['role'].endswith('WithOverrides') or set(binding['members']) - set(invokers)):
            raise ValueError('existing execution grants differ from owner policy; review them in Google IAM')
    return policy


def protected(cloud, policy, kind, resource):
    value = resource['metadata']['name']
    template = resource['spec']['template']
    pod = template['spec']
    if kind == 'job':
        execution = pod
        pod = execution['template']['spec']
        if not JOB_EXECUTION.items() <= execution.items() or not JOB_TASK.items() <= pod.items():
            raise ValueError('job must have one task, one parallel instance and no retries')
    else:
        settings = {'run.googleapis.com/invoker-iam-disabled': 'false', 'run.googleapis.com/minScale': '0',
                    **resource['metadata'].get('annotations', {})}
        if not SERVICE_ANNOTATIONS.items() <= settings.items():
            raise ValueError('service must require private ingress and IAM')
        revision = {'autoscaling.knative.dev/minScale': '0', **template['metadata']['annotations']}
        if not REVISION_ANNOTATIONS.items() <= revision.items():
            raise ValueError('service must be limited to one instance')
    if pod['serviceAccountName'] != policy['service_account']:
        raise ValueError('resource service identity differs from owner configuration')
    annotations = template['metadata']['annotations']
    if annotations.get('run.googleapis.com/vpc-access-egress') != 'all-traffic' or json.loads(annotations.get('run.googleapis.com/network-interfaces', 'null')) != [dict(network=policy['network'], subnetwork=policy['subnet'])]:
        raise ValueError('resource must use the approved VPC egress policy')
    iam(cloud, kind, value, policy['invokers'])


def deploy(cloud, policy, args):
    resource = manifest(args, policy)
    if args.dry_run:
        template = resource['spec']['template']['spec']
        containers = (template['template']['spec'] if args.kind == 'job' else template)['containers']
        return dict(action='deploy', id=reference(cloud, args.kind, name(args.name)),
                    images=[item['image'] for item in containers], limits=[item.get('resources', {}).get('limits', {}) for item in containers],
                    service_account=policy['service_account'], network=policy['network'], instances=1)
    existing = sdk(cloud, args.kind + 's', 'list', '--filter=metadata.name=' + name(args.name))
    for item in existing:
        owned(item, name(args.name))
        iam(cloud, args.kind, name(args.name), policy['invokers'])
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'resource.json'
        path.write_text(json.dumps(resource))
        path.chmod(0o600)
        sdk(cloud, args.kind + 's', 'replace', str(path))
        authorization = iam(cloud, args.kind, name(args.name), policy['invokers'])
        role = 'roles/run.invoker' if args.kind == 'service' else 'roles/run.jobsExecutor'
        granted = {member for item in authorization.get('bindings', []) if item['role'] == role for member in item['members']}
        missing = set(policy['invokers']) - granted
        if missing:
            binding = next((item for item in authorization.get('bindings', []) if item['role'] == role and not item.get('condition')), None)
            if binding:
                binding['members'] = sorted(set(binding['members']) | missing)
            else:
                authorization.setdefault('bindings', []).append(dict(role=role, members=sorted(missing)))
        path.write_text(json.dumps(authorization))
        sdk(cloud, args.kind + 's', 'set-iam-policy', name(args.name), str(path))
    created = discover(cloud, args.kind, name(args.name))[0]
    protected(cloud, policy, args.kind, created)
    return descriptor(cloud, args.kind, created)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return None


def token(cloud, purpose, *options):
    result = subprocess.run(['gcloud', '--quiet', '--project=' + cloud['project'], 'auth', 'print-' + purpose + '-token', *options],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(os.environ, CLOUDSDK_CORE_LOG_HTTP='false'))
    credential = result.stdout.decode().strip()
    if result.returncode or not re.fullmatch(r'[A-Za-z0-9_.-]+', credential):
        raise ValueError('Google token unavailable; configure the approved caller identity')
    return credential


def http(url, credential, body=None, content_type='application/json'):
    if body and len(body) > 16 * 1024 * 1024:
        raise ValueError('request exceeds 16 MiB; stage large inputs separately')
    request = urllib.request.Request(url, data=body,
        headers={'Authorization': 'Bearer ' + credential, 'Content-Type': content_type})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request) as response:
        content = response.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            raise ValueError('response exceeds 16 MiB')
        return response.status, content


def invoke(cloud, policy, args, target):
    if target['ready'] != 'True' or not target['url']:
        raise ValueError('service is not ready')
    if not args.path.startswith('/') or args.path.startswith('//') or any(c.isspace() or c in '#\\' for c in args.path):
        raise ValueError('provide a relative HTTP path')
    account = policy.get('invoke_as', '')
    if 'serviceAccount:' + account not in policy['invokers']:
        raise ValueError('configure cloud_run.invoke_as as an approved caller service account')
    credential = token(cloud, 'identity', '--audiences=' + target['audience'], '--impersonate-service-account=' + account)
    body = None
    if args.data:
        with args.data.open('rb') as stream:
            body = stream.read(16 * 1024 * 1024 + 1)
    status, body = http(target['url'] + args.path, credential, body, args.content_type)
    return dict(status=status, body=body.decode('utf-8', 'replace'))


def completed(execution):
    return next((item['status'] for item in execution.get('status', {}).get('conditions', []) if item['type'] == 'Completed'), 'Unknown')


def run_job(cloud, args, value):
    url = 'https://' + cloud['region'] + '-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/' + cloud['project'] + '/jobs/' + value + ':run'
    _, body = http(url, token(cloud, 'access'), b'{}')
    execution = json.loads(body)
    identity = execution['metadata']['name']
    while args.wait and completed(execution) == 'Unknown':
        time.sleep(1)
        execution = sdk(cloud, 'jobs', 'executions', 'describe', identity)
    return dict(job=reference(cloud, 'job', value), execution=identity,
                succeeded=completed(execution) == 'True' if args.wait else None, status=execution.get('status', {}))


def execute(cloud, policy, args):
    if args.action == 'deploy':
        return deploy(cloud, policy, args)
    if args.action == 'ls':
        return [descriptor(cloud, args.kind, item) for item in discover(cloud, args.kind)]
    value = name(args.name)
    resource = discover(cloud, args.kind, value)[0]
    if args.action not in ('delete', 'logs'):
        protected(cloud, policy, args.kind, resource)
    if args.action == 'describe':
        return descriptor(cloud, args.kind, resource)
    if args.action == 'delete':
        sdk(cloud, args.kind + 's', 'delete', value)
        return dict(deleted=reference(cloud, args.kind, value))
    if args.action == 'logs':
        return sdk(cloud, args.kind + 's', 'logs', 'read', value, '--limit=100')
    if args.kind == 'service':
        return invoke(cloud, policy, args, descriptor(cloud, args.kind, resource))
    executions = sdk(cloud, 'jobs', 'executions', 'list', '--job=' + value)
    if args.action == 'executions':
        return [dict(id=item['metadata']['name'], status=item.get('status', {})) for item in executions]
    if args.action == 'cancel':
        matches = [item for item in executions if item['metadata']['name'] == args.execution]
        if len(matches) != 1:
            raise ValueError('execution does not belong to this job')
        sdk(cloud, 'jobs', 'executions', 'cancel', args.execution)
        return dict(cancelled=args.execution)
    if any(not item.get('status', {}).get('completionTime') for item in executions):
        raise ValueError('job already has an active execution')
    return run_job(cloud, args, value)


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('job', 'service'))
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('ls')
    deployment = commands.add_parser('deploy')
    deployment.add_argument('name')
    source = deployment.add_mutually_exclusive_group(required=True)
    source.add_argument('--image')
    source.add_argument('--file', type=Path)
    deployment.add_argument('--cpu', default='1')
    deployment.add_argument('--memory', default='512Mi')
    deployment.add_argument('--dry-run', action='store_true')
    actions = ('invoke',) if arguments[:1] == ['service'] else ('executions', 'cancel', 'run')
    for action in ('describe', 'delete', 'logs', *actions):
        command = commands.add_parser(action)
        command.add_argument('name')
        if action == 'cancel':
            command.add_argument('execution')
        if action == 'run':
            command.add_argument('--wait', action='store_true')
        if action == 'invoke':
            command.add_argument('--data', type=Path)
            command.add_argument('--path', default='/')
            command.add_argument('--content-type', default='application/json')
    args = parser.parse_args(arguments)
    try:
        result = execute(*configuration(), args)
        print(json.dumps(result, sort_keys=True))
        return int(args.action == 'run' and args.wait and not result['succeeded'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('tailworker: Cloud Run operation failed: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
