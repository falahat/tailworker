import argparse
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('tailworker', str(ROOT / 'tailworker'))
spec = importlib.util.spec_from_loader(loader.name, loader)
tw = importlib.util.module_from_spec(spec)
loader.exec_module(tw)


class Core(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_native_control_import_does_not_require_fcntl(self):
        with mock.patch.dict(sys.modules, fcntl=None):
            loader.exec_module(tw)

    def test_ssh_policy_pins_all_private_dns_and_preserves_owner_settings(self):
        config = self.root / 'ssh_config'
        tw.save(config, 'Host worker\n HostName worker.internal\n')
        effective = 'hostname worker.internal\nport 2222\nuser job\nhostkeyalias pinned\nidentityfile /keys/%h\nidentityfile /keys/static\nchanneltimeout session=1m\n'
        self.enterContext(mock.patch.object(tw.subprocess, 'check_output', return_value=effective))
        dns = self.enterContext(mock.patch.object(tw.socket, 'getaddrinfo', return_value=[(0, 0, 0, '', (ip, 2222)) for ip in ('::ffff:100.64.0.3', 'fd7a::1')]))
        argv = tw.ssh_argv('worker', config)
        for option in ('-oProxyCommand=none', '-oProxyJump=none', '-oForwardAgent=no', '-oClearAllForwardings=yes', '-oPermitLocalCommand=no',
                       '-oControlPath=none', '-oControlMaster=no', '-oControlPersist=no', '-oCanonicalizeHostname=no', '-oServerAliveInterval=0',
                       '-oStrictHostKeyChecking=yes', '-oBatchMode=yes', '-oIdentitiesOnly=yes', '-oPreferredAuthentications=publickey', '-oChannelTimeout=none'):
            self.assertIn(option, argv)
        self.assertEqual(argv[-11:], ['-p', '2222', '-l', 'job', '-oHostName=100.64.0.3', '-oHostKeyAlias=pinned',
                                     '-i', '/keys/worker.internal', '-i', '/keys/static', 'worker'])
        self.assertEqual(argv[:3], ['ssh', '-F', str(config)])
        dns.return_value.append((0, 0, 0, '', ('8.8.8.8', 2222)))
        with self.assertRaisesRegex(ValueError, 'only to private'):
            tw.ssh_argv('worker')
        config.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'owned private regular'):
            tw.ssh_argv('worker', config)
        config.chmod(0o600)
        config.with_suffix('.link').symlink_to(config)
        with self.assertRaisesRegex(ValueError, 'without a symlink'):
            tw.ssh_argv('worker', config.with_suffix('.link'))

    def test_resolve_returns_the_selected_approved_machine(self):
        self.enterContext(mock.patch.object(tw, 'output', return_value='{"rootless":true}'))
        machine = dict(name='worker', host='tw-worker', kind='dedicated', online=True,
                       capacity=dict(gpu_name='GPU'))
        with mock.patch.object(tw, 'inventory', return_value=[machine]), mock.patch.object(sys, 'argv', ['tailworker', 'resolve', 'worker']), contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(tw.main(), 0)
        self.assertEqual(json.loads(captured.getvalue()), machine)
        self.assertTrue(machine['runtime']['rootless'])

    def test_input_identity_ignores_times_and_roundtrips_three_files(self):
        source = self.root / 'source'
        (source / 'nested').mkdir(parents=True)
        values = {'first': b'\0\x01sample', 'second': b'other\n', 'nested/third': b'xyz'}
        for name, content in values.items():
            (source / name).write_bytes(content)
        archive = self.root / 'first.tar'
        identity = tw.archive_inputs(source, archive)
        os.utime(source / 'first', (1234, 1234))
        self.assertEqual(identity, tw.archive_inputs(source, self.root / 'second.tar'))
        result = self.root / 'result'
        tw.unpack_outputs(archive, result)
        self.assertEqual({name: (result / name).read_bytes() for name in values}, values)
        (source / 'first').write_bytes(b'changed')
        self.assertNotEqual(identity, tw.archive_inputs(source, self.root / 'changed.tar'))

    def test_owned_order_offline_and_gpu_selection(self):
        found = [dict(name='cpu', host='local', capacity=dict(gpu_name='')),
                 dict(name='offline', capacity=None),
                 dict(name='gpu', host='gpu', capacity=dict(gpu_name='device'))]
        with mock.patch.object(tw, 'inventory', return_value=found):
            self.assertEqual(tw.choose()['name'], 'cpu')
            self.assertEqual(tw.choose(gpu=True)['name'], 'gpu')
            with self.assertRaisesRegex(ValueError, 'no reachable'):
                tw.choose('offline')
            with self.assertRaisesRegex(ValueError, 'unknown machine'):
                tw.choose('missing')

    def test_offline_alias_disappears_but_pin_remains(self):
        config = self.root / 'config'
        config.mkdir()
        pins = 'tw-example ssh-ed25519 AAAA\n'
        (config / 'known_hosts').write_text(pins)
        worker = dict(kind='dedicated', online=True, host='tw-example', ip='100.64.0.8')
        with mock.patch.object(tw, 'CONFIG', config):
            tw.aliases([worker])
            self.assertIn('HostName 100.64.0.8', (config / 'ssh_config').read_text())
            worker['online'] = False
            tw.aliases([worker])
            self.assertEqual((config / 'ssh_config').read_text(), '')
            self.assertEqual((config / 'known_hosts').read_text(), pins)
            self.assertEqual((config / 'known_hosts').stat().st_mode & 0o777, 0o600)

    def test_gpu_descriptor_keeps_native_lock_until_process_exits(self):
        locks = self.root / 'locks'
        locks.mkdir()
        for name in ('gpu', 'gpu.want'):
            (locks / name).touch()
        binary = self.root / 'bin'
        binary.mkdir()
        executable = binary / 'podman'
        executable.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                              'print(json.dumps(dict(args=sys.argv[1:],fd=os.fstat(3).st_ino)),flush=True)\n'
                              'sys.stdin.read(1)\n')
        executable.chmod(0o700)
        receipt = self.root / 'receipt.json'
        with mock.patch.dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH']):
            process = subprocess.Popen([sys.executable, '-c', tw.RUN, str(locks), str(receipt), '1', 'own', '--rm', 'image', 'true'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                ready = json.loads(process.stdout.readline())
                self.assertEqual(ready['fd'], (locks / 'gpu').stat().st_ino)
                self.assertEqual(ready['args'][:2], ['run', '--preserve-fds=1'])
                waiting = self.root / 'waiting.json'
                waiter = subprocess.Popen([sys.executable, '-c', tw.RUN, str(locks), str(waiting), '1', 'waiting', 'true'], stdout=subprocess.PIPE)
                while not waiting.with_suffix('.pid').exists():
                    time.sleep(0.01)
                waiter.send_signal(signal.SIGINT)
                self.assertEqual((waiter.communicate()[0], waiter.returncode), (b'', 130))
                self.assertEqual(json.loads(waiting.read_text())['execution_seconds'], 0)
                with (locks / 'gpu').open() as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                process.communicate('x')
                self.assertEqual(process.returncode, 0)
                self.assertEqual(json.loads(receipt.read_text())['exit_code'], 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait()
        with (locks / 'gpu').open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_cache_hit_sends_no_input(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'file').write_text('content')
        with mock.patch.object(tw, 'CACHE', self.root / 'cache'), mock.patch.object(tw, 'output', return_value='/work/cache'), mock.patch.object(tw, 'call') as call, mock.patch.object(tw, 'pipe') as pipe, mock.patch.object(tw.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), mock.patch('sys.stdout', io.StringIO()):
            path = tw.cache_inputs('local', source)
        pipe.assert_not_called()
        self.assertRegex(path, r'/inputs/[0-9a-f]{64}$')
        call.assert_called_once_with('local', ['touch', path])

    def test_pruning_removes_old_inputs_but_keeps_active_mounts_and_fresh_inputs(self):
        cache = self.root / 'tailworker' / 'inputs'
        old, active, fresh = [cache / (letter * 64) for letter in 'abc']
        for directory in (old, active, fresh):
            directory.mkdir(parents=True)
            (directory / 'input').write_text('data')
        expired = time.time() - 8 * 86400
        for directory in (old, active):
            os.utime(directory, (expired, expired))
        binary = self.root / 'bin'
        binary.mkdir()
        podman = binary / 'podman'
        podman.write_text('#!' + sys.executable + '\nimport json,sys\n'
                          'print("container" if sys.argv[1]=="ps" else json.dumps('
                          + repr([dict(Mounts=[dict(Source=str(active))])]) + '))\n')
        podman.chmod(0o700)
        environment = dict(os.environ, XDG_CACHE_HOME=str(self.root),
                           PATH=str(binary) + os.pathsep + os.environ['PATH'])
        subprocess.run([sys.executable, '-c', tw.PRUNE], env=environment, check=True)
        self.assertFalse(old.exists())
        self.assertTrue((active / 'input').is_file())
        self.assertTrue((fresh / 'input').is_file())

    def test_offline_run_uses_memory_cap_and_never_pulls(self):
        args = argparse.Namespace(on=None, gpu=False, image='image', inputs=None, out=None, receipt=self.root / 'receipt.json',
                                  net=False, memory=None, command=['sh', '-c', 'exit 23'])
        machine = dict(name='owned', host='local', capacity=dict(memory_bytes=1000))
        process = mock.Mock()
        process.wait.return_value = 23
        image = 'sha256:' + 'a' * 64
        answers = ['true', image.removeprefix('sha256:'), '/work/receipts/result.json', json.dumps(dict(gpu_lock_wait_seconds=0, execution_seconds=2.5, exit_code=23))]
        with mock.patch.object(tw, 'choose', return_value=machine), mock.patch.object(tw, 'output', side_effect=answers), mock.patch.object(tw, 'call'), mock.patch.object(tw.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), mock.patch.object(tw.subprocess, 'Popen', return_value=process) as launch, mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(tw.run(args), 23)
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertEqual(command[command.index('--memory') + 1], '800')
        self.assertIn('--pull=never', command)
        for flag in ('--read-only', '--cap-drop=all', '--security-opt=no-new-privileges', image):
            self.assertIn(flag, command)
        receipt = json.loads(args.receipt.read_text())
        self.assertEqual((receipt['machine'], receipt['host'], receipt['image_id'], receipt['exit_code']), ('owned', 'local', image, 23))
        self.assertEqual(receipt['execution_seconds'], 2.5)
        self.assertEqual(receipt['gpu_lock_wait_seconds'], 0)
        self.assertTrue(receipt['container'].startswith('tw-'))
        with self.assertRaisesRegex(ValueError, 'receipt must be new'):
            tw.run(args)


if __name__ == '__main__':
    unittest.main()
