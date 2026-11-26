"""Cooperative reservation CAS and retained-invocation release contract."""

import copy
import importlib.util
import multiprocessing
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import syncwheel_test_slot as slots
import syncwheel_private_json as private_json
from syncwheel_errors import SyncwheelError


def facade():
    spec = importlib.util.spec_from_file_location('slot_fixture_facade', SCRIPTS / 'syncwheel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExistingJsonStore:
    def __init__(self, path, module, barrier=None):
        self.path, self.module, self.barrier = path, module, barrier

    def load(self):
        snapshot = private_json.load_document(self.path, {'active': None, 'receipts': {}})
        if self.barrier:
            self.barrier.wait(timeout=10)
        return snapshot

    def save(self, snapshot):
        self.module.save_private_configuration(self.path, snapshot)


def request():
    workspace = {'clean': True, 'head': 'a' * 40, 'tree': 'b' * 40,
                 'admin': '/synthetic/admin', 'device': 1, 'inode': 2,
                 'lockfiles': {'package-lock.json': None}}
    return {'workspace': workspace, 'plan_digest': 'plan', 'composition_digest': 'recipe',
            'base': 'c' * 40, 'sources': [{'stack': 'a', 'revision': 'd' * 40}],
            'candidate': workspace['head'], 'tree': workspace['tree'],
            'argv': ['/usr/bin/python3', '-c', 'print("literal $HOME")'],
            'tools': {'python': 'fixture'}, 'environment': {'HOME': '/synthetic/home'}}


def contender(path, barrier, output):
    value = request()
    store = ExistingJsonStore(Path(path), facade(), barrier)
    slot = slots.CooperativeTestSlot(store, lambda: copy.deepcopy(value['workspace']))
    try:
        operation = slot.reserve(value)
        output.put(('reserved', operation['nonce']))
    except SyncwheelError as error:
        output.put(('refused', str(error)))


class CooperativeTestSlotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='syncwheel-slot-state-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'slot.json'
        self.module = facade()
        self.value = request()
        self.workspace = copy.deepcopy(self.value['workspace'])
        self.store = ExistingJsonStore(self.path, self.module)
        self.slot = slots.CooperativeTestSlot(self.store, lambda: copy.deepcopy(self.workspace))

    def running(self):
        operation = self.slot.reserve(self.value)
        nonce = operation['nonce']
        unit = 'syncwheel-test-' + nonce + '.service'
        configuration = {'Type': 'exec', 'ExitType': 'cgroup', 'KillMode': 'control-group',
                         'RemainAfterExit': 'yes', 'argv': self.value['argv']}
        self.slot.starting(nonce, unit, configuration)
        binding = {'unit': unit, 'invocation': 'native-invocation', 'configuration': configuration,
                   'cgroup': '/synthetic/cgroup/' + unit}
        self.slot.bind(nonce, binding)
        terminal = dict(binding, job='', main_pid=0, result='success', active='active',
                        substate='exited', main_code=1, main_status=0)
        return nonce, binding, terminal

    def finish(self, nonce, terminal, exists=True, populated=False):
        return self.slot.finish(nonce, terminal, cgroup_exists=exists,
                                cgroup_populated=populated, log_digest='log',
                                measurements={'wall_seconds': 1})

    def test_simultaneous_process_reservations_use_existing_exact_byte_cas(self):
        context = multiprocessing.get_context('fork')
        barrier = context.Barrier(2)
        output = context.Queue()
        processes = [context.Process(target=contender, args=(str(self.path), barrier, output))
                     for _ in range(2)]
        for process in processes:
            process.start()
            self.addCleanup(lambda p=process: p.kill() if p.is_alive() else None)
        results = [output.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted(result[0] for result in results), ['refused', 'reserved'])
        self.assertIn('changed concurrently', next(result[1] for result in results if result[0] == 'refused'))
        self.assertEqual(self.store.load()['active']['nonce'], next(result[1] for result in results if result[0] == 'reserved'))

    def test_cancel_is_only_before_dispatch_and_stale_dispatch_refuses(self):
        operation = self.slot.reserve(self.value)
        self.slot.cancel_reserved(operation['nonce'])
        with self.assertRaisesRegex(SyncwheelError, 'reservation changed'):
            self.slot.starting(operation['nonce'], 'old.service', {'config': True})
        nonce, _, terminal = self.running()
        with self.assertRaisesRegex(SyncwheelError, 'only an undispatched'):
            self.slot.cancel_reserved(nonce)
        with self.assertRaisesRegex(SyncwheelError, 'already dispatched'):
            self.slot.starting(nonce, 'duplicate.service', {'config': True})
        self.finish(nonce, terminal)

    def test_missing_or_changed_witness_never_releases(self):
        nonce, binding, terminal = self.running()
        before = self.path.read_bytes()
        for field in ('unit', 'invocation', 'configuration', 'job', 'main_pid',
                      'main_code', 'main_status', 'active', 'substate', 'result', 'cgroup'):
            with self.subTest(field=field):
                changed = copy.deepcopy(terminal)
                changed.pop(field)
                with self.assertRaisesRegex(SyncwheelError, 'running or unknown'):
                    self.finish(nonce, changed)
                self.assertEqual(self.path.read_bytes(), before)
        for field in ('unit', 'invocation', 'configuration', 'cgroup'):
            changed = copy.deepcopy(terminal)
            changed[field] = 'different'
            with self.assertRaises(SyncwheelError):
                self.finish(nonce, changed)
            self.assertEqual(self.path.read_bytes(), before)
        self.finish(nonce, terminal)

    def test_zero_main_pid_with_a_live_child_retains_slot(self):
        nonce, _, terminal = self.running()
        terminal['substate'] = 'running'
        with self.assertRaisesRegex(SyncwheelError, 'running or unknown'):
            self.finish(nonce, terminal, populated=True)
        with self.assertRaisesRegex(SyncwheelError, 'occupied'):
            self.slot.reserve(self.value)

    def test_retired_path_requires_prior_binding_and_terminal_witness(self):
        nonce, binding, terminal = self.running()
        terminal['cgroup'] = ''
        for exists in (True, None):
            with self.assertRaises(SyncwheelError):
                self.finish(nonce, terminal, exists=exists)
        broken = dict(binding, cgroup='')
        self.assertIsNone(slots.terminal_observation(broken, terminal,
                          cgroup_exists=False, cgroup_populated=None))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(SyncwheelError, 'running or unknown'):
            self.finish(nonce, terminal, exists=False, populated=True)
        self.assertEqual(self.path.read_bytes(), before)
        receipt = self.finish(nonce, terminal, exists=False, populated=None)
        self.assertTrue(receipt['qualified'])
        self.assertTrue(receipt['result']['retired_cgroup'])

    def test_failed_job_releases_physical_slot_without_manufacturing_success(self):
        nonce, _, terminal = self.running()
        terminal.update(active='failed', substate='failed', result='exit-code', main_status=7)
        receipt = self.finish(nonce, terminal)
        self.assertFalse(receipt['qualified'])
        self.assertFalse(receipt['result']['passed'])
        self.assertIsNone(self.store.load()['active'])

    def test_recovery_is_idempotent_and_cannot_release_a_newer_reservation(self):
        nonce, _, terminal = self.running()
        receipt = self.finish(nonce, terminal)
        second = self.slot.reserve(self.value)
        before = self.path.read_bytes()
        self.assertEqual(self.finish(nonce, terminal), receipt)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.load()['active']['nonce'], second['nonce'])

    def test_candidate_change_prevents_dispatch_or_successful_qualification(self):
        operation = self.slot.reserve(self.value)
        self.workspace['tree'] = 'changed'
        with self.assertRaisesRegex(SyncwheelError, 'changed before dispatch'):
            self.slot.starting(operation['nonce'], 'test-' + operation['nonce'] + '.service', {'config': True})
        self.slot.cancel_reserved(operation['nonce'])
        self.workspace = copy.deepcopy(self.value['workspace'])
        nonce, _, terminal = self.running()
        self.workspace['tree'] = 'changed'
        receipt = self.finish(nonce, terminal)
        self.assertFalse(receipt['candidate_unchanged'])
        self.assertFalse(receipt['qualified'])
        self.assertIsNone(self.store.load()['active'])

class NativeAdapterBoundaryTest(unittest.TestCase):
    def test_aliases_resolve_the_same_physical_and_admin_identity(self):
        import os
        import subprocess
        from unittest import mock
        from syncwheel_test_slot_native import workspace_observation
        with tempfile.TemporaryDirectory(prefix='syncwheel-slot-alias-') as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            environment = {'HOME': str(root), 'PATH': os.defpath}
            with mock.patch.dict(os.environ, environment, clear=True):
                def git(*args):
                    subprocess.run(['git', *args], cwd=workspace, check=True, capture_output=True)
                git('init', '-q')
                git('config', 'user.name', 'Slot Fixture')
                git('config', 'user.email', 'slot@example.invalid')
                (workspace / 'candidate').write_text('fixture\n')
                git('add', '.')
                git('commit', '-qm', 'fixture')
                alias = root / 'alias'
                alias.symlink_to(workspace, target_is_directory=True)
                self.assertEqual(workspace_observation(workspace), workspace_observation(alias))
                for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CONFIG_COUNT'):
                    with mock.patch.dict(os.environ, {key: 'different'}):
                        with self.assertRaisesRegex(SyncwheelError, 'Git selectors'):
                            workspace_observation(workspace)

    def test_native_runtime_dbus_property_is_a_scalar_not_an_array(self):
        from unittest import mock
        from syncwheel_test_slot_native import NativeCooperativeJob
        job = NativeCooperativeJob.__new__(NativeCooperativeJob)
        job.manager = mock.Mock(return_value='Type=exec\nExitType=cgroup\n')
        job.dbus_property = mock.Mock(side_effect=[[
            ['/usr/bin/python3', ['/usr/bin/python3', 'worker'], False, 0, 0, 0, 0, 1, 0, 0]
        ], 15000000])
        config = job.configuration('fixture.service')
        self.assertEqual(config['runtime_us'], 15000000)
        self.assertEqual(config['exec'], ['/usr/bin/python3', ['/usr/bin/python3', 'worker'], False])


class NativeRecoveryUncertaintyTest(unittest.TestCase):
    setUp = CooperativeTestSlotTest.setUp
    running = CooperativeTestSlotTest.running
    def test_native_mismatch_reports_unknown_without_releasing_or_redispatching(self):
        from unittest import mock
        from syncwheel_test_slot_native import NativeCooperativeJob
        nonce, binding, terminal = self.running()
        before = self.path.read_bytes()
        job = NativeCooperativeJob.__new__(NativeCooperativeJob)
        job.store = self.store
        job.slot = self.slot
        terminal['invocation'] = 'different'
        job.observe = mock.Mock(return_value=terminal)
        with self.assertRaisesRegex(SyncwheelError, 'identity/configuration is unknown'):
            job.recover(nonce)
        self.assertEqual(self.path.read_bytes(), before)

    def test_incomplete_terminal_does_not_spin_or_release(self):
        from unittest import mock
        from syncwheel_test_slot_native import NativeCooperativeJob
        nonce, binding, terminal = self.running()
        before = self.path.read_bytes()
        job = NativeCooperativeJob.__new__(NativeCooperativeJob)
        job.store = self.store
        job.slot = self.slot
        terminal['job'] = 'pending'
        job.observe = mock.Mock(return_value=terminal)
        job.population = mock.Mock(return_value=(False, None))
        with self.assertRaisesRegex(SyncwheelError, 'terminal evidence is incomplete'):
            job.recover(nonce, wait=True)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
