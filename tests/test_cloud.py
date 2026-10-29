import argparse
import base64
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from test_core import tw

PUBLIC = 'ssh-ed25519 ' + base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + b'x' * 32).decode()


class Cloud(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(tw, 'CONFIG', self.root).start()
        self.calls, self.requests, self.created, self.existing = [], [], False, []
        (self.root / 'pub').write_text(PUBLIC)
        tw.save(self.root / 'tailscale-oauth.json', json.dumps(dict(client_id='fixture-id', client_secret='fixture-secret')))
        self.config = dict(owned=dict(control='local'), ssh=dict(public_keys=[str(self.root / 'pub')]),
            gcp=dict(project='fixture-project', zone='fixture-zone', machine_type='fixture-machine',
                     image_family='fixture-cpu', image_project='fixture-images', gpu_image_family='fixture-gpu', gpu_image_project='fixture-images'))
        self.args = argparse.Namespace(name='probe', hours=1, gpu=True, spot=True, dry_run=False,
            machine_type=None, disk_gb=200, tag=['tag:tw-app-runner'], allow=[])
        mock.patch.object(tw, 'ssh_argv', side_effect=lambda host: ['ssh', host]).start()
        mock.patch.object(tw.subprocess, 'run', side_effect=self.sdk_run).start()
        mock.patch.object(tw.subprocess, 'check_output', side_effect=self.output).start()
        mock.patch.object(tw.urllib.request, 'urlopen', side_effect=self.request).start()

    def request(self, request):
        self.requests.append(request)
        value = {
            'https://api.tailscale.com/api/v2/oauth/token': dict(access_token='fixture-token'),
            'https://api.tailscale.com/api/v2/tailnet/-/keys': dict(key='tskey-auth-fixture-join'),
        }[request.full_url]
        response = io.BytesIO(json.dumps(value).encode())
        return response

    def output(self, command, **options):
        self.calls.append((command, options))
        if command[0] == 'tailscale':
            peer = dict(ID='new', HostName='tw-probe', Online=True, Tags=['tag:tw', 'tag:tw-app-runner'], TailscaleIPs=['100.64.0.3'])
            return json.dumps(dict(Self=dict(ID='owner', HostName='control', TailscaleIPs=['100.64.0.1']), Peer={'new': peer} if self.created else {}))
        return {
            'git': 'a' * 40, 'gcloud': json.dumps(self.existing).encode(),
            'ssh-keyscan': '100.64.0.3 ' + PUBLIC + '\n',
        }[command[0]]

    def sdk_run(self, command, **options):
        self.calls.append((command, options))
        if command[0] == 'gcloud' and 'create' in command:
            self.created = True
            metadata = next(value for value in command if value.startswith('--metadata-from-file='))
            self.temporary_files = [Path(value.split('=', 1)[1]) for value in metadata.split('=', 1)[1].split(',')]
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.temporary_files))
            self.assertEqual(self.temporary_files[1].read_text(), 'tskey-auth-fixture-join')
        return subprocess.CompletedProcess(command, 0, stdout=('a' * 40 + '\n').encode())

    def test_up_has_lifetime_private_network_and_one_use_key(self):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            tw.up(self.args, self.config)
        creation = [command for command, _ in self.calls if command[0] == 'gcloud' and 'create' in command]
        self.assertEqual(len(creation), 1)
        for flag in ('--no-address', '--no-service-account', '--no-scopes', '--max-run-duration=1h', '--instance-termination-action=DELETE', '--labels=tailworker=1', '--network=tailworker', '--subnet=tailworker', '--provisioning-model=SPOT', '--maintenance-policy=TERMINATE'):
            self.assertIn(flag, creation[0])
        minted = json.loads(self.requests[1].data)
        self.assertEqual(minted['expirySeconds'], 900)
        self.assertEqual(minted['capabilities']['devices']['create'], dict(reusable=False, ephemeral=True, preauthorized=True, tags=['tag:tw', 'tag:tw-app-runner']))
        for secret in ('fixture-secret', 'fixture-token', 'tskey-auth-fixture-join'):
            self.assertNotIn(secret, repr(self.calls) + captured.getvalue())
        self.assertTrue(all(not path.exists() for path in self.temporary_files))

    def test_bounds_existing_instance_dry_run_and_name_collision(self):
        for hours in (0, 25):
            self.args.hours = hours
            with self.assertRaises(ValueError):
                tw.up(self.args, self.config)
        self.args.hours = 1
        self.existing = [dict(name='tw-other')]
        with self.assertRaisesRegex(ValueError, 'already exists'):
            tw.up(self.args, self.config)
        self.args.dry_run = True
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            tw.up(self.args, self.config)
        self.assertIn('gcloud', captured.getvalue())
        self.assertIn('--max-run-duration=1h', captured.getvalue())
        self.assertEqual(self.requests, [])
        tw.save(self.root / 'known_hosts', 'tw-probe ' + PUBLIC + '\n')
        with self.assertRaisesRegex(ValueError, 'host pin'):
            tw.up(self.args, self.config)

    def test_down_does_not_accept_an_offline_peer(self):
        self.existing = [dict(name='tw-probe', labels=dict(tailworker='1'), zone='zones/fixture-zone')]
        with mock.patch.object(tw, 'nodes', return_value=[dict(HostName='tw-probe', Online=False)]):
            with self.assertRaisesRegex(ValueError, 'cleanup is incomplete'):
                tw.down(self.args, self.config)
        self.assertEqual([command[0] for command, _ in self.calls if 'delete' in command or command[0] == 'ssh'], ['gcloud'])

    def test_stale_or_wrongly_tagged_peer_never_establishes_a_pin(self):
        old = dict(ID='owner', HostName='control', TailscaleIPs=['100.64.0.1'])
        peer = dict(ID='owner', HostName='tw-probe', Tags=['tag:tw'], Online=True, TailscaleIPs=['100.64.0.3'])
        with mock.patch.object(tw, 'nodes', side_effect=[[old], [peer]]):
            with self.assertRaisesRegex(ValueError, 'stale'):
                tw.up(self.args, self.config)
        self.assertFalse((self.root / 'known_hosts').exists())
        self.assertFalse(any(command[0] == 'ssh-keyscan' for command, _ in self.calls))

    def test_ambiguous_creation_is_not_repeated_or_deleted(self):
        original = self.sdk_run
        def fail(command, **options):
            if command[0] == 'gcloud' and 'create' in command:
                original(command, **options)
                raise subprocess.CalledProcessError(1, command)
            return original(command, **options)
        with mock.patch.object(tw.subprocess, 'run', side_effect=fail), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                tw.up(self.args, self.config)
        self.assertEqual(sum('create' in command for command, _ in self.calls), 1)
        self.assertFalse(any('delete' in command for command, _ in self.calls))
