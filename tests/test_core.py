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

    def environment(self, gpu=False):
        source = self.root / 'data'
        source.mkdir(exist_ok=True)
        graph = self.root / 'graph'
        graph.mkdir(exist_ok=True)
        value = dict(machines=['worker'], image='sha256:'+'a'*64, cpus=1, memory_bytes=1024,
                     min_free_bytes=1, workdir='/work', gpu=gpu, env={'PUBLIC':'value'},
                     mounts=[dict(source=str(source),target='/work',read_only=False)])
        self.enterContext(mock.patch.dict(os.environ, XDG_RUNTIME_DIR=str(self.root/'runtime')))
        binary = self.root / 'bin'
        binary.mkdir(exist_ok=True)
        executable = binary / 'podman'
        executable.write_text('#!'+sys.executable+'\nimport json,os,signal,sys\nfrom pathlib import Path\n'
                              'if sys.argv[1]=="info": print('+repr(json.dumps(dict(host=dict(security=dict(rootless=True)),store=dict(graphRoot=str(graph)))))+')\n'
                              'elif sys.argv[1]=="image": print("sha256:"+"a"*64)\n'
                              'elif sys.argv[1]=="kill": os.kill(int((Path(os.environ["XDG_RUNTIME_DIR"])/"container.pid").read_text()),signal.SIGTERM)\n'
                              'elif sys.argv[1]=="run":\n (Path(os.environ["XDG_RUNTIME_DIR"])/"container.pid").write_text(str(os.getpid()))\n print(json.dumps(dict(args=sys.argv[1:],fd=os.fstat(3).st_ino if "--preserve-fds=1" in sys.argv else None)),flush=True)\n sys.stdin.read(1)\n sys.exit(int(os.environ.get("FAKE_EXIT","0")))\n')
        executable.chmod(0o700)
        self.enterContext(mock.patch.dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH']))
        return value

    def test_profile_policy_is_confined_and_reserved_settings_cannot_be_overridden(self):
        config = self.root / 'config'
        self.enterContext(mock.patch.object(tw,'CONFIG',config))
        path = config / 'profiles' / 'worker.toml'
        value = 'machines=["worker"]\nimage="sha256:'+'a'*64+'"\ncpus=1\nmemory_bytes=1024\nmin_free_bytes=1\nworkdir="/work"\nmounts=[]\n'
        tw.save(path,value)
        self.assertEqual(tw.profile(path)['machines'],['worker'])
        for installed, expected in (('/tools/pwsh','/tools/pwsh'),(None,'powershell')):
            with mock.patch.object(tw.os,'name','nt'), mock.patch.object(tw.shutil,'which',return_value=installed), mock.patch.object(tw.subprocess,'run') as acl:
                self.assertEqual(tw.profile(path)['machines'],['worker'])
                self.assertEqual(acl.call_args.args[0][0],expected)
        for change in ('[env]\nTAILWORKER_MANAGED="1"\n','unexpected=true\n'):
            path.write_text(value+change)
            with self.assertRaises(ValueError):
                tw.profile(path)
        path.write_text(value)
        path.chmod(0o666)
        with self.assertRaisesRegex(ValueError,'writable'):
            tw.profile(path)
        path.chmod(0o600)
        outside = self.root/'outside.toml'
        outside.write_text(value)
        with self.assertRaisesRegex(ValueError,'owner configuration'):
            tw.profile(outside)

    def test_environment_uses_persistent_mounts_and_fixed_isolation(self):
        value = self.environment()
        result = subprocess.run([sys.executable,'-c',tw.EXEC,json.dumps(value),'tw-own','run','sh','-c','exit 23'],input=b'x',stdout=subprocess.PIPE,check=True)
        args = json.loads(result.stdout)['args']
        for item in ('--interactive','--cpus','--memory','--memory-swap','--read-only','--cap-drop=all','--security-opt=no-new-privileges','--network=none','--pull=never','TAILWORKER_MANAGED=1',str(self.root/'data')+':/work:rw,rprivate,nosuid,nodev'):
            self.assertIn(item,args)
        self.assertEqual(args[-4:],[value['image'],'sh','-c','exit 23'])
        with mock.patch.dict(os.environ,FAKE_EXIT='23'):
            result = subprocess.run([sys.executable,'-c',tw.EXEC,json.dumps(value),'tw-own','run','true'],input=b'x',stdout=subprocess.PIPE)
        self.assertEqual(result.returncode,23)
        for source in ('/etc',str(self.root/'missing')):
            value['mounts'][0]['source']=source
            result = subprocess.run([sys.executable,'-c',tw.EXEC,json.dumps(value),'tw-own','check'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            self.assertNotEqual(result.returncode,0)

    def test_gpu_descriptor_and_cancellation_use_the_same_native_lock(self):
        value = self.environment(gpu=True)
        (self.root/'capacity.json').write_text('{"gpu_name":"admitted"}')
        locks = self.root/'locks'
        locks.mkdir()
        for name in ('gpu','gpu.want'):
            (locks/name).touch()
        script = tw.EXEC.replace('/var/lib/tailworker/locks',str(locks)).replace('/var/lib/tailworker/capacity.json',str(self.root/'capacity.json'))
        process = subprocess.Popen([sys.executable,'-c',script,json.dumps(value),'tw-own','run','true'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            ready = json.loads(process.stdout.readline())
            self.assertEqual(ready['fd'],(locks/'gpu').stat().st_ino)
            self.assertIn('TAILWORKER_GPU_LEASE_PATH=/run/tailworker/gpu.lock',ready['args'])
            with (locks/'gpu').open() as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            waiter = subprocess.Popen([sys.executable,'-c',script,json.dumps(value),'tw-waiting','run','true'],stdout=subprocess.PIPE)
            while not (self.root/'runtime/tailworker/tw-waiting.pid').exists():
                time.sleep(0.01)
            waiter.send_signal(signal.SIGINT)
            self.assertEqual((waiter.communicate()[0],waiter.returncode),(b'',130))
            self.assertFalse((self.root/'runtime/tailworker/tw-waiting.pid').exists())
            subprocess.run([sys.executable,'-c',tw.CANCEL,'tw-own'],check=True)
            self.assertEqual(process.wait(),130)
            process.communicate()
            self.assertFalse((self.root/'runtime/tailworker/tw-own.pid').exists())
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait()

    def test_only_approved_machines_are_probed_and_execution_is_never_retried(self):
        value = dict(machines=['offline','first','second'],gpu=False)
        found = [dict(name=name,host=name,online=capacity is not None) for name,capacity in
                 [('other',{}),('offline',None),('first',{}),('second',{})]]
        args = argparse.Namespace(profile=Path('policy'),command=['true'])
        with mock.patch.object(tw,'profile',return_value=value), mock.patch.object(tw,'settings',return_value={}), mock.patch.object(tw,'machines',return_value=found), mock.patch.object(tw,'aliases'), mock.patch.object(tw,'call',side_effect=[subprocess.CalledProcessError(1,[]),None]) as probe, mock.patch.object(tw,'command',side_effect=lambda host,args:[host,*args]), mock.patch.object(tw.subprocess,'Popen') as launch, contextlib.redirect_stderr(io.StringIO()):
            launch.return_value.wait.return_value=23
            self.assertEqual(tw.run(args),23)
        self.assertEqual([call.args[0] for call in probe.call_args_list],['first','second'])
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0][0],'second')


if __name__ == '__main__':
    unittest.main()
