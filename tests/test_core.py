import argparse
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('tailworker', str(ROOT / 'tailworker'))
spec = importlib.util.spec_from_loader(loader.name, loader)
tw = importlib.util.module_from_spec(spec)
loader.exec_module(tw)


class Core(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

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
        with mock.patch.dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH']):
            process = subprocess.Popen([sys.executable, '-c', tw.GPU_RUN, str(locks), '--rm', 'image', 'true'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                ready = json.loads(process.stdout.readline())
                self.assertEqual(ready['fd'], (locks / 'gpu').stat().st_ino)
                self.assertEqual(ready['args'][:2], ['run', '--preserve-fds=1'])
                with (locks / 'gpu').open() as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                process.communicate('x')
                self.assertEqual(process.returncode, 0)
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

    def test_offline_run_uses_memory_cap_and_never_pulls(self):
        args = argparse.Namespace(on=None, gpu=False, image='image', inputs=None, out=None,
                                  net=False, memory=None, command=['sh', '-c', 'exit 23'])
        machine = dict(name='owned', host='local', capacity=dict(memory_bytes=1000))
        process = mock.Mock()
        process.wait.return_value = 23
        with mock.patch.object(tw, 'choose', return_value=machine), mock.patch.object(tw, 'output', return_value='true'), mock.patch.object(tw, 'call'), mock.patch.object(tw.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), mock.patch.object(tw.subprocess, 'Popen', return_value=process) as launch, mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(tw.run(args), 23)
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertEqual(command[command.index('--memory') + 1], '800')
        self.assertIn('--pull=never', command)


if __name__ == '__main__':
    unittest.main()
