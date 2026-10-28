"""Native Cloud Run resources; Google owns discovery and execution history."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request


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
                            env=dict(os.environ, CLOUDSDK_CORE_MAX_RETRIES='0'))
    if result.returncode:
        raise ValueError('Google operation failed; inspect the named resource before repeating a mutation')
    return json.loads(result.stdout) if result.stdout.strip() else None


def name(value):
    if not re.fullmatch(r'[a-z](?:[a-z0-9-]{0,57}[a-z0-9])?', value):
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
    selector = 'metadata.name=' + value if value else 'metadata.labels.tailworker=1'
    resources = sdk(cloud, kind + 's', 'list', '--filter=' + selector)
    if value and len(resources) != 1:
        raise ValueError('resource absent or ambiguous; no mutation performed')
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
        result.update(url=url, audience=url, authentication='google-id-token', ingress='internal')
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
        spec['maxRetries'] = 0
        resource['spec'] = dict(template=dict(metadata=dict(labels=dict(tailworker='1')),
            spec=dict(taskCount=1, parallelism=1, template=dict(metadata=metadata, spec=spec))))
    else:
        resource.update(apiVersion='serving.knative.dev/v1', kind='Service')
        resource['metadata']['annotations'] = {'run.googleapis.com/ingress': 'internal',
            'run.googleapis.com/invoker-iam-disabled': 'false', 'run.googleapis.com/maxScale': '1',
            'run.googleapis.com/minScale': '0'}
        annotations.update({'autoscaling.knative.dev/maxScale': '1', 'autoscaling.knative.dev/minScale': '0'})
        resource['spec'] = dict(template=dict(metadata=metadata, spec=spec))
    return resource


def iam(cloud, kind, value):
    policy = sdk(cloud, kind + 's', 'get-iam-policy', value)
    if any(member in ('allUsers', 'allAuthenticatedUsers') for binding in policy.get('bindings', []) for member in binding['members']):
        raise ValueError('resource has a public IAM grant')
    return policy


def protected(cloud, policy, kind, resource):
    value = resource['metadata']['name']
    template = resource['spec']['template']
    if kind == 'job':
        execution = template['spec']
        template = execution['template']
        if execution.get('taskCount') != 1 or execution.get('parallelism') != 1 or template['spec'].get('maxRetries') != 0:
            raise ValueError('job must have one task, one parallel instance and no retries')
    else:
        settings = resource['metadata'].get('annotations', {})
        if settings.get('run.googleapis.com/ingress') != 'internal' or settings.get('run.googleapis.com/invoker-iam-disabled', 'false') != 'false':
            raise ValueError('service must require private ingress and IAM')
        if settings.get('run.googleapis.com/maxScale') != '1' or template['metadata']['annotations'].get('autoscaling.knative.dev/maxScale') != '1':
            raise ValueError('service must be limited to one instance')
    if template['spec']['serviceAccountName'] != policy['service_account']:
        raise ValueError('resource service identity differs from owner configuration')
    annotations = template['metadata']['annotations']
    if annotations.get('run.googleapis.com/vpc-access-egress') != 'all-traffic' or json.loads(annotations.get('run.googleapis.com/network-interfaces', 'null')) != [dict(network=policy['network'], subnetwork=policy['subnet'])]:
        raise ValueError('resource must use the approved VPC egress policy')
    iam(cloud, kind, value)


def deploy(cloud, policy, args):
    resource = manifest(args, policy)
    if args.dry_run:
        return dict(action='deploy', id=reference(cloud, args.kind, name(args.name)),
                    images=[item['image'] for item in resource['spec']['template']['spec'].get('containers',
                            resource['spec']['template']['spec'].get('template', {}).get('spec', {}).get('containers', []))],
                    service_account=policy['service_account'], network=policy['network'], instances=1)
    existing = sdk(cloud, args.kind + 's', 'list', '--filter=metadata.name=' + name(args.name))
    for item in existing:
        owned(item, name(args.name))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'resource.json'
        path.write_text(json.dumps(resource))
        path.chmod(0o600)
        sdk(cloud, args.kind + 's', 'replace', str(path))
        authorization = iam(cloud, args.kind, name(args.name))
        role = 'roles/run.invoker' if args.kind == 'service' else 'roles/run.jobsExecutor'
        authorization['bindings'] = [binding for binding in authorization.get('bindings', []) if binding['role'] != role]
        authorization['bindings'].append(dict(role=role, members=policy['invokers']))
        path.write_text(json.dumps(authorization))
        sdk(cloud, args.kind + 's', 'set-iam-policy', name(args.name), str(path))
    created = discover(cloud, args.kind, name(args.name))[0]
    protected(cloud, policy, args.kind, created)
    return descriptor(cloud, args.kind, created)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return None


def invoke(cloud, args, target):
    if target['ready'] != 'True' or not target['url']:
        raise ValueError('service is not ready')
    if not args.path.startswith('/') or args.path.startswith('//') or any(c.isspace() or c in '#\\' for c in args.path):
        raise ValueError('provide a relative HTTP path')
    result = subprocess.run(['gcloud', '--quiet', '--project=' + cloud['project'], 'auth', 'print-identity-token'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    token = result.stdout.decode().strip()
    if result.returncode or not re.fullmatch(r'[A-Za-z0-9_.-]+', token):
        raise ValueError('Google identity token unavailable')
    body = args.data.read_bytes() if args.data else None
    if body and len(body) > 16 * 1024 * 1024:
        raise ValueError('request exceeds 16 MiB; stage large inputs separately')
    request = urllib.request.Request(target['url'] + args.path, data=body,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': args.content_type})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request) as response:
        content = response.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            raise ValueError('response exceeds 16 MiB')
        return dict(status=response.status, body=content.decode('utf-8', 'replace'))


def execute(cloud, policy, args):
    if args.action == 'deploy':
        return deploy(cloud, policy, args)
    if args.action == 'ls':
        return [descriptor(cloud, args.kind, item) for item in discover(cloud, args.kind)]
    value = name(args.name)
    resource = discover(cloud, args.kind, value)[0]
    if args.action == 'describe':
        protected(cloud, policy, args.kind, resource)
        return descriptor(cloud, args.kind, resource)
    if args.action == 'delete':
        sdk(cloud, args.kind + 's', 'delete', value)
        return dict(deleted=reference(cloud, args.kind, value))
    if args.action == 'logs':
        return sdk(cloud, args.kind + 's', 'logs', 'read', value, '--limit=100')
    protected(cloud, policy, args.kind, resource)
    if args.kind == 'service':
        return invoke(cloud, args, descriptor(cloud, args.kind, resource))
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
    result = sdk(cloud, 'jobs', 'execute', value, '--wait' if args.wait else '--async')
    return dict(job=reference(cloud, 'job', value), execution=result['metadata']['name'], status=result.get('status', {}))


def main(arguments=None):
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
    for action in ('describe', 'delete', 'logs', 'executions', 'cancel', 'run', 'invoke'):
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
    if (args.kind == 'service' and args.action in ('run', 'executions', 'cancel')) or (args.kind == 'job' and args.action == 'invoke'):
        parser.error('operation does not apply to this resource kind')
    try:
        print(json.dumps(execute(*configuration(), args), sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('tailworker: Cloud Run operation failed: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
