import copy
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / 'scripts'
CLI = SCRIPTS / 'syncwheel.py'
PROTOCOL_FIXTURES = REPO_ROOT / 'tests' / 'fixtures' / 'revision-provider-v1'
PROTOCOL_TRANSCRIPT_SHA256 = {
    'check-request.json': '95230aeef627edd963ce96dae41fb6ad75454e03e071393053a5b1f7161de2a7',
    'check-response.json': 'fb275fc6922aab5d7d74f5060d219384e31c8297685f53dc9d0babfd7ce859c2',
    'request-finalize.json': '8cc0fb602ec4e5a566641bca2e67a7c6f6377b8374510db498dfe85f1044c333',
    'response-finalize.json': 'df259bd6ba6871aee5a8a96ca15618c82b827995aa948e3ba6dad51801b1b2e7',
    'response-finalize-error.json': '4e4b281a14ee73e25fb6b1e19add1b9fe891af98dffaec73a2f1150f2966e268',
    'negative-request-vectors.json': '78636804deedba316515393f8356ef08fa87963beed3336bad2986baaff4c09d',
}

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import syncwheel_revision_provider as protocol


def load_syncwheel_module():
    spec = importlib.util.spec_from_file_location('syncwheel_revision_provider_test', CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SYNCWHEEL = load_syncwheel_module()


class RevisionProviderRepository:
    def __init__(self, *, coordination_mode='disabled', base_ref='origin/main'):
        self.temp = tempfile.TemporaryDirectory(prefix='syncwheel-revision-provider-')
        self.root = Path(self.temp.name)
        self.remote = self.root / 'origin.git'
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.repo)], check=True)
        self.git('config', 'user.name', 'Revision Provider Test')
        self.git('config', 'user.email', 'revision-provider@example.invalid')
        self.git('remote', 'add', 'origin', str(self.remote))
        (self.repo / 'base.txt').write_text('base\n')
        (self.repo / '.gitignore').write_text(
            '.syncwheel/profile.local.json\n'
            '.syncwheel/ledger/\n'
            'var/syncwheel/\n'
        )
        manifest = {
            'version': 2,
            'repository_mode': 'delivery',
            'syncwheel_tracking': 'git-tracked',
            'syncwheel_worktree_root': 'var/syncwheel',
            'defaults': {
                'canonical_remote': 'origin',
                'publication_remote': 'origin',
                'base_branch': 'main',
                'base_ref': base_ref,
                'integration_membership': 'required',
            },
            'integration': {
                'branch': 'main-integration',
                'base': base_ref,
                'strategy': 'cherry-pick',
                'stacks': [],
            },
            'stacks': [],
            'coordination': {
                'mode': coordination_mode,
                'id': 'revision-provider-test',
                'remote': 'origin',
                'state_branch': 'syncwheel/state/revision-provider-test',
                'gc': {
                    'worktree_grace_days': 7,
                    'backup_retention_days': 30,
                    'backup_keep': 2,
                },
            },
            'channels': [],
        }
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        # This fixture models a pre-existing isolated clone. Persist its
        # authoritative common-Git guard state directly so ledger-durability
        # tests start with an empty event stream; command-level disable/audit
        # behavior is covered by the managed-ref-guard suite.
        SYNCWHEEL.ensure_syncwheel_metadata_excluded(
            self.repo, 'git-tracked', manifest['syncwheel_worktree_root']
        )
        SYNCWHEEL.save_primary_guard(
            self.repo,
            manifest,
            enabled=False,
            reason='isolated revision-provider fixture',
        )
        self.git('add', '.gitignore', 'base.txt', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: initialize managed repository')
        self.git('push', '-q', '-u', 'origin', 'main')
        self.git(
            'symbolic-ref',
            'refs/remotes/origin/HEAD',
            'refs/remotes/origin/main',
        )
        self.git('switch', '-q', '-c', 'main-integration', 'main')
        if coordination_mode == 'active-active':
            self.cli('int', 'push')

    def close(self):
        self.temp.cleanup()

    def git(self, *args, check=True, cwd=None):
        result = subprocess.run(
            ['git', *args],
            cwd=cwd or self.repo,
            text=True,
            capture_output=True,
        )
        if check and result.returncode:
            raise AssertionError(
                f"git {args} failed\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return result.stdout.strip()

    def cli(self, *args, expected=0, payload=None):
        return self.cli_at(self.repo, *args, expected=expected, payload=payload)

    def cli_at(self, cwd, *args, expected=0, payload=None):
        env = os.environ.copy()
        env['SYNCWHEEL_UPDATE_MODE'] = 'off'
        result = subprocess.run(
            ['python3', str(CLI), *args],
            cwd=cwd,
            input=(json.dumps(payload) if payload is not None else None),
            text=True,
            capture_output=True,
            env=env,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"expected exit {expected}, got {result.returncode}\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def sha256(self, content):
        return hashlib.sha256(content.encode()).hexdigest()

    def request(
        self,
        action,
        *,
        operation_id='op-001',
        path='feature.txt',
        before=None,
        after_content='feature\n',
        reason='Keep Fleet revision ownership deterministic.',
        no_commit=False,
    ):
        return {
            'protocolVersion': 1,
            'action': action,
            'operationId': operation_id,
            'repositoryRoot': str(self.repo.resolve()),
            'expectedHead': self.git('rev-parse', 'HEAD'),
            'commandName': 'agentwheel install',
            'reason': reason,
            'noCommit': no_commit,
            'paths': [
                {
                    'path': path,
                    'beforeSha256': before,
                    'afterSha256': (
                        None if after_content is None else self.sha256(after_content)
                    ),
                }
            ],
        }

    def protocol_request(self, payload, expected=0):
        result = self.cli('revision-provider', expected=expected, payload=payload)
        lines = result.stdout.splitlines()
        if len(lines) != 1:
            raise AssertionError(f'expected one stdout response, got {lines!r}')
        return json.loads(lines[0]), result

    def check_request(self, payload):
        return {**payload, 'action': 'check', 'paths': []}

    def remote_heads(self):
        output = self.git('ls-remote', '--heads', str(self.remote))
        return sorted(output.splitlines())

    def provider_journal_root(self):
        root = Path(self.git('rev-parse', '--git-common-dir'))
        if not root.is_absolute():
            root = self.repo / root
        return root / 'syncwheel' / 'revision-provider'

    def raw_index_bytes(self):
        path = Path(self.git('rev-parse', '--git-path', 'index'))
        if not path.is_absolute():
            path = self.repo / path
        return path.read_bytes()

    def all_refs(self):
        return self.git(
            'for-each-ref', '--format=%(refname) %(objectname)', 'refs/'
        )

    def all_ref_bindings(self):
        return self.git(
            'for-each-ref',
            '--format=%(refname) %(objectname) %(symref)',
            'refs/',
        )

    def remote_ref_bindings(self):
        return self.git(
            'for-each-ref',
            '--format=%(refname) %(objectname) %(symref)',
            'refs/remotes/',
        )

    def install_same_oid_remote_alias(self):
        object_oid = self.git('rev-parse', 'refs/remotes/origin/main')
        first = 'refs/remotes/revision-provider/first'
        second = 'refs/remotes/revision-provider/second'
        alias = 'refs/remotes/revision-provider/alias'
        self.git('update-ref', first, object_oid)
        self.git('update-ref', second, object_oid)
        self.git('symbolic-ref', alias, first)
        return alias, first, second, object_oid

    def set_base_ref(self, value):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['defaults']['base_ref'] = value
        manifest['integration']['base'] = value
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('add', '--', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: configure revision-provider base')

    def install_existing_stack(
        self, *, path='base.txt', content='existing\n', manifest_on_base=False
    ):
        stack_base = self.git('rev-parse', 'origin/main')
        self.git('switch', '-q', '-c', 'syncwheel/stack/existing', stack_base)
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git('add', '--', path)
        self.git('commit', '-q', '-m', 'test: existing stack product')
        product = self.git('rev-parse', 'HEAD')
        if manifest_on_base:
            self.git('switch', '-q', 'main')
        else:
            self.git('switch', '-q', 'main-integration')
            self.git('merge', '-q', '--ff-only', 'syncwheel/stack/existing')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['stacks'].append(
            {
                'id': 'existing',
                'branch': 'syncwheel/stack/existing',
                'base': stack_base if manifest_on_base else 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'main-integration',
                'commits': [product],
                'state': 'draft',
                'publication': {'enabled': False},
            }
        )
        manifest['integration']['stacks'].append('existing')
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: register existing stack')
        if manifest_on_base:
            self.git('push', '-q', 'origin', 'main')
            self.git('switch', '-q', 'main-integration')
            self.git('reset', '--hard', 'origin/main')
            self.git('cherry-pick', product)
        self.cli('validate')
        return product

    @property
    def manifest_path(self):
        return self.repo / '.syncwheel' / 'manifest.json'

    def read_manifest(self):
        return json.loads(self.manifest_path.read_text())

    def write_manifest(self, manifest):
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

    def staged_derived_digest(self, *paths):
        path_blobs = {}
        for path in paths:
            result = subprocess.run(
                ['git', 'rev-parse', '--verify', f':{path}'],
                cwd=self.repo,
                text=True,
                capture_output=True,
            )
            path_blobs[path] = (
                result.stdout.strip() if result.returncode == 0 else None
            )
        return SYNCWHEEL.derived_projection_paths_digest(path_blobs)

    def append_derived_provenance(self, operation_id, commit, paths):
        manifest = self.read_manifest()
        paths = sorted(paths)
        paths_digest = SYNCWHEEL.derived_projection_commit_paths_digest(
            self.repo, commit, paths
        )
        payload = {
            'operation_id': operation_id,
            'commit': commit,
            'paths': paths,
            'paths_digest': paths_digest,
            'composition_digest': SYNCWHEEL.integration_composition_digest(
                manifest
            ),
        }
        SYNCWHEEL.record_common_derived_provenance(
            self.repo, manifest, payload
        )
        SYNCWHEEL.append_ledger_event(
            self.repo,
            'revision_provider_derived_commit',
            payload,
            self.manifest_path,
        )
        return paths_digest

    def commit_derived_projection(self, operation_id, paths, *, subject):
        paths = sorted(paths)
        paths_digest = self.staged_derived_digest(*paths)
        self.git(
            'commit',
            '-q',
            '-m',
            subject,
            '-m',
            f'Syncwheel-Derived-Projection: {operation_id}\n'
            f'Syncwheel-Derived-Paths: {paths_digest}',
        )
        commit = self.git('rev-parse', 'HEAD')
        self.append_derived_provenance(operation_id, commit, paths)
        return commit

    def enable_derived_paths(self, *prefixes, on_base=False):
        """Enable manifest-v3 derived paths on integration or its canonical base."""
        if on_base:
            self.git('switch', '-q', 'main')
        manifest = self.read_manifest()
        manifest['version'] = 3
        manifest['integration']['derived_paths'] = list(prefixes or ('locks/',))
        manifest.setdefault('channels', [])
        self.write_manifest(manifest)
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: enable derived projections')
        commit = self.git('rev-parse', 'HEAD')
        if on_base:
            self.git('push', '-q', 'origin', 'main')
            self.git('switch', '-q', 'main-integration')
            self.git('reset', '--hard', 'origin/main')
        return commit


def run_cli(cwd, *args):
    env = os.environ.copy()
    env['SYNCWHEEL_UPDATE_MODE'] = 'off'
    return subprocess.run(
        ['python3', str(CLI), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=env,
    )


class CoordinationPeer:
    """A second clone of the fixture's coordination remote, sharing its helpers."""

    git = RevisionProviderRepository.git
    cli = RevisionProviderRepository.cli
    cli_at = RevisionProviderRepository.cli_at
    manifest_path = RevisionProviderRepository.manifest_path
    read_manifest = RevisionProviderRepository.read_manifest
    write_manifest = RevisionProviderRepository.write_manifest
    staged_derived_digest = RevisionProviderRepository.staged_derived_digest
    append_derived_provenance = RevisionProviderRepository.append_derived_provenance
    commit_derived_projection = RevisionProviderRepository.commit_derived_projection

    def __init__(self, fixture, name='peer-b'):
        self.root = fixture.root
        self.remote = fixture.remote
        self.repo = fixture.root / name
        subprocess.run(
            ['git', '-C', str(self.remote), 'symbolic-ref', 'HEAD', 'refs/heads/main'],
            check=True,
        )
        subprocess.run(
            ['git', 'clone', '-q', str(self.remote), str(self.repo)], check=True
        )
        self.git('config', 'user.name', f'Revision Provider {name}')
        self.git('config', 'user.email', f'{name}@example.invalid')
        self.git(
            'switch', '-q', '-c', 'main-integration', '--track', 'origin/main-integration'
        )
        self.git(
            'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main'
        )
        self.cli(
            'hooks', 'remove', '--disable',
            '--reason', 'isolated coordination peer', '--apply',
        )


class RevisionProviderProtocolTest(unittest.TestCase):
    def test_cli_skips_startup_update_and_hook_bootstrap_for_provider(self):
        with (
            mock.patch.object(sys, 'argv', [str(CLI), 'revision-provider']),
            mock.patch.object(
                SYNCWHEEL, 'maybe_handle_startup_update_policy'
            ) as startup_update,
            mock.patch.object(
                SYNCWHEEL, 'converge_default_repository_hooks'
            ) as hook_bootstrap,
            mock.patch.object(
                SYNCWHEEL, 'execute_parsed_command', return_value=17
            ) as execute,
        ):
            self.assertEqual(SYNCWHEEL.main(), 17)
        startup_update.assert_not_called()
        hook_bootstrap.assert_not_called()
        execute.assert_called_once()

    def valid_payload(self):
        return {
            'protocolVersion': 1,
            'action': 'check',
            'operationId': 'op-001',
            'repositoryRoot': '/tmp/example',
            'expectedHead': 'a' * 40,
            'commandName': 'agentwheel install',
            'reason': 'A complete reason.',
            'noCommit': False,
            'paths': [
                {
                    'path': 'config/example.json',
                    'beforeSha256': None,
                    'afterSha256': 'b' * 64,
                }
            ],
        }

    def test_request_validation_is_strict_and_action_independent_for_digest(self):
        payload = self.valid_payload()
        request = protocol.parse_request(payload)
        finalize = protocol.parse_request({**payload, 'action': 'finalize'})
        self.assertEqual(request.plan_digest, finalize.plan_digest)

        with self.assertRaisesRegex(protocol.RevisionProviderError, 'unknown request field'):
            protocol.parse_request({**payload, 'extra': True})
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'unsupported protocolVersion'):
            protocol.parse_request({**payload, 'protocolVersion': 2})
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'not normalized'):
            bad = copy.deepcopy(payload)
            bad['paths'][0]['path'] = 'config/../secret'
            protocol.parse_request(bad)
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'control state'):
            bad = copy.deepcopy(payload)
            bad['paths'][0]['path'] = '.syncwheel/manifest.json'
            protocol.parse_request(bad)
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'control state'):
            bad = copy.deepcopy(payload)
            bad['paths'][0]['path'] = '.syncwheel/ledger'
            protocol.parse_request(bad)
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'must differ'):
            bad = copy.deepcopy(payload)
            bad['paths'][0]['beforeSha256'] = bad['paths'][0]['afterSha256']
            protocol.parse_request(bad)
        for operation_id in ('op-', 'op_'):
            accepted = protocol.parse_request({**payload, 'operationId': operation_id})
            self.assertEqual(accepted.operation_id, operation_id)
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'control character'):
            protocol.parse_request({**payload, 'reason': 'invalid\x08reason'})
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'at most 4096'):
            protocol.parse_request({**payload, 'reason': 'r' * 4097})

    def test_agentwheel_protocol_v1_golden_transcript(self):
        self.assertEqual(
            {
                name: hashlib.sha256((PROTOCOL_FIXTURES / name).read_bytes()).hexdigest()
                for name in PROTOCOL_TRANSCRIPT_SHA256
            },
            PROTOCOL_TRANSCRIPT_SHA256,
        )
        check_payload = json.loads((PROTOCOL_FIXTURES / 'check-request.json').read_text())
        check_request = protocol.parse_request(check_payload)
        self.assertEqual(
            protocol._base_response(
                check_request.action, check_request.operation_id, True, 'ready'
            ),
            json.loads((PROTOCOL_FIXTURES / 'check-response.json').read_text()),
        )

        finalize_payload = json.loads(
            (PROTOCOL_FIXTURES / 'request-finalize.json').read_text()
        )
        finalize_request = protocol.parse_request(finalize_payload)
        journal = {
            'phase': 'verified',
            'terminalStatus': 'verified',
            'expectedHead': finalize_request.expected_head,
            'resultingHead': '3' * 40,
            'productCommitSha': '2' * 40,
            'draftStackId': finalize_request.draft_stack_id,
            'draftBranch': finalize_request.draft_branch,
            'candidateDraftCommitSha': '4' * 40,
            'controlCommitSha': '3' * 40,
            'manifestDigest': 'd' * 64,
            'unmappedIntegrationCommits': [],
        }
        self.assertEqual(
            protocol._mutation_response(finalize_request, journal),
            json.loads((PROTOCOL_FIXTURES / 'response-finalize.json').read_text()),
        )
        error_message = (
            f'operation {finalize_request.operation_id} has no prepared journal; '
            'run preflight first'
        )
        self.assertEqual(
            protocol._error_response(finalize_payload, error_message),
            json.loads(
                (PROTOCOL_FIXTURES / 'response-finalize-error.json').read_text()
            ),
        )

        partial = {
            'expectedHead': finalize_request.expected_head,
            'resultingHead': '2' * 40,
            'productCommitSha': '2' * 40,
            'draftStackId': finalize_request.draft_stack_id,
            'draftBranch': finalize_request.draft_branch,
            'candidateDraftCommitSha': '4' * 40,
            'controlCommitSha': None,
        }
        partial_error = protocol._error_response(
            finalize_payload, 'partial ownership', partial
        )
        self.assertEqual(
            {
                field: partial_error[field]
                for field in (
                    'draftStackId', 'draftBranch', 'draftTipSha', 'controlCommitSha'
                )
            },
            {
                'draftStackId': None,
                'draftBranch': None,
                'draftTipSha': None,
                'controlCommitSha': None,
            },
        )

    def test_agentwheel_negative_request_vectors_fail_closed(self):
        vectors = json.loads(
            (PROTOCOL_FIXTURES / 'negative-request-vectors.json').read_text()
        )
        self.assertEqual(len(vectors), 5)
        for vector in vectors:
            with self.subTest(vector=vector['name']):
                payload = copy.deepcopy(self.valid_payload())
                payload.update(copy.deepcopy(vector['overrides']))
                with self.assertRaises(protocol.RevisionProviderError):
                    protocol.parse_request(payload)
                stdin = io.StringIO(json.dumps(payload))
                stdout = io.StringIO()
                stderr = io.StringIO()
                self.assertEqual(
                    protocol.run_provider_stream(object(), stdin, stdout, stderr), 2
                )
                self.assertEqual(len(stdout.getvalue().splitlines()), 1)
                response = json.loads(stdout.getvalue())
                self.assertFalse(response['ok'])
                self.assertEqual(response['status'], 'rejected')
                self.assertEqual(response['action'], payload['action'])
                self.assertEqual(response['operationId'], payload['operationId'])
                self.assertGreater(len(response['error']), 0)
                self.assertLessEqual(protocol._utf16_length(response['error']), 4096)
                self.assertEqual(stderr.getvalue(), response['error'] + '\n')
                self.assertLessEqual(protocol._utf16_length(stderr.getvalue()), 4096)

    def test_error_wire_sanitizes_hashes_lengths_and_invalid_correlation(self):
        payload = {
            **self.valid_payload(),
            'action': 'finalize',
            'expectedHead': '1234abcd',
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        self.assertEqual(
            protocol.run_provider_stream(
                object(), io.StringIO(json.dumps(payload)), stdout, stderr
            ),
            2,
        )
        response = json.loads(stdout.getvalue())
        for field in (
            'expectedHead', 'resultingHead', 'productCommitSha', 'draftTipSha',
            'controlCommitSha', 'manifestDigest',
        ):
            self.assertIsNone(response[field], field)

        max_path = 'p' * 4096
        duplicate = copy.deepcopy(self.valid_payload())
        duplicate['paths'] = [
            {
                'path': max_path,
                'beforeSha256': None,
                'afterSha256': '1' * 64,
            },
            {
                'path': max_path,
                'beforeSha256': None,
                'afterSha256': '2' * 64,
            },
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        self.assertEqual(
            protocol.run_provider_stream(
                object(), io.StringIO(json.dumps(duplicate)), stdout, stderr
            ),
            2,
        )
        duplicate_response = json.loads(stdout.getvalue())
        self.assertIn('duplicate path:', duplicate_response['error'])
        self.assertLessEqual(
            protocol._utf16_length(duplicate_response['error']), 4096
        )
        self.assertLessEqual(protocol._utf16_length(stderr.getvalue()), 4096)

        oversized = {
            **self.valid_payload(),
            'action': '\U0001f6ab' * 81,
            'operationId': '\U0001f6ab' * 129,
            '\U0001f6ab' * 3000: True,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        self.assertEqual(
            protocol.run_provider_stream(
                object(), io.StringIO(json.dumps(oversized)), stdout, stderr
            ),
            2,
        )
        oversized_response = json.loads(stdout.getvalue())
        self.assertEqual(oversized_response['action'], 'unknown')
        self.assertEqual(oversized_response['operationId'], 'unknown')
        self.assertLessEqual(
            protocol._utf16_length(oversized_response['error']), 4096
        )
        self.assertLessEqual(protocol._utf16_length(stderr.getvalue()), 4096)

    def test_error_wire_sanitizes_malformed_journal_recovery_fields(self):
        payload = {**self.valid_payload(), 'action': 'finalize'}

        class MalformedJournalBackend:
            @contextlib.contextmanager
            def operation_lock(self, request):
                yield

            def load_journal(self, request):
                return {
                    'schemaVersion': 1,
                    'providerId': protocol.PROVIDER_ID,
                    'operationId': request.operation_id,
                    'planDigest': request.plan_digest,
                    'phase': 'corrupt-phase',
                    'expectedHead': 'abcd',
                    'resultingHead': 'f' * 39,
                    'productCommitSha': 'G' * 40,
                    'draftStackId': request.draft_stack_id,
                    'draftBranch': request.draft_branch,
                    'candidateDraftCommitSha': '1' * 39,
                    'controlCommitSha': '2' * 41,
                    'manifestDigest': '3' * 63,
                    'unmappedIntegrationCommits': [
                        '4' * 40, 'bad', '4' * 40,
                    ],
                }

        stdout = io.StringIO()
        stderr = io.StringIO()
        self.assertEqual(
            protocol.run_provider_stream(
                MalformedJournalBackend(),
                io.StringIO(json.dumps(payload)),
                stdout,
                stderr,
            ),
            2,
        )
        response = json.loads(stdout.getvalue())
        self.assertEqual(response['expectedHead'], payload['expectedHead'])
        for field in (
            'resultingHead', 'productCommitSha', 'draftTipSha',
            'controlCommitSha', 'manifestDigest',
        ):
            self.assertIsNone(response[field], field)
        self.assertIsNone(response['draftStackId'])
        self.assertIsNone(response['draftBranch'])
        self.assertEqual(response['unmappedIntegrationCommits'], ['4' * 40])

    def test_provider_backend_has_no_worktree_materialization_path(self):
        module_source = CLI.read_text()
        class_start = module_source.index('class SyncwheelRevisionBackend:')
        class_end = module_source.index('\ndef command_revision_provider', class_start)
        source = module_source[
            class_start:class_end
        ]
        self.assertNotIn('materialize_new_stack_branch', source)
        self.assertNotIn('deterministic_stack_replay_worktree', source)

    def test_invalid_wire_request_returns_one_fail_closed_json_response(self):
        fixture = RevisionProviderRepository()
        try:
            payload = fixture.request('check')
            payload['unknown'] = 'blocked'
            response, result = fixture.protocol_request(payload, expected=2)
            self.assertFalse(response['ok'])
            self.assertEqual(response['status'], 'rejected')
            self.assertIn('unknown request field', response['error'])
            self.assertIn('unknown request field', result.stderr)
        finally:
            fixture.close()


class RevisionProviderIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = RevisionProviderRepository()

    def tearDown(self):
        self.fixture.close()

    def test_base_ref_cannot_alias_any_managed_branch_before_journaling(self):
        fixture = RevisionProviderRepository(base_ref='main-integration')
        try:
            request = fixture.request(
                'preflight', operation_id='managed-base-collision'
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            refs_before = fixture.git(
                'for-each-ref', '--format=%(refname) %(objectname)', 'refs/'
            )
            response, _ = fixture.protocol_request(request, expected=2)
            self.assertIn('resolves to managed ref', response['error'])
            self.assertIn('refs/heads/main-integration', response['error'])
            self.assertEqual(
                fixture.git(
                    'for-each-ref', '--format=%(refname) %(objectname)', 'refs/'
                ),
                refs_before,
            )
            self.assertFalse(
                (fixture.provider_journal_root() / 'managed-base-collision.json').exists()
            )
            self.assertEqual(
                fixture.git('rev-parse', 'HEAD'), request['expectedHead']
            )
        finally:
            fixture.close()

    def test_base_ref_rejects_revision_syntax_and_abbreviated_shas_without_effects(self):
        scenarios = (
            ('main-integration^0', 'base-caret-expression', 'revision expressions'),
            ('main-integration~1', 'base-tilde-expression', 'revision expressions'),
            ('abbreviated', 'base-abbreviated-sha', 'abbreviated commit SHA'),
            ('main@{upstream}', 'base-upstream-selector', 'revision expressions'),
            ('main@{u}', 'base-upstream-short-selector', 'revision expressions'),
            ('main@{push}', 'base-push-selector', 'revision expressions'),
            ('@{u}', 'base-head-upstream-selector', 'revision expressions'),
            ('@{push}', 'base-head-push-selector', 'revision expressions'),
        )
        for configured, operation_id, expected_error in scenarios:
            with self.subTest(configured=configured):
                fixture = RevisionProviderRepository()
                try:
                    fixture.git(
                        'branch', '--set-upstream-to=origin/main',
                        'main-integration',
                    )
                    fixture.git('config', 'push.default', 'upstream')
                    value = configured
                    if configured == 'abbreviated':
                        value = fixture.git('rev-parse', 'origin/main')[:12]
                    fixture.set_base_ref(value)
                    request = fixture.request(
                        'preflight', operation_id=operation_id
                    )
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    refs_before = fixture.all_refs()
                    index_before = fixture.raw_index_bytes()
                    response, _ = fixture.protocol_request(request, expected=2)
                    self.assertIn(expected_error, response['error'])
                    self.assertEqual(fixture.all_refs(), refs_before)
                    self.assertEqual(fixture.raw_index_bytes(), index_before)
                    self.assertEqual(
                        fixture.git('rev-parse', 'HEAD'), request['expectedHead']
                    )
                    self.assertFalse(
                        (
                            fixture.provider_journal_root()
                            / f'{operation_id}.json'
                        ).exists()
                    )
                    self.assertEqual(
                        fixture.git(
                            'rev-parse', '--verify',
                            f'refs/heads/syncwheel/draft/agentwheel-{operation_id}',
                            check=False,
                        ),
                        '',
                    )
                finally:
                    fixture.close()

    def test_direct_base_ref_forms_are_accepted(self):
        fixture = RevisionProviderRepository()
        try:
            fixture.git('tag', 'revision-provider-base', 'origin/main')
            direct_forms = (
                'main',
                'refs/heads/main',
                'origin/main',
                'refs/remotes/origin/main',
                'heads/main',
                'remotes/origin/main',
                'revision-provider-base',
                'refs/tags/revision-provider-base',
            )
            for index, value in enumerate(direct_forms):
                with self.subTest(value=value):
                    fixture.set_base_ref(value)
                    request = fixture.request(
                        'check', operation_id=f'direct-base-{index}'
                    )
                    response, _ = fixture.protocol_request(
                        fixture.check_request(request)
                    )
                    self.assertEqual(response['status'], 'ready')
                    self.assertFalse(
                        (
                            fixture.provider_journal_root()
                            / f'direct-base-{index}.json'
                        ).exists()
                    )
        finally:
            fixture.close()

    def test_symbolic_base_refs_are_rejected_without_effects(self):
        scenarios = (
            (
                'refs/heads/base-alias',
                'refs/heads/base-alias',
                'refs/heads/main-integration',
                'symbolic-local-base',
            ),
            (
                'origin/HEAD',
                'refs/remotes/origin/HEAD',
                'refs/remotes/origin/main',
                'symbolic-remote-head-base',
            ),
        )
        for base_ref, symbolic_ref, target_ref, operation_id in scenarios:
            with self.subTest(base_ref=base_ref):
                fixture = RevisionProviderRepository()
                try:
                    fixture.set_base_ref(base_ref)
                    fixture.git('symbolic-ref', symbolic_ref, target_ref)
                    request = fixture.request(
                        'preflight', operation_id=operation_id
                    )
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    refs_before = fixture.all_ref_bindings()
                    index_before = fixture.raw_index_bytes()
                    response, _ = fixture.protocol_request(request, expected=2)
                    self.assertIn('symbolic ref', response['error'])
                    self.assertEqual(fixture.all_ref_bindings(), refs_before)
                    self.assertEqual(fixture.raw_index_bytes(), index_before)
                    self.assertEqual(
                        fixture.git('rev-parse', 'HEAD'), request['expectedHead']
                    )
                    self.assertEqual(
                        fixture.git(
                            'symbolic-ref', '--no-recurse', symbolic_ref
                        ),
                        target_ref,
                    )
                    self.assertFalse(
                        (
                            fixture.provider_journal_root()
                            / f'{operation_id}.json'
                        ).exists()
                    )
                    self.assertEqual(
                        fixture.git(
                            'rev-parse', '--verify',
                            f'refs/heads/syncwheel/draft/agentwheel-{operation_id}',
                            check=False,
                        ),
                        '',
                    )
                finally:
                    fixture.close()

    def test_remote_head_symbolic_lease_completes_finalize_and_recover(self):
        fixture = RevisionProviderRepository()
        try:
            other = 'refs/remotes/origin/other'
            object_oid = fixture.git('rev-parse', 'refs/remotes/origin/main')
            fixture.git('update-ref', other, object_oid)
            remote_before = fixture.remote_ref_bindings()
            worktrees_before = [
                line
                for line in fixture.git(
                    'worktree', 'list', '--porcelain'
                ).splitlines()
                if line.startswith('worktree ')
            ]
            payload = fixture.request(
                'preflight', operation_id='remote-head-symbolic-lease'
            )
            request = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(
                backend, protocol.parse_request(fixture.check_request(payload))
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            protocol.handle_request(backend, request)

            class RefLockProbeBackend(SYNCWHEEL.SyncwheelRevisionBackend):
                retarget = None

                def checkpoint(self, phase):
                    if phase == 'ref_transaction_prepared' and self.retarget is None:
                        self.retarget = subprocess.run(
                            [
                                'git', 'symbolic-ref',
                                'refs/remotes/origin/HEAD', other,
                            ],
                            cwd=fixture.repo,
                            text=True,
                            capture_output=True,
                        )

            lock_backend = RefLockProbeBackend(protocol)
            finalized = protocol.handle_request(
                lock_backend, replace(request, action='finalize')
            )
            recovered = protocol.handle_request(
                backend, replace(request, action='recover')
            )
            self.assertEqual(finalized['status'], 'verified')
            self.assertEqual(recovered['status'], 'verified')
            self.assertIsNotNone(lock_backend.retarget)
            self.assertNotEqual(lock_backend.retarget.returncode, 0)
            self.assertIn('HEAD.lock', lock_backend.retarget.stderr)
            self.assertEqual(
                fixture.git(
                    'symbolic-ref', '--no-recurse',
                    'refs/remotes/origin/HEAD',
                ),
                'refs/remotes/origin/main',
            )
            self.assertEqual(fixture.remote_ref_bindings(), remote_before)
            self.assertEqual(
                [
                    line
                    for line in fixture.git(
                        'worktree', 'list', '--porcelain'
                    ).splitlines()
                    if line.startswith('worktree ')
                ],
                worktrees_before,
            )
            self.assertEqual(fixture.git('status', '--porcelain'), '')
            self.assertEqual(
                fixture.git('write-tree'), fixture.git('rev-parse', 'HEAD^{tree}')
            )
            journal = json.loads(
                (
                    fixture.provider_journal_root()
                    / 'remote-head-symbolic-lease.json'
                ).read_text()
            )
            self.assertEqual(
                journal['baselineRemoteRefs']['refs/remotes/origin/HEAD'],
                {
                    'name': 'refs/remotes/origin/HEAD',
                    'kind': 'symbolic',
                    'objectOid': object_oid,
                    'symbolicTarget': 'refs/remotes/origin/main',
                },
            )
            self.assertEqual(journal['integrationBranch'], 'main-integration')
            self.assertEqual(
                journal['refTransactionRefs']['refs/remotes/origin/HEAD'],
                journal['baselineRemoteRefs']['refs/remotes/origin/HEAD'],
            )
            self.assertEqual(
                journal['refTransactionRefs']['refs/remotes/origin/main'],
                journal['baselineRemoteRefs']['refs/remotes/origin/main'],
            )
        finally:
            fixture.close()

    def test_same_oid_symbolic_ref_retarget_and_type_drift_fail_closed(self):
        for mutation in ('retarget', 'type'):
            with self.subTest(mutation=mutation):
                fixture = RevisionProviderRepository()
                try:
                    alias, _, second, object_oid = (
                        fixture.install_same_oid_remote_alias()
                    )
                    payload = fixture.request(
                        'preflight', operation_id=f'symbolic-{mutation}-drift'
                    )
                    fixture.protocol_request(fixture.check_request(payload))
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    fixture.protocol_request(payload)
                    if mutation == 'retarget':
                        fixture.git('symbolic-ref', alias, second)
                    else:
                        fixture.git('update-ref', '--no-deref', alias, object_oid)
                    rejected, _ = fixture.protocol_request(
                        {**payload, 'action': 'finalize'}, expected=2
                    )
                    self.assertIn('managed local ref lease was lost', rejected['error'])
                    self.assertEqual(
                        fixture.git('rev-parse', 'HEAD'), payload['expectedHead']
                    )
                    self.assertEqual(
                        fixture.git(
                            'rev-parse', '--verify',
                            f'refs/heads/syncwheel/draft/agentwheel-symbolic-{mutation}-drift',
                            check=False,
                        ),
                        '',
                    )
                finally:
                    fixture.close()

    def test_ref_kind_inspection_exit_128_fails_closed(self):
        fixture = RevisionProviderRepository()
        try:
            refs_before = fixture.all_ref_bindings()
            index_before = fixture.raw_index_bytes()

            class InspectionErrorBackend(SYNCWHEEL.SyncwheelRevisionBackend):
                def _symbolic_ref_result(self, repo_root, name):
                    return subprocess.CompletedProcess(
                        args=['git', 'symbolic-ref'],
                        returncode=128,
                        stdout='',
                        stderr='fatal: injected ref inspection failure',
                    )

            with self.assertRaisesRegex(
                protocol.RevisionProviderError, 'symbolic-ref exit 128'
            ):
                InspectionErrorBackend(protocol)._observe_ref(
                    fixture.repo, 'refs/remotes/origin/main'
                )
            self.assertEqual(fixture.all_ref_bindings(), refs_before)
            self.assertEqual(fixture.raw_index_bytes(), index_before)
        finally:
            fixture.close()

    def test_exact_full_sha_base_is_accepted_and_persisted_immutably(self):
        fixture = RevisionProviderRepository()
        try:
            base_sha = fixture.git('rev-parse', 'origin/main')
            fixture.set_base_ref(base_sha)
            request = fixture.request(
                'preflight', operation_id='exact-base-sha'
            )
            fixture.protocol_request(fixture.check_request(request))
            (fixture.repo / 'feature.txt').write_text('feature\n')
            fixture.protocol_request(request)
            response, _ = fixture.protocol_request(
                {**request, 'action': 'finalize'}
            )
            self.assertEqual(response['status'], 'verified')
            journal = json.loads(
                (
                    fixture.provider_journal_root() / 'exact-base-sha.json'
                ).read_text()
            )
            self.assertEqual(journal['baseRef'], base_sha)
            self.assertEqual(journal['baseRefSha'], base_sha)
            self.assertIsNone(journal['baseRefFullName'])
            self.assertIsNone(journal['baseRefObjectSha'])
            manifest = json.loads(
                (fixture.repo / '.syncwheel' / 'manifest.json').read_text()
            )
            owned = next(
                stack for stack in manifest['stacks']
                if stack['id'] == 'agentwheel-exact-base-sha'
            )
            self.assertEqual(owned['base'], base_sha)
        finally:
            fixture.close()

    def test_finalize_owns_product_commit_without_publication_or_worktree_leak(self):
        request = self.fixture.request('preflight')
        remote_before = self.fixture.remote_heads()
        worktrees_before = [
            line for line in self.fixture.git('worktree', 'list', '--porcelain').splitlines()
            if line.startswith('worktree ')
        ]

        checked, _ = self.fixture.protocol_request(self.fixture.check_request(request))
        self.assertEqual(checked['status'], 'ready')
        self.assertFalse(self.fixture.provider_journal_root().exists())
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        response, _ = self.fixture.protocol_request(request)
        self.assertEqual(response['status'], 'prepared')

        finalized, _ = self.fixture.protocol_request({**request, 'action': 'finalize'})
        self.assertTrue(finalized['ok'])
        self.assertEqual(finalized['status'], 'verified')
        self.assertFalse(finalized['published'])
        self.assertIsNotNone(finalized['productCommitSha'])
        self.assertIsNotNone(finalized['controlCommitSha'])
        self.assertEqual(finalized['expectedHead'], request['expectedHead'])
        self.assertEqual(finalized['resultingHead'], finalized['controlCommitSha'])
        self.assertEqual(
            finalized['resultingHead'], self.fixture.git('rev-parse', 'HEAD')
        )
        self.assertEqual(finalized['unmappedIntegrationCommits'], [])
        self.assertEqual(finalized['draftStackId'], 'agentwheel-op-001')
        self.assertEqual(
            finalized['draftBranch'], 'syncwheel/draft/agentwheel-op-001'
        )
        self.assertEqual(
            finalized['draftTipSha'],
            self.fixture.git('rev-parse', finalized['draftBranch']),
        )
        self.assertNotEqual(finalized['draftTipSha'], finalized['controlCommitSha'])
        manifest = json.loads(
            (self.fixture.repo / '.syncwheel' / 'manifest.json').read_text()
        )
        owned_stack = next(
            stack for stack in manifest['stacks']
            if stack['id'] == finalized['draftStackId']
        )
        self.assertEqual(
            owned_stack['base'], self.fixture.git('rev-parse', 'origin/main')
        )
        self.assertRegex(owned_stack['base'], r'^[0-9a-f]{40}$')

        product_message = self.fixture.git(
            'show', '-s', '--format=%B', finalized['productCommitSha']
        )
        control_message = self.fixture.git(
            'show', '-s', '--format=%B', finalized['controlCommitSha']
        )
        draft_message = self.fixture.git(
            'show', '-s', '--format=%B', finalized['draftBranch']
        )
        for message in (product_message, draft_message, control_message):
            self.assertIn('Keep Fleet revision ownership deterministic.', message)
            self.assertIn('Agentwheel-Operation: op-001', message)
        changed_product = self.fixture.git(
            'diff-tree', '--no-commit-id', '--name-only', '-r',
            finalized['productCommitSha'],
        ).splitlines()
        changed_control = self.fixture.git(
            'diff-tree', '--no-commit-id', '--name-only', '-r',
            finalized['controlCommitSha'],
        ).splitlines()
        self.assertEqual(changed_product, ['feature.txt'])
        self.assertEqual(changed_control, ['.syncwheel/manifest.json'])
        self.assertEqual(self.fixture.git('status', '--porcelain'), '')
        self.assertEqual(
            [
                line
                for line in self.fixture.git('worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ')
            ],
            worktrees_before,
        )
        self.assertEqual(self.fixture.remote_heads(), remote_before)
        self.assertEqual(
            self.fixture.git(
                'ls-remote', '--heads', str(self.fixture.remote),
                'syncwheel/draft/agentwheel-op-001',
            ),
            '',
        )
        validation = self.fixture.cli('validate')
        self.assertNotIn('unmapped', validation.stdout.lower())
        finalized_manifest = json.loads(
            (self.fixture.repo / '.syncwheel/manifest.json').read_text()
        )
        self.assertEqual(
            finalized['manifestDigest'], SYNCWHEEL.manifest_digest(finalized_manifest)
        )
        journal = json.loads(
            (self.fixture.provider_journal_root() / 'op-001.json').read_text()
        )
        self.assertEqual(journal['publicationState'], 'owned-but-unpublished')

        repeated, _ = self.fixture.protocol_request({**request, 'action': 'recover'})
        self.assertEqual(repeated, {**finalized, 'action': 'recover'})

    def test_route_is_manifest_base_when_projection_reproduces_product_blobs(self):
        request = self.fixture.request(
            'preflight', operation_id='manifest-base-route'
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        journal = json.loads(
            (self.fixture.provider_journal_root() / 'manifest-base-route.json').read_text()
        )
        manifest = self.fixture.read_manifest()
        stack = next(
            item for item in manifest['stacks']
            if item['id'] == 'agentwheel-manifest-base-route'
        )
        self.assertEqual(finalized['status'], 'verified')
        self.assertEqual(journal['projectionRoute'], 'manifest-base')
        self.assertEqual(stack['base'], self.fixture.git('rev-parse', 'origin/main'))

    def test_manifest_base_route_edits_a_base_file_end_to_end(self):
        request = self.fixture.request(
            'preflight',
            operation_id='manifest-base-edit',
            path='base.txt',
            before=self.fixture.sha256('base\n'),
            after_content='edited\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'base.txt').write_text('edited\n')
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        journal = json.loads(
            (self.fixture.provider_journal_root() / 'manifest-base-edit.json').read_text()
        )
        self.assertEqual(finalized['status'], 'verified')
        self.assertEqual(journal['projectionRoute'], 'manifest-base')
        self.assertEqual(
            self.fixture.git('show', f"{finalized['draftTipSha']}:base.txt"),
            'edited',
        )

    def test_route_compares_product_blobs_without_requiring_equal_modes(self):
        stack_base = self.fixture.git('rev-parse', 'origin/main')
        self.fixture.git(
            'switch', '-q', '-c', 'syncwheel/stack/mode-only', stack_base
        )
        (self.fixture.repo / 'base.txt').chmod(0o755)
        self.fixture.git('add', 'base.txt')
        self.fixture.git('commit', '-q', '-m', 'test: stack changes mode only')
        stack_commit = self.fixture.git('rev-parse', 'HEAD')
        self.fixture.git('switch', '-q', 'main-integration')
        self.fixture.git('merge', '-q', '--ff-only', 'syncwheel/stack/mode-only')
        manifest = self.fixture.read_manifest()
        manifest['stacks'].append(
            {
                'id': 'mode-only',
                'branch': 'syncwheel/stack/mode-only',
                'base': 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'main-integration',
                'commits': [stack_commit],
                'state': 'draft',
                'publication': {'enabled': False},
            }
        )
        manifest['integration']['stacks'].append('mode-only')
        self.fixture.write_manifest(manifest)
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: register mode-only stack')
        request = self.fixture.request(
            'preflight',
            operation_id='blob-only-route-test',
            path='base.txt',
            before=self.fixture.sha256('base\n'),
            after_content='provider\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'base.txt').write_text('provider\n')
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        journal = json.loads(
            (self.fixture.provider_journal_root() / 'blob-only-route-test.json').read_text()
        )
        self.assertEqual(finalized['status'], 'verified')
        self.assertEqual(journal['projectionRoute'], 'manifest-base')
        projected_mode = self.fixture.git(
            'ls-tree', finalized['draftTipSha'], '--', 'base.txt'
        ).split()[0]
        candidate_mode = self.fixture.git(
            'ls-tree', journal['candidateProductCommitSha'], '--', 'base.txt'
        ).split()[0]
        self.assertEqual(projected_mode, '100644')
        self.assertEqual(candidate_mode, '100755')

    def test_manifest_base_route_deletes_a_base_file_end_to_end(self):
        request = self.fixture.request(
            'preflight',
            operation_id='manifest-base-delete',
            path='base.txt',
            before=self.fixture.sha256('base\n'),
            after_content=None,
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'base.txt').unlink()
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        journal = json.loads(
            (self.fixture.provider_journal_root() / 'manifest-base-delete.json').read_text()
        )
        self.assertEqual(finalized['status'], 'verified')
        self.assertEqual(journal['projectionRoute'], 'manifest-base')
        self.assertEqual(
            self.fixture.git('ls-tree', finalized['draftTipSha'], '--', 'base.txt'),
            '',
        )

    def test_route_ignores_a_manifest_only_control_commit_ahead_of_base(self):
        self.fixture.git('switch', '-q', 'main')
        (self.fixture.repo / 'absorbed.txt').write_text('absorbed\n')
        self.fixture.git('add', 'absorbed.txt')
        self.fixture.git('commit', '-q', '-m', 'test: product already delivered')
        absorbed = self.fixture.git('rev-parse', 'HEAD')
        self.fixture.git('branch', 'syncwheel/stack/absorbed', absorbed)
        self.fixture.git('push', '-q', 'origin', 'main')
        self.fixture.git('switch', '-q', 'main-integration')
        self.fixture.git('reset', '--hard', 'origin/main')
        manifest = self.fixture.read_manifest()
        manifest['stacks'].append(
            {
                'id': 'absorbed',
                'branch': 'syncwheel/stack/absorbed',
                'base': 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'main-integration',
                'commits': [absorbed],
                'state': 'draft',
                'publication': {'enabled': False},
            }
        )
        manifest['integration']['stacks'].append('absorbed')
        self.fixture.write_manifest(manifest)
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: record absorbed stack')
        self.fixture.cli('validate')
        request = self.fixture.request(
            'preflight', operation_id='control-only-route'
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        journal = json.loads(
            (self.fixture.provider_journal_root() / 'control-only-route.json').read_text()
        )
        self.assertEqual(finalized['status'], 'verified')
        self.assertEqual(journal['projectionRoute'], 'manifest-base')
        self.assertEqual(
            self.fixture.git('rev-list', '--count', 'origin/main..' + request['expectedHead']),
            '1',
        )

    def test_route_is_derived_when_projection_changes_a_product_blob(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        manifest_path = self.fixture.repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['version'] = 3
        manifest['integration']['derived_paths'] = ['locks/']
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: enable derived lock projections')
        request = self.fixture.request(
            'preflight',
            operation_id='integration-first-lock',
            path='locks/codex.lock',
            before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        self.assertEqual(finalized['status'], 'verified')
        self.assertIsNone(finalized['draftStackId'])
        self.assertIsNone(finalized['draftBranch'])
        self.assertIsNone(finalized['draftTipSha'])
        self.assertIn(
            'Syncwheel-Derived-Projection: integration-first-lock',
            self.fixture.git('show', '-s', '--format=%B', finalized['productCommitSha']),
        )
        journal = json.loads(
            (self.fixture.provider_journal_root() / 'integration-first-lock.json').read_text()
        )
        self.assertEqual(journal['projectionRoute'], 'derived')

    def test_derived_route_deletes_a_stack_only_file_end_to_end(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='stack-only\n'
        )
        self.fixture.enable_derived_paths('locks/')
        request = self.fixture.request(
            'preflight',
            operation_id='derived-delete',
            path='locks/codex.lock',
            before=self.fixture.sha256('stack-only\n'),
            after_content=None,
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks' / 'codex.lock').unlink()
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        journal = json.loads(
            (self.fixture.provider_journal_root() / 'derived-delete.json').read_text()
        )
        self.assertEqual(finalized['status'], 'verified')
        self.assertEqual(journal['projectionRoute'], 'derived')
        self.assertEqual(journal['candidateDraftCommitSha'], None)
        self.assertEqual(
            self.fixture.git(
                'ls-tree', finalized['productCommitSha'], '--', 'locks/codex.lock'
            ),
            '',
        )
        message = self.fixture.git(
            'show', '-s', '--format=%B', finalized['productCommitSha']
        )
        self.assertIn('Syncwheel-Derived-Projection: derived-delete', message)
        self.assertIn(
            f"Syncwheel-Derived-Paths: {journal['derivedContentDigest']}",
            message,
        )

    def test_derived_route_preserves_a_path_containing_lf_end_to_end(self):
        path = 'locks/line\nbreak.lock'
        self.fixture.install_existing_stack(path=path, content='first\n')
        self.fixture.enable_derived_paths('locks/')
        request = self.fixture.request(
            'preflight',
            operation_id='derived-lf-path',
            path=path,
            before=self.fixture.sha256('first\n'),
            after_content='second\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / path).write_text('second\n')
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        self.assertEqual(finalized['status'], 'verified')
        self.assertEqual(
            SYNCWHEEL.commit_changed_files(
                self.fixture.repo, finalized['productCommitSha']
            ),
            [path],
        )
        manifest, _ = SYNCWHEEL.load_manifest(self.fixture.repo)
        validation = SYNCWHEEL.validate_manifest(self.fixture.repo, manifest)
        self.assertEqual(
            validation['details']['integration']['derived_commits'],
            [finalized['productCommitSha']],
        )
        self.assertEqual(
            validation['details']['integration']['unmapped_commits'],
            [],
        )

    def test_derived_route_creates_no_draft_ref_and_no_manifest_delta(self):
        """A lock delta already represented by integration is a derived commit, never a stack."""
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        manifest_path = self.fixture.repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['version'] = 3
        manifest['integration']['derived_paths'] = ['locks/']
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: enable derived lock projections')

        request = self.fixture.request(
            'preflight', operation_id='derived-lock', path='locks/codex.lock',
            before=self.fixture.sha256('first-owner\n'), after_content='second-owner\n',
        )
        manifest_before = manifest_path.read_bytes()
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        self.fixture.protocol_request(request)
        finalized, _ = self.fixture.protocol_request({**request, 'action': 'finalize'})

        self.assertEqual(finalized['status'], 'verified')
        self.assertIsNotNone(finalized['productCommitSha'])
        self.assertIsNone(finalized['draftStackId'])
        self.assertIsNone(finalized['draftBranch'])
        self.assertIsNone(finalized['draftTipSha'])
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(
            self.fixture.git(
                'rev-parse', '--verify', 'refs/heads/syncwheel/draft/agentwheel-derived-lock',
                check=False,
            ),
            '',
        )
        message = self.fixture.git('show', '-s', '--format=%B', finalized['productCommitSha'])
        self.assertIn('Syncwheel-Derived-Projection: derived-lock', message)

    def test_derived_route_refuses_paths_outside_derived_paths(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        self.fixture.enable_derived_paths('locks/')
        request = self.fixture.request(
            'preflight', operation_id='mixed-derived-paths',
            path='locks/codex.lock', before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        request['paths'].append(
            {
                'path': 'source.py',
                'beforeSha256': None,
                'afterSha256': self.fixture.sha256('print("source")\n'),
            }
        )
        expected_head = request['expectedHead']
        manifest_before = self.fixture.manifest_path.read_bytes()
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        (self.fixture.repo / 'source.py').write_text('print("source")\n')
        self.fixture.protocol_request(request)

        rejected, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}, expected=2
        )

        self.assertIn('derived route refuses paths outside', rejected['error'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), expected_head)
        self.assertEqual(self.fixture.manifest_path.read_bytes(), manifest_before)
        self.assertEqual(
            self.fixture.git(
                'rev-parse', '--verify',
                'refs/heads/syncwheel/draft/agentwheel-mixed-derived-paths',
                check=False,
            ),
            '',
        )

    def test_agentwheel_response_shape_unchanged_for_derived_route(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        self.fixture.enable_derived_paths('locks/')
        request = self.fixture.request(
            'preflight', operation_id='derived-response-shape',
            path='locks/codex.lock', before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        self.assertEqual(finalized['status'], 'verified')
        self.assertIsNotNone(finalized['productCommitSha'])
        self.assertIsNone(finalized['draftStackId'])
        self.assertIsNone(finalized['draftBranch'])
        self.assertIsNone(finalized['draftTipSha'])
        self.assertIsNone(finalized['controlCommitSha'])
        self.assertEqual(finalized['unmappedIntegrationCommits'], [])
        self.assertFalse(finalized['published'])

    def test_recovery_keeps_the_hook_validated_derived_candidate_immutable(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        self.fixture.enable_derived_paths('locks/')
        hook_log = self.fixture.root / 'validated-commits.log'
        hook = self.fixture.repo / '.git' / 'hooks' / 'commit-msg'
        hook.write_text(
            '#!/bin/sh\n'
            f'printf "%s\\n" "$SYNCWHEEL_REVISION_PROVIDER_COMMIT" >> {shlex.quote(str(hook_log))}\n'
        )
        hook.chmod(0o755)
        request_payload = self.fixture.request(
            'preflight', operation_id='derived-hook-recovery',
            path='locks/codex.lock', before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        request = protocol.parse_request(request_payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(request_payload))
        )
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        protocol.handle_request(backend, request)

        class FaultAfterProductHooks(SYNCWHEEL.SyncwheelRevisionBackend):
            def checkpoint(inner_self, phase):
                if phase == 'product_hooks_validated':
                    raise protocol.RevisionProviderError('injected post-hook fault')

        with self.assertRaisesRegex(protocol.RevisionProviderError, 'post-hook fault'):
            protocol.handle_request(
                FaultAfterProductHooks(protocol), replace(request, action='finalize')
            )
        validated = hook_log.read_text().splitlines()
        self.assertEqual(len(validated), 1)
        time.sleep(1.1)

        recovered = protocol.handle_request(
            backend, replace(request, action='recover')
        )

        self.assertEqual(recovered['status'], 'verified')
        self.assertEqual(recovered['productCommitSha'], validated[0])
        self.assertEqual(hook_log.read_text().splitlines(), validated)

    def test_partial_route_journal_names_release_and_new_update_remedy(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        self.fixture.enable_derived_paths('locks/')
        payload = self.fixture.request(
            'preflight',
            operation_id='partial-route-journal',
            path='locks/codex.lock',
            before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        protocol.handle_request(backend, request)

        class FaultAfterRoute(SYNCWHEEL.SyncwheelRevisionBackend):
            def checkpoint(inner_self, phase):
                if phase == 'route_decided':
                    raise protocol.RevisionProviderError('injected after route')

        with self.assertRaisesRegex(protocol.RevisionProviderError, 'after route'):
            protocol.handle_request(
                FaultAfterRoute(protocol), replace(request, action='finalize')
            )
        journal = backend.load_journal(request)
        journal.pop('productPathObjects')
        backend.save_journal(request, journal)

        rejected, _ = self.fixture.protocol_request(
            {**payload, 'action': 'recover'}, expected=2
        )

        self.assertIn('journaled productPathObjects is missing or invalid', rejected['error'])
        self.assertIn('release the prepared operation', rejected['error'])
        self.assertIn('run a new Agentwheel update', rejected['error'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), payload['expectedHead'])
        released, _ = self.fixture.protocol_request(
            {**payload, 'action': 'release'}
        )
        self.assertEqual(released['status'], 'released')

    def test_manifest_base_provider_operation_can_land_end_to_end(self):
        request = self.fixture.request(
            'preflight', operation_id='landability-repro'
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )
        self.assertEqual(finalized['status'], 'verified')

        preview = json.loads(
            self.fixture.cli(
                'stack', 'land', finalized['draftStackId'], '--allow-direct',
                '--operation-id', 'provider-land',
            ).stdout
        )
        self.assertEqual(preview['status'], 'ready')
        applied = json.loads(
            self.fixture.cli(
                'stack', 'land', finalized['draftStackId'], '--allow-direct',
                '--operation-id', 'provider-land', '--plan-digest', preview['planDigest'],
                '--apply',
            ).stdout
        )
        self.assertEqual(applied['status'], 'succeeded')
        self.assertEqual(
            self.fixture.git('ls-remote', '--heads', str(self.fixture.remote), 'main').split()[0],
            finalized['draftTipSha'],
        )

    def test_manifest_invalidated_pending_receipt_expires_with_ledger_remedy(self):
        class ManifestChangedAfterProductObjects(SYNCWHEEL.SyncwheelRevisionBackend):
            def checkpoint(inner_self, phase):
                if phase == 'product_objects_prepared':
                    manifest_path = inner_self._repo_root(request) / '.syncwheel' / 'manifest.json'
                    manifest = json.loads(manifest_path.read_text())
                    manifest['revision_provider_test_marker'] = 'manifest changed'
                    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        payload = self.fixture.request('preflight', operation_id='manifest-expiry')
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        protocol.handle_request(backend, request)

        with self.assertRaisesRegex(
            protocol.RevisionProviderError,
            r'operation manifest-expiry expired: manifest changed before draft object preparation; '
            r'run a new Agentwheel update',
        ):
            protocol.handle_request(
                ManifestChangedAfterProductObjects(protocol),
                replace(request, action='finalize'),
            )

        journal = backend.load_journal(request)
        self.assertEqual(journal['phase'], 'expired')
        self.assertEqual(journal['expiration']['remedy'], 'run a new Agentwheel update')
        events = SYNCWHEEL.load_ledger_events(self.fixture.repo)
        expired = [
            event for event in events
            if event['type'] == 'revision_provider_expired'
            and event['payload']['operation_id'] == request.operation_id
        ]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]['payload']['remedy'], 'run a new Agentwheel update')
        with self.assertRaisesRegex(
            protocol.RevisionProviderError,
            r'operation manifest-expiry expired: manifest changed before draft object preparation; '
            r'run a new Agentwheel update',
        ):
            protocol.handle_request(backend, replace(request, action='recover'))

    def test_manifest_drift_before_route_expires_terminally(self):
        payload = self.fixture.request(
            'preflight', operation_id='drift-before-route'
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        protocol.handle_request(backend, request)
        manifest = self.fixture.read_manifest()
        manifest['unrelated_marker'] = 'changed after preflight'
        self.fixture.write_manifest(manifest)

        with self.assertRaisesRegex(
            protocol.RevisionProviderError,
            r'operation drift-before-route expired: manifest changed after preflight; '
            r'run a new Agentwheel update',
        ):
            protocol.handle_request(backend, replace(request, action='finalize'))

        journal = backend.load_journal(request)
        self.assertEqual(journal['phase'], 'expired')
        expired = [
            event for event in SYNCWHEEL.load_ledger_events(self.fixture.repo)
            if event['type'] == 'revision_provider_expired'
            and event['payload']['operation_id'] == request.operation_id
        ]
        self.assertEqual(len(expired), 1)

    def test_unrelated_manifest_edit_does_not_expire_a_derived_receipt(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        self.fixture.enable_derived_paths('locks/')
        payload = self.fixture.request(
            'preflight', operation_id='unrelated-derived-manifest-edit',
            path='locks/codex.lock', before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        protocol.handle_request(backend, request)

        class EditUnrelatedManifestAfterRoute(SYNCWHEEL.SyncwheelRevisionBackend):
            changed = False

            def checkpoint(inner_self, phase):
                if phase == 'route_decided' and not inner_self.changed:
                    inner_self.changed = True
                    manifest = self.fixture.read_manifest()
                    manifest['unrelated_marker'] = 'preserve me'
                    self.fixture.write_manifest(manifest)
                    raise protocol.RevisionProviderError('injected after unrelated edit')

        with self.assertRaisesRegex(protocol.RevisionProviderError, 'unrelated edit'):
            protocol.handle_request(
                EditUnrelatedManifestAfterRoute(protocol),
                replace(request, action='finalize'),
            )

        recovered = protocol.handle_request(
            backend, replace(request, action='recover')
        )

        self.assertEqual(recovered['status'], 'verified')
        self.assertEqual(self.fixture.read_manifest()['unrelated_marker'], 'preserve me')
        self.assertEqual(
            self.fixture.git('diff', '--name-only'), '.syncwheel/manifest.json'
        )
        self.assertEqual(
            self.fixture.git('show', f"{recovered['productCommitSha']}:.syncwheel/manifest.json"),
            self.fixture.git('show', f"{payload['expectedHead']}:.syncwheel/manifest.json"),
        )

    def test_composition_change_expires_derived_receipt_terminally(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        self.fixture.enable_derived_paths('locks/')
        payload = self.fixture.request(
            'preflight', operation_id='derived-composition-expiry',
            path='locks/codex.lock', before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        protocol.handle_request(backend, request)

        class ChangeCompositionAfterRoute(SYNCWHEEL.SyncwheelRevisionBackend):
            changed = False

            def checkpoint(inner_self, phase):
                if phase == 'route_decided' and not inner_self.changed:
                    inner_self.changed = True
                    manifest = self.fixture.read_manifest()
                    manifest['integration']['strategy'] = 'merge-stacks'
                    self.fixture.write_manifest(manifest)

        with self.assertRaisesRegex(
            protocol.RevisionProviderError,
            r'operation derived-composition-expiry expired: integration composition changed '
            r'while derived projection was pending; run a new Agentwheel update',
        ):
            protocol.handle_request(
                ChangeCompositionAfterRoute(protocol),
                replace(request, action='finalize'),
            )

        journal = backend.load_journal(request)
        self.assertEqual(journal['phase'], 'expired')
        self.assertNotEqual(
            journal['expiration']['observedDigest'],
            journal['expiration']['currentDigest'],
        )
        expired = [
            event for event in SYNCWHEEL.load_ledger_events(self.fixture.repo)
            if event['type'] == 'revision_provider_expired'
            and event['payload']['operation_id'] == request.operation_id
        ]
        self.assertEqual(len(expired), 1)

    def test_derived_paths_change_after_cas_expires_terminally(self):
        self.fixture.install_existing_stack(
            path='locks/codex.lock', content='first-owner\n'
        )
        self.fixture.enable_derived_paths('locks/')
        payload = self.fixture.request(
            'preflight', operation_id='derived-paths-expiry',
            path='locks/codex.lock', before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        protocol.handle_request(backend, request)

        class ChangeDerivedPathsAfterCas(SYNCWHEEL.SyncwheelRevisionBackend):
            changed = False

            def checkpoint(inner_self, phase):
                if phase == 'integration_product_cas' and not inner_self.changed:
                    inner_self.changed = True
                    manifest = self.fixture.read_manifest()
                    manifest['integration']['derived_paths'] = ['other/']
                    self.fixture.write_manifest(manifest)

        with self.assertRaisesRegex(
            protocol.RevisionProviderError,
            r'operation derived-paths-expiry expired: integration.derived_paths changed '
            r'while derived projection was pending; run a new Agentwheel update',
        ):
            protocol.handle_request(
                ChangeDerivedPathsAfterCas(protocol),
                replace(request, action='finalize'),
            )

        journal = backend.load_journal(request)
        self.assertEqual(journal['phase'], 'expired')
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), journal['candidateProductCommitSha'])
        self.assertNotEqual(
            journal['expiration']['observedDigest'],
            journal['expiration']['currentDigest'],
        )

    def test_expiry_is_idempotent_after_a_crash_between_journal_and_ledger(self):
        payload = self.fixture.request(
            'preflight', operation_id='journal-first-expiry'
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        protocol.handle_request(backend, request)

        class CrashAfterExpirationJournal(SYNCWHEEL.SyncwheelRevisionBackend):
            changed = False

            def checkpoint(inner_self, phase):
                if phase == 'product_objects_prepared' and not inner_self.changed:
                    inner_self.changed = True
                    manifest = self.fixture.read_manifest()
                    manifest['expiry_marker'] = 'first'
                    self.fixture.write_manifest(manifest)
                elif phase == 'receipt_expired':
                    raise protocol.RevisionProviderError('injected after expiration journal')

        with self.assertRaisesRegex(protocol.RevisionProviderError, 'expiration journal'):
            protocol.handle_request(
                CrashAfterExpirationJournal(protocol),
                replace(request, action='finalize'),
            )
        stored = copy.deepcopy(backend.load_journal(request)['expiration'])
        self.assertEqual(backend.load_journal(request)['phase'], 'expired')
        self.assertEqual(SYNCWHEEL.load_ledger_events(self.fixture.repo), [])
        manifest = self.fixture.read_manifest()
        manifest['expiry_marker'] = 'second'
        self.fixture.write_manifest(manifest)

        for _ in range(2):
            with self.assertRaisesRegex(
                protocol.RevisionProviderError,
                r'operation journal-first-expiry expired: manifest changed before draft object '
                r'preparation; run a new Agentwheel update',
            ):
                protocol.handle_request(backend, replace(request, action='recover'))

        journal = backend.load_journal(request)
        self.assertEqual(journal['expiration'], stored)
        expired = [
            event for event in SYNCWHEEL.load_ledger_events(self.fixture.repo)
            if event['type'] == 'revision_provider_expired'
        ]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]['payload']['current_digest'], stored['currentDigest'])

    def test_release_and_recover_on_expired_receipt_are_terminal(self):
        payload = self.fixture.request(
            'preflight', operation_id='terminal-expired-actions'
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        protocol.handle_request(backend, request)
        manifest = self.fixture.read_manifest()
        manifest['expiry_marker'] = 'changed'
        self.fixture.write_manifest(manifest)
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'expired'):
            protocol.handle_request(backend, replace(request, action='finalize'))

        released = protocol.handle_request(
            backend, replace(request, action='release')
        )
        self.assertEqual(released['status'], 'expired')
        with self.assertRaisesRegex(
            protocol.RevisionProviderError,
            r'operation terminal-expired-actions expired: manifest changed after preflight; '
            r'run a new Agentwheel update',
        ):
            protocol.handle_request(backend, replace(request, action='recover'))
        self.assertEqual(backend.load_journal(request)['phase'], 'expired')

    def test_check_on_expired_receipt_returns_the_terminal_error(self):
        payload = self.fixture.request(
            'preflight', operation_id='check-expired-receipt'
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        protocol.handle_request(backend, request)
        manifest = self.fixture.read_manifest()
        manifest['expiry_marker'] = 'changed'
        self.fixture.write_manifest(manifest)
        with self.assertRaisesRegex(protocol.RevisionProviderError, 'expired'):
            protocol.handle_request(backend, replace(request, action='finalize'))

        with self.assertRaisesRegex(
            protocol.RevisionProviderError,
            r'operation check-expired-receipt expired: manifest changed after preflight; '
            r'run a new Agentwheel update',
        ):
            protocol.handle_request(
                backend, protocol.parse_request(self.fixture.check_request(payload))
            )

    def test_conflict_diagnostic_names_the_conflicted_path(self):
        self.fixture.install_existing_stack(path='base.txt', content='stack\n')
        payload = self.fixture.request(
            'preflight',
            operation_id='projection-conflict-detail',
            path='base.txt',
            before=self.fixture.sha256('stack\n'),
            after_content='provider\n',
        )
        request = protocol.parse_request(payload)
        backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
        protocol.handle_request(
            backend, protocol.parse_request(self.fixture.check_request(payload))
        )
        (self.fixture.repo / 'base.txt').write_text('provider\n')
        protocol.handle_request(backend, request)
        journal = backend.load_journal(request)
        journal.update(
            backend.prepare_product_commit(request, protocol.product_commit_message(request))
        )
        journal['projectionBaseSha'] = self.fixture.git('rev-parse', 'origin/main')
        backend.save_journal(request, journal)

        with self.assertRaises(protocol.RevisionProviderError) as rejected:
            backend.prepare_draft_projection(request, journal)
        message = str(rejected.exception)
        self.assertIn('conflicts: base.txt', message)
        self.assertIn(f'base {journal["projectionBaseSha"]}', message)
        self.assertNotRegex(message, r'conflicts: [0-9a-f]{40}')

    def test_preflight_rejects_dirty_checkout_and_operation_id_collision(self):
        dirty = self.fixture.request('preflight', operation_id='dirty-op')
        (self.fixture.repo / 'unrelated.txt').write_text('dirty\n')
        response, _ = self.fixture.protocol_request(
            self.fixture.check_request(dirty), expected=2
        )
        self.assertIn('completely clean', response['error'])
        (self.fixture.repo / 'unrelated.txt').unlink()

        request = self.fixture.request('preflight', operation_id='collision-op')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        collision = {**request, 'reason': 'A different intent.'}
        response, _ = self.fixture.protocol_request(collision, expected=2)
        self.assertIn('operationId collision', response['error'])

    def test_preflight_rejects_changes_outside_exact_path_scope(self):
        request = self.fixture.request('preflight', operation_id='scope-op')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        (self.fixture.repo / 'outside.txt').write_text('outside\n')
        response, _ = self.fixture.protocol_request(
            request, expected=2
        )
        self.assertIn('outside the declared allowlist', response['error'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), request['expectedHead'])
        journal = (
            Path(self.fixture.git('rev-parse', '--git-common-dir'))
            / 'syncwheel' / 'revision-provider' / 'scope-op.json'
        )
        if not journal.is_absolute():
            journal = self.fixture.repo / journal
        self.assertFalse(journal.exists())

    def test_no_repository_delta_creates_no_empty_stack(self):
        request = self.fixture.request(
            'preflight',
            operation_id='no-delta-op',
        )
        request['paths'] = []
        self.fixture.protocol_request(self.fixture.check_request(request))
        self.fixture.protocol_request(request)
        response, _ = self.fixture.protocol_request({**request, 'action': 'finalize'})
        self.assertEqual(response['status'], 'no-repository-delta')
        self.assertIsNone(response['productCommitSha'])
        self.assertIsNone(response['draftStackId'])
        self.assertIsNone(response['draftTipSha'])
        manifest = json.loads((self.fixture.repo / '.syncwheel/manifest.json').read_text())
        self.assertEqual(response['manifestDigest'], SYNCWHEEL.manifest_digest(manifest))
        self.assertEqual(manifest['stacks'], [])

    def test_release_is_allowed_only_before_git_or_manifest_mutation(self):
        request = self.fixture.request('preflight', operation_id='release-op')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        released, _ = self.fixture.protocol_request({**request, 'action': 'release'})
        self.assertEqual(released['status'], 'released')
        journal_root = self.fixture.provider_journal_root()
        self.assertFalse((journal_root / 'release-op.json').exists())

    def test_finalize_without_a_preflight_journal_has_nullable_recovery_fields(self):
        request = self.fixture.request('finalize', operation_id='missing-preflight-op')
        response, _ = self.fixture.protocol_request(request, expected=2)
        self.assertIn('has no prepared journal', response['error'])
        self.assertEqual(response['expectedHead'], request['expectedHead'])
        for field in (
            'resultingHead',
            'productCommitSha',
            'draftStackId',
            'draftBranch',
            'draftTipSha',
            'controlCommitSha',
            'manifestDigest',
        ):
            self.assertIsNone(response[field])
        self.assertEqual(response['unmappedIntegrationCommits'], [])
        self.assertFalse(response['published'])

    def test_no_commit_is_audited_without_creating_git_or_stack_state(self):
        request = self.fixture.request(
            'preflight', operation_id='no-commit-op', no_commit=True
        )
        head_before = request['expectedHead']
        remote_before = self.fixture.remote_heads()
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)

        response, _ = self.fixture.protocol_request({**request, 'action': 'finalize'})
        self.assertEqual(response['status'], 'revisioning-skipped')
        self.assertEqual(response['resultingHead'], head_before)
        self.assertIsNone(response['productCommitSha'])
        self.assertIsNone(response['draftStackId'])
        self.assertIsNone(response['draftTipSha'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), head_before)
        self.assertEqual(self.fixture.remote_heads(), remote_before)
        self.assertEqual(self.fixture.git('status', '--porcelain'), '?? feature.txt')
        manifest = json.loads((self.fixture.repo / '.syncwheel/manifest.json').read_text())
        self.assertEqual(manifest['stacks'], [])
        journal = json.loads(
            (self.fixture.provider_journal_root() / 'no-commit-op.json').read_text()
        )
        self.assertEqual(journal['phase'], 'verified')
        self.assertEqual(journal['terminalStatus'], 'revisioning-skipped')
        self.assertEqual(journal['request']['reason'], request['reason'])

    def test_mode_only_change_is_rejected_with_a_precise_v1_error(self):
        payload = self.fixture.request(
            'preflight', path='base.txt', before=self.fixture.sha256('base\n'),
            after_content='base\n',
        )
        with self.assertRaisesRegex(
            protocol.RevisionProviderError, 'mode-only change.*no mode lease'
        ):
            protocol.parse_request(payload)

    def test_conflicting_projection_moves_no_managed_ref(self):
        fixture = RevisionProviderRepository()
        try:
            fixture.install_existing_stack(path='base.txt')
            before_head = fixture.git('rev-parse', 'HEAD')
            payload = fixture.request(
                'preflight', operation_id='conflict-projection', path='base.txt',
                before=fixture.sha256('existing\n'),
                after_content='agentwheel\n',
            )
            request = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(
                backend, protocol.parse_request(fixture.check_request(payload))
            )
            (fixture.repo / 'base.txt').write_text('agentwheel\n')
            protocol.handle_request(backend, request)
            journal = backend.load_journal(request)
            journal.update(
                backend.prepare_product_commit(
                    request, protocol.product_commit_message(request)
                )
            )
            journal['projectionBaseSha'] = fixture.git('rev-parse', 'origin/main')
            backend.save_journal(request, journal)
            with self.assertRaisesRegex(
                protocol.RevisionProviderError, 'conflicting draft projection'
            ):
                backend.prepare_draft_projection(request, journal)
            self.assertEqual(fixture.git('rev-parse', 'HEAD'), before_head)
            self.assertEqual(
                fixture.git(
                    'rev-parse', '--verify',
                    f'refs/heads/{request.draft_branch}',
                    check=False,
                ),
                '',
            )
        finally:
            fixture.close()

    def test_draft_ref_race_fails_before_integration_advances(self):
        request = self.fixture.request('preflight', operation_id='draft-race')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        self.fixture.git(
            'update-ref', 'refs/heads/syncwheel/draft/agentwheel-draft-race',
            request['expectedHead'],
        )
        rejected, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}, expected=2
        )
        self.assertIn('managed local ref lease was lost', rejected['error'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), request['expectedHead'])

    def test_existing_managed_ref_drift_fails_before_new_draft_or_integration(self):
        self.fixture.install_existing_stack()
        request = self.fixture.request('preflight', operation_id='managed-ref-drift')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        old_stack = self.fixture.git('rev-parse', 'syncwheel/stack/existing')
        moved = self.fixture.git(
            'commit-tree', f'{old_stack}^{{tree}}', '-p', old_stack,
            '-m', 'test: concurrent stack ref movement',
        )
        self.fixture.git(
            'update-ref', 'refs/heads/syncwheel/stack/existing', moved, old_stack
        )
        rejected, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}, expected=2
        )
        self.assertIn('managed local ref lease was lost', rejected['error'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), request['expectedHead'])
        self.assertEqual(
            self.fixture.git(
                'rev-parse', '--verify',
                'refs/heads/syncwheel/draft/agentwheel-managed-ref-drift',
                check=False,
            ),
            '',
        )

    def test_symlink_and_parent_substitution_fail_before_ref_ownership(self):
        for scenario in ('leaf', 'parent'):
            with self.subTest(scenario=scenario):
                fixture = RevisionProviderRepository()
                try:
                    path = 'feature.txt' if scenario == 'leaf' else 'config/feature.txt'
                    request = fixture.request(
                        'preflight', operation_id=f'{scenario}-symlink', path=path
                    )
                    fixture.protocol_request(fixture.check_request(request))
                    target = fixture.repo / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text('feature\n')
                    fixture.protocol_request(request)
                    if scenario == 'leaf':
                        target.unlink()
                        target.symlink_to(fixture.repo / 'base.txt')
                    else:
                        original = fixture.repo / 'config-original'
                        target.parent.rename(original)
                        target.parent.symlink_to(original, target_is_directory=True)
                    rejected, _ = fixture.protocol_request(
                        {**request, 'action': 'finalize'}, expected=2
                    )
                    self.assertIn('symbolic link', rejected['error'])
                    self.assertEqual(fixture.git('rev-parse', 'HEAD'), request['expectedHead'])
                    self.assertEqual(
                        fixture.git(
                            'rev-parse', '--verify',
                            f'refs/heads/syncwheel/draft/agentwheel-{scenario}-symlink',
                            check=False,
                        ),
                        '',
                    )
                finally:
                    fixture.close()

    def test_hook_side_effect_is_detected_before_draft_ownership(self):
        hook = self.fixture.repo / '.git' / 'hooks' / 'pre-commit'
        hook.write_text('#!/bin/sh\nprintf "side effect\\n" > hook-side-effect.txt\n')
        hook.chmod(0o755)
        request = self.fixture.request('preflight', operation_id='hook-side-effect')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        rejected, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}, expected=2
        )
        self.assertIn('hook changed repository state', rejected['error'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), request['expectedHead'])
        self.assertEqual(
            self.fixture.git(
                'rev-parse', '--verify',
                'refs/heads/syncwheel/draft/agentwheel-hook-side-effect',
                check=False,
            ),
            '',
        )

    def test_hook_snapshot_detects_tag_head_and_worktree_side_effects(self):
        scenarios = {
            'tag': 'git tag revision-provider-hook-tag\n',
            'head': 'git symbolic-ref HEAD refs/heads/main\n',
            'worktree': (
                'git_dir=$(git rev-parse --absolute-git-dir)\n'
                'git worktree add --detach "$git_dir/hook-worktree" HEAD >/dev/null\n'
            ),
        }
        for scenario, body in scenarios.items():
            with self.subTest(scenario=scenario):
                fixture = RevisionProviderRepository()
                try:
                    hook = fixture.repo / '.git' / 'hooks' / 'pre-commit'
                    hook.write_text('#!/bin/sh\nset -eu\n' + body)
                    hook.chmod(0o755)
                    request = fixture.request(
                        'preflight', operation_id=f'hook-{scenario}-side-effect'
                    )
                    fixture.protocol_request(fixture.check_request(request))
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    fixture.protocol_request(request)
                    rejected, _ = fixture.protocol_request(
                        {**request, 'action': 'finalize'}, expected=2
                    )
                    self.assertIn('hook changed repository state', rejected['error'])
                    self.assertEqual(
                        fixture.git(
                            'rev-parse', '--verify',
                            f'refs/heads/syncwheel/draft/agentwheel-hook-{scenario}-side-effect',
                            check=False,
                        ),
                        '',
                    )
                finally:
                    fixture.close()

    def test_hook_snapshot_detects_same_oid_symbolic_ref_changes(self):
        for mutation in ('retarget', 'type'):
            with self.subTest(mutation=mutation):
                fixture = RevisionProviderRepository()
                try:
                    alias, _, second, object_oid = (
                        fixture.install_same_oid_remote_alias()
                    )
                    hook = fixture.repo / '.git' / 'hooks' / 'pre-commit'
                    if mutation == 'retarget':
                        body = f'git symbolic-ref {alias} {second}\n'
                    else:
                        body = (
                            f'git update-ref --no-deref {alias} {object_oid}\n'
                        )
                    hook.write_text('#!/bin/sh\nset -eu\n' + body)
                    hook.chmod(0o755)
                    payload = fixture.request(
                        'preflight', operation_id=f'hook-symbolic-{mutation}'
                    )
                    fixture.protocol_request(fixture.check_request(payload))
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    fixture.protocol_request(payload)
                    rejected, _ = fixture.protocol_request(
                        {**payload, 'action': 'finalize'}, expected=2
                    )
                    self.assertIn('hook changed repository state', rejected['error'])
                    self.assertEqual(
                        fixture.git('rev-parse', 'HEAD'), payload['expectedHead']
                    )
                    self.assertEqual(
                        fixture.git(
                            'rev-parse', '--verify',
                            f'refs/heads/syncwheel/draft/agentwheel-hook-symbolic-{mutation}',
                            check=False,
                        ),
                        '',
                    )
                finally:
                    fixture.close()

    def test_final_snapshot_detects_same_oid_symbolic_ref_changes(self):
        for mutation in ('retarget', 'type'):
            with self.subTest(mutation=mutation):
                fixture = RevisionProviderRepository()
                try:
                    alias, _, second, object_oid = (
                        fixture.install_same_oid_remote_alias()
                    )
                    payload = fixture.request(
                        'preflight', operation_id=f'final-symbolic-{mutation}'
                    )
                    request = protocol.parse_request(payload)
                    backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
                    protocol.handle_request(
                        backend,
                        protocol.parse_request(fixture.check_request(payload)),
                    )
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    protocol.handle_request(backend, request)

                    class FinalRefDriftBackend(SYNCWHEEL.SyncwheelRevisionBackend):
                        changed = False

                        def checkpoint(self, phase):
                            if phase != 'control_committed' or self.changed:
                                return
                            self.changed = True
                            if mutation == 'retarget':
                                fixture.git('symbolic-ref', alias, second)
                            else:
                                fixture.git(
                                    'update-ref', '--no-deref', alias, object_oid
                                )

                    with self.assertRaisesRegex(
                        protocol.RevisionProviderError,
                        'operation changed a remote-tracking ref',
                    ):
                        protocol.handle_request(
                            FinalRefDriftBackend(protocol),
                            replace(request, action='finalize'),
                        )
                finally:
                    fixture.close()

    def test_pinned_local_base_ref_movement_after_preflight_fails_closed(self):
        fixture = RevisionProviderRepository(base_ref='main')
        try:
            payload = fixture.request('preflight', operation_id='base-after-preflight')
            fixture.protocol_request(fixture.check_request(payload))
            (fixture.repo / 'feature.txt').write_text('feature\n')
            fixture.protocol_request(payload)
            journal = json.loads(
                (fixture.provider_journal_root() / 'base-after-preflight.json').read_text()
            )
            self.assertEqual(journal['baseRef'], 'main')
            self.assertEqual(journal['baseRefFullName'], 'refs/heads/main')
            old_base = journal['baseRefSha']
            self.assertEqual(journal['baseRefObjectSha'], old_base)
            new_base = fixture.git(
                'commit-tree', f'{old_base}^{{tree}}', '-p', old_base,
                '-m', 'test: advance pinned local base',
            )
            fixture.git('update-ref', 'refs/heads/main', new_base, old_base)

            rejected, _ = fixture.protocol_request(
                {**payload, 'action': 'finalize'}, expected=2
            )
            self.assertIn('managed local ref lease was lost', rejected['error'])
            self.assertIn('refs/heads/main', rejected['error'])
            self.assertEqual(fixture.git('rev-parse', 'HEAD'), payload['expectedHead'])
            self.assertEqual(
                fixture.git(
                    'rev-parse', '--verify',
                    'refs/heads/syncwheel/draft/agentwheel-base-after-preflight',
                    check=False,
                ),
                '',
            )
        finally:
            fixture.close()

    def test_check_refuses_a_preexisting_unmapped_integration_commit(self):
        (self.fixture.repo / 'unmapped.txt').write_text('unmapped\n')
        self.fixture.git('add', 'unmapped.txt')
        self.fixture.git('commit', '-q', '-m', 'test: preexisting unmapped commit')
        request = self.fixture.request('preflight', operation_id='unmapped-op')
        response, _ = self.fixture.protocol_request(
            self.fixture.check_request(request), expected=2
        )
        self.assertIn('integration already contains unmapped commits', response['error'])
        self.assertFalse(self.fixture.provider_journal_root().exists())

    def test_commit_hooks_receive_exact_product_and_control_indexes(self):
        pre_commit = self.fixture.repo / '.git' / 'hooks' / 'pre-commit'
        pre_commit.write_text(
            '#!/bin/sh\n'
            'set -eu\n'
            'git_dir=$(git rev-parse --absolute-git-dir)\n'
            'names=$(git diff --cached --name-only)\n'
            'case "$names" in\n'
            '  feature.txt|.syncwheel/manifest.json) ;;\n'
            '  *) echo "unexpected staged paths: $names" >&2; exit 42 ;;\n'
            'esac\n'
            'printf "%s\\n" "$names" >> "$git_dir/revision-hook.log"\n'
        )
        pre_commit.chmod(0o755)
        commit_message = self.fixture.repo / '.git' / 'hooks' / 'commit-msg'
        commit_message.write_text(
            '#!/bin/sh\n'
            'set -eu\n'
            'git_dir=$(git rev-parse --absolute-git-dir)\n'
            'grep -q "^Agentwheel-Operation: " "$1"\n'
            'git diff --cached --name-only >> "$git_dir/revision-message-hook.log"\n'
        )
        commit_message.chmod(0o755)
        request = self.fixture.request('preflight', operation_id='hook-pass-op')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        response, _ = self.fixture.protocol_request({**request, 'action': 'finalize'})
        self.assertEqual(response['status'], 'verified')
        self.assertEqual(
            (self.fixture.repo / '.git' / 'revision-hook.log').read_text().splitlines(),
            ['feature.txt', '.syncwheel/manifest.json'],
        )
        self.assertEqual(
            (
                self.fixture.repo / '.git' / 'revision-message-hook.log'
            ).read_text().splitlines(),
            ['feature.txt', '.syncwheel/manifest.json'],
        )

    def test_commit_message_hook_cannot_change_the_deterministic_message(self):
        hook = self.fixture.repo / '.git' / 'hooks' / 'commit-msg'
        hook.write_text('#!/bin/sh\nprintf "\\nmutated\\n" >> "$1"\n')
        hook.chmod(0o755)
        request = self.fixture.request('preflight', operation_id='message-mutation-op')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        response, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}, expected=2
        )
        self.assertIn('commit-msg hook modified', response['error'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), request['expectedHead'])

    def test_rejected_commit_hook_moves_no_ref_and_keeps_recovery_fields(self):
        hook = self.fixture.repo / '.git' / 'hooks' / 'pre-commit'
        hook.write_text(
            '#!/bin/sh\n'
            'echo "blocked by policy" >&2\n'
            'exit 23\n'
        )
        hook.chmod(0o755)
        request = self.fixture.request('preflight', operation_id='hook-reject-op')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)
        response, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}, expected=2
        )
        self.assertIn('pre-commit hook rejected', response['error'])
        self.assertEqual(response['expectedHead'], request['expectedHead'])
        self.assertEqual(response['resultingHead'], request['expectedHead'])
        self.assertIsNone(response['productCommitSha'])
        self.assertFalse(response['published'])
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), request['expectedHead'])
        self.assertEqual(
            self.fixture.git(
                'rev-parse', '--verify',
                'refs/heads/syncwheel/draft/agentwheel-hook-reject-op',
                check=False,
            ),
            '',
        )
        journal = json.loads(
            (self.fixture.provider_journal_root() / 'hook-reject-op.json').read_text()
        )
        self.assertEqual(journal['phase'], 'prepared')
        self.assertIsNotNone(journal['candidateProductCommitSha'])
        self.assertIsNone(journal['productCommitSha'])

    def test_prepare_commit_message_hook_fails_closed_and_same_journal_retries(self):
        hook = self.fixture.repo / '.git' / 'hooks' / 'prepare-commit-msg'
        hook.write_text('#!/bin/sh\necho "message mutation forbidden" >&2\n')
        hook.chmod(0o755)
        request = self.fixture.request('preflight', operation_id='prepare-hook-op')
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'feature.txt').write_text('feature\n')
        self.fixture.protocol_request(request)

        rejected, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}, expected=2
        )
        self.assertIn('prepare-commit-msg hooks are unsupported', rejected['error'])
        journal_path = self.fixture.provider_journal_root() / 'prepare-hook-op.json'
        prepared = json.loads(journal_path.read_text())
        candidate = prepared['candidateProductCommitSha']
        self.assertIsNotNone(candidate)
        self.assertEqual(prepared['phase'], 'prepared')
        self.assertEqual(self.fixture.git('rev-parse', 'HEAD'), request['expectedHead'])

        hook.unlink()
        recovered, _ = self.fixture.protocol_request({**request, 'action': 'recover'})
        self.assertEqual(recovered['status'], 'verified')
        self.assertEqual(recovered['productCommitSha'], candidate)
        self.assertEqual(self.fixture.git('status', '--porcelain'), '')

    def test_active_active_preflight_rechecks_a_fresh_aligned_handoff(self):
        fixture = RevisionProviderRepository(coordination_mode='active-active')
        try:
            aligned = fixture.request('preflight', operation_id='aligned-handoff-op')
            aligned['paths'] = []
            fixture.protocol_request(fixture.check_request(aligned))
            prepared, _ = fixture.protocol_request(aligned)
            self.assertEqual(prepared['status'], 'prepared')
            aligned_journal = json.loads(
                (fixture.provider_journal_root() / 'aligned-handoff-op.json').read_text()
            )
            self.assertEqual(aligned_journal['coordination']['mode'], 'active-active')
            self.assertRegex(aligned_journal['coordination']['stateTip'], r'^[0-9a-f]{40}$')
            fixture.protocol_request({**aligned, 'action': 'release'})

            request = fixture.request('preflight', operation_id='active-handoff-op')
            ready, _ = fixture.protocol_request(fixture.check_request(request))
            self.assertEqual(ready['status'], 'ready')

            head = request['expectedHead']
            drift = fixture.git(
                'commit-tree', f'{head}^{{tree}}', '-p', head,
                '-m', 'test: concurrent unmanaged remote advance',
            )
            fixture.git(
                'push', '-q', 'origin',
                f'{drift}:refs/heads/main-integration',
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            rejected, _ = fixture.protocol_request(request, expected=2)
            self.assertIn(
                'coordination state does not match fresh remote refs',
                rejected['error'],
            )
            self.assertFalse(
                (fixture.provider_journal_root() / 'active-handoff-op.json').exists()
            )
            self.assertEqual(fixture.git('rev-parse', 'HEAD'), head)
        finally:
            fixture.close()

    def test_active_active_finalize_stops_owned_and_unpublished_at_handoff_base(self):
        fixture = RevisionProviderRepository(coordination_mode='active-active')
        try:
            request = fixture.request('preflight', operation_id='owned-unpublished')
            remote_before = fixture.remote_heads()
            fixture.protocol_request(fixture.check_request(request))
            (fixture.repo / 'feature.txt').write_text('feature\n')
            fixture.protocol_request(request)
            response, _ = fixture.protocol_request({**request, 'action': 'finalize'})
            self.assertEqual(response['status'], 'verified')
            self.assertFalse(response['published'])
            self.assertEqual(fixture.remote_heads(), remote_before)
            journal = json.loads(
                (fixture.provider_journal_root() / 'owned-unpublished.json').read_text()
            )
            self.assertEqual(journal['publicationState'], 'owned-but-unpublished')
            self.assertEqual(journal['coordination']['mode'], 'active-active')
            self.assertRegex(journal['coordination']['stateTip'], r'^[0-9a-f]{40}$')
            self.assertRegex(journal['coordination']['manifestDigest'], r'^[0-9a-f]{64}$')
        finally:
            fixture.close()

    def test_active_active_handoff_accepts_owned_control_commit_only_ahead(self):
        fixture = RevisionProviderRepository(coordination_mode='active-active')
        try:
            manifest = fixture.repo / '.syncwheel' / 'manifest.json'
            original = manifest.read_text()
            manifest.write_text(original + '\n')
            fixture.git('add', '.syncwheel/manifest.json')
            fixture.git('commit', '-q', '-m', 'syncwheel: intermediate control state')
            manifest.write_text(original)
            fixture.git('add', '.syncwheel/manifest.json')
            fixture.git('commit', '-q', '-m', 'syncwheel: restore published control state')

            next_request = fixture.request('preflight', operation_id='after-control-ahead')
            ready, _ = fixture.protocol_request(fixture.check_request(next_request))

            self.assertEqual(ready['status'], 'ready')
        finally:
            fixture.close()

    def test_active_active_handoff_rejects_non_control_commit_ahead(self):
        fixture = RevisionProviderRepository(coordination_mode='active-active')
        try:
            manifest = fixture.repo / '.syncwheel' / 'manifest.json'
            original = manifest.read_text()
            manifest.write_text(original + '\n')
            fixture.git('add', '.syncwheel/manifest.json')
            fixture.git('commit', '-q', '-m', 'syncwheel: intermediate control state')
            manifest.write_text(original)
            fixture.git('add', '.syncwheel/manifest.json')
            fixture.git('commit', '-q', '-m', 'syncwheel: restore published control state')

            (fixture.repo / 'unowned.txt').write_text('unowned\n')
            fixture.git('add', 'unowned.txt')
            fixture.git('commit', '-q', '-m', 'test: unowned product commit')
            next_request = fixture.request('preflight', operation_id='after-product-ahead')
            rejected, _ = fixture.protocol_request(
                fixture.check_request(next_request), expected=2
            )
            self.assertIn('integration already contains unmapped commits', rejected['error'])
        finally:
            fixture.close()

    def test_untrailed_lock_commit_stays_unmapped(self):
        self.fixture.enable_derived_paths('locks/', on_base=True)
        path = 'locks/codex.lock'
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / path).write_text('manual\n')
        self.fixture.git('add', '--', path)
        self.fixture.git('commit', '-q', '-m', 'test: manual lock update')
        commit = self.fixture.git('rev-parse', 'HEAD')
        self.fixture.append_derived_provenance(
            'untrailed-lock', commit, [path]
        )
        request = self.fixture.request(
            'preflight', operation_id='untrailed-lock'
        )

        rejected, _ = self.fixture.protocol_request(
            self.fixture.check_request(request), expected=2
        )

        self.assertIn('integration already contains unmapped commits', rejected['error'])
        self.assertIn(commit, rejected['error'])

    def test_false_derived_trailer_in_message_body_stays_unmapped(self):
        self.fixture.enable_derived_paths('locks/', on_base=True)
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('manual\n')
        self.fixture.git('add', 'locks/codex.lock')
        self.fixture.git(
            'commit', '-q', '-m',
            'test: false trailer body\n\n'
            'Syncwheel-Derived-Projection: false-body\n'
            'This prose keeps the line outside the trailer block.',
        )
        commit = self.fixture.git('rev-parse', 'HEAD')
        message = self.fixture.git('show', '-s', '--format=%B', commit)
        parsed = subprocess.run(
            ['git', 'interpret-trailers', '--parse'], cwd=self.fixture.repo,
            text=True, input=message, capture_output=True, check=True,
        )
        self.assertEqual(parsed.stdout, '')
        request = self.fixture.request(
            'preflight', operation_id='false-trailer-body'
        )

        rejected, _ = self.fixture.protocol_request(
            self.fixture.check_request(request), expected=2
        )

        self.assertIn('integration already contains unmapped commits', rejected['error'])
        self.assertIn(commit, rejected['error'])

    def test_derived_trailers_with_unknown_operation_stay_unmapped(self):
        self.fixture.enable_derived_paths('locks/', on_base=True)
        path = 'locks/codex.lock'
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / path).write_text('manual\n')
        self.fixture.git('add', '--', path)
        digest = self.fixture.staged_derived_digest(path)
        self.fixture.git(
            'commit',
            '-q',
            '-m',
            'test: unproven derived projection',
            '-m',
            'Syncwheel-Derived-Projection: nonexistent-operation\n'
            f'Syncwheel-Derived-Paths: {digest}',
        )
        commit = self.fixture.git('rev-parse', 'HEAD')
        request = self.fixture.request(
            'preflight', operation_id='unknown-derived-operation'
        )

        rejected, _ = self.fixture.protocol_request(
            self.fixture.check_request(request), expected=2
        )

        self.assertIn('integration already contains unmapped commits', rejected['error'])
        self.assertIn(commit, rejected['error'])

    def test_verified_requires_every_declared_stack_present_in_integration(self):
        product = self.fixture.install_existing_stack()
        manifest = self.fixture.read_manifest()
        self.fixture.git('reset', '--hard', 'origin/main')
        self.fixture.write_manifest(manifest)
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: omit declared stack projection')
        request = self.fixture.request(
            'preflight', operation_id='missing-declared-stack'
        )

        rejected, _ = self.fixture.protocol_request(
            self.fixture.check_request(request), expected=2
        )

        self.assertIn('declared stack(s) are missing from integration', rejected['error'])
        self.assertIn(product, rejected['error'])

    def test_verified_gate_ignores_stacks_outside_integration_stacks(self):
        self.fixture.git(
            'switch', '-q', '-c', 'syncwheel/stack/outside', 'origin/main'
        )
        (self.fixture.repo / 'outside.txt').write_text('outside\n')
        self.fixture.git('add', 'outside.txt')
        self.fixture.git('commit', '-q', '-m', 'test: outside stack')
        product = self.fixture.git('rev-parse', 'HEAD')
        self.fixture.git('switch', '-q', 'main-integration')
        manifest = self.fixture.read_manifest()
        manifest['defaults']['integration_membership'] = 'legacy'
        manifest['stacks'].append(
            {
                'id': 'outside',
                'branch': 'syncwheel/stack/outside',
                'base': 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'main-integration',
                'commits': [product],
                'state': 'draft',
                'publication': {'enabled': False},
            }
        )
        self.fixture.write_manifest(manifest)
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: declare outside stack')
        request = self.fixture.request(
            'preflight', operation_id='outside-stack-is-allowed'
        )

        response, _ = self.fixture.protocol_request(
            self.fixture.check_request(request)
        )

        self.assertEqual(response['status'], 'ready')

    def test_land_rejects_a_source_containing_a_derived_commit(self):
        self.fixture.enable_derived_paths('locks/', on_base=True)
        self.fixture.git(
            'switch', '-q', '-c', 'syncwheel/stack/derived-source', 'origin/main'
        )
        path = 'locks/line\nbreak.lock'
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / path).write_text('derived\n')
        self.fixture.git('add', '--', path)
        derived = self.fixture.commit_derived_projection(
            'derived-source',
            [path],
            subject='test: derived source',
        )
        self.fixture.git('switch', '-q', 'main-integration')
        self.fixture.git('merge', '-q', '--ff-only', 'syncwheel/stack/derived-source')
        manifest = self.fixture.read_manifest()
        manifest['stacks'].append(
            {
                'id': 'derived-source',
                'branch': 'syncwheel/stack/derived-source',
                'base': 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'main-integration',
                'commits': [derived],
                'state': 'draft',
                'publication': {'enabled': False},
            }
        )
        manifest['integration']['stacks'].append('derived-source')
        self.fixture.write_manifest(manifest)
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: register derived source')

        rejected = self.fixture.cli(
            'stack', 'land', 'derived-source', '--allow-direct', expected=2
        )

        self.assertIn('source contains derived projection commit', rejected.stderr)
        self.assertIn(derived, rejected.stderr)

    def test_common_dir_provenance_blocks_land_from_a_linked_worktree(self):
        self.fixture.enable_derived_paths('locks/', on_base=True)
        self.fixture.git(
            'switch', '-q', '-c', 'syncwheel/stack/linked-derived', 'origin/main'
        )
        path = 'locks/codex.lock'
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / path).write_text('derived\n')
        self.fixture.git('add', '--', path)
        derived = self.fixture.commit_derived_projection(
            'linked-derived', [path], subject='test: linked derived source'
        )
        self.fixture.git('switch', '-q', 'main-integration')
        self.fixture.git('merge', '-q', '--ff-only', 'syncwheel/stack/linked-derived')
        manifest = self.fixture.read_manifest()
        manifest['stacks'].append(
            {
                'id': 'linked-derived',
                'branch': 'syncwheel/stack/linked-derived',
                'base': 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'main-integration',
                'commits': [derived],
                'state': 'draft',
                'publication': {'enabled': False},
            }
        )
        manifest['integration']['stacks'].append('linked-derived')
        self.fixture.write_manifest(manifest)
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: register linked derived source')
        lane = self.fixture.root / 'linked-land'
        self.fixture.git(
            'worktree', 'add', '-q', '--detach', str(lane), 'main-integration'
        )
        try:
            common_path = SYNCWHEEL.derived_provenance_store_path(self.fixture.repo)
            self.assertTrue(common_path.is_file())
            self.assertEqual(common_path, SYNCWHEEL.derived_provenance_store_path(lane))
            self.assertEqual(common_path.stat().st_mode & 0o777, 0o600)
            lane_manifest, _ = SYNCWHEEL.load_manifest(lane)
            self.assertTrue(
                SYNCWHEEL.is_derived_projection_commit(
                    lane, lane_manifest, derived
                )
            )

            rejected = self.fixture.cli_at(
                lane,
                'stack',
                'land',
                'linked-derived',
                '--allow-direct',
                expected=2,
            )

            self.assertIn(
                'source contains derived projection commit', rejected.stderr
            )
            self.assertIn(derived, rejected.stderr)
            self.assertEqual(
                self.fixture.git('rev-parse', 'refs/remotes/origin/main'),
                self.fixture.git('ls-remote', str(self.fixture.remote), 'refs/heads/main').split()[0],
            )
        finally:
            self.fixture.git('worktree', 'remove', str(lane))

    def test_linked_worktree_check_and_rebuild_share_common_provenance(self):
        self.fixture.enable_derived_paths('locks/', on_base=True)
        self.fixture.install_existing_stack(
            path='locks/codex.lock',
            content='first-owner\n',
            manifest_on_base=True,
        )
        request = self.fixture.request(
            'preflight',
            operation_id='linked-provider-derived',
            path='locks/codex.lock',
            before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        self.fixture.protocol_request(request)
        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )
        lane = self.fixture.root / 'linked-rebuild'
        self.fixture.git(
            'worktree', 'add', '-q', '--detach', str(lane), 'main-integration'
        )
        try:
            lane_manifest, _ = SYNCWHEEL.load_manifest(lane)
            self.assertTrue(
                SYNCWHEEL.is_derived_projection_commit(
                    lane, lane_manifest, finalized['productCommitSha']
                )
            )
            subprocess.run(
                [
                    'git',
                    'switch',
                    '-q',
                    '--ignore-other-worktrees',
                    'main-integration',
                ],
                cwd=lane,
                check=True,
            )
            self.fixture.cli_at(
                lane,
                'hooks',
                'remove',
                '--disable',
                '--reason',
                'linked revision-provider fixture',
                '--apply',
            )
            lane_check = self.fixture.check_request(
                self.fixture.request(
                    'preflight', operation_id='linked-provider-check'
                )
            )
            lane_check['repositoryRoot'] = str(lane.resolve())
            lane_check['expectedHead'] = self.fixture.git(
                'rev-parse', 'HEAD', cwd=lane
            )
            checked = self.fixture.cli_at(
                lane, 'revision-provider', payload=lane_check
            )
            self.assertEqual(json.loads(checked.stdout)['status'], 'ready')
            subprocess.run(
                ['git', 'switch', '-q', '--detach'], cwd=lane, check=True
            )

            self.fixture.cli_at(lane, 'int', 'rebuild')
            stale = self.fixture.cli_at(lane, 'validate', expected=1)

            self.assertIn('derived-projection-stale', stale.stdout)
            self.assertIn('locks/codex.lock', stale.stdout)
            self.assertIn('run a new Agentwheel update', stale.stdout)
        finally:
            self.fixture.git('worktree', 'remove', str(lane))

    def test_narrowed_derived_paths_have_an_executable_rebuild_remedy(self):
        self.fixture.enable_derived_paths('locks/', on_base=True)
        path = 'locks/codex.lock'
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / path).write_text('derived\n')
        self.fixture.git('add', '--', path)
        derived = self.fixture.commit_derived_projection(
            'narrowed-derived', [path], subject='test: derived before narrowing'
        )
        self.fixture.git('switch', '-q', 'main')
        manifest = self.fixture.read_manifest()
        manifest['integration']['derived_paths'] = ['locks/graph/']
        self.fixture.write_manifest(manifest)
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: narrow derived paths')
        policy_commit = self.fixture.git('rev-parse', 'HEAD')
        self.fixture.git('push', '-q', 'origin', 'main')
        self.fixture.git('switch', '-q', 'main-integration')
        self.fixture.git('cherry-pick', policy_commit)

        validated = self.fixture.cli('validate', expected=1)
        status = self.fixture.cli('status')
        planned = self.fixture.cli('plan', '--json', expected=1)
        push_preview = self.fixture.cli('int', 'push', '--dry-run')
        missing_reason = self.fixture.cli('int', 'rebuild', expected=2)

        for output in (validated.stdout, status.stdout):
            self.assertIn('derived-paths-narrowed', output)
            self.assertIn(derived, output)
            self.assertIn(path, output)
            self.assertIn('int rebuild --reason', output)
        plan = json.loads(planned.stdout)
        narrowed_action = next(
            item for item in plan if item['type'] == 'derived-paths-narrowed'
        )
        self.assertEqual(narrowed_action['commits'], [derived])
        self.assertEqual(narrowed_action['paths'], [path])
        self.assertIn('int rebuild --reason', narrowed_action['remedy'])
        self.assertIn('git push', push_preview.stdout)
        self.assertIn('reconciliation requires --reason', missing_reason.stderr)

        self.fixture.cli(
            'int',
            'rebuild',
            '--reason',
            'accept removal after derived path policy narrowing',
        )

        reconciled_manifest, _ = SYNCWHEEL.load_manifest(self.fixture.repo)
        self.assertEqual(
            SYNCWHEEL.derived_provenance_records(
                self.fixture.repo, reconciled_manifest
            ),
            [],
        )
        self.assertFalse(
            SYNCWHEEL.branch_contains(
                self.fixture.repo, 'main-integration', derived
            )
        )
        self.fixture.cli('validate')
        rebuilt = [
            event for event in SYNCWHEEL.load_ledger_events(self.fixture.repo)
            if event.get('type') == 'integration_rebuilt'
        ][-1]['payload']
        self.assertEqual(
            rebuilt['reason'],
            'accept removal after derived path policy narrowing',
        )
        self.assertEqual(
            rebuilt['derived_provenance_reconciled'][0]['commit'], derived
        )

    def test_active_active_narrowing_keeps_push_and_rebuild_available(self):
        self.fixture.close()
        self.fixture = RevisionProviderRepository(
            coordination_mode='active-active'
        )
        self.fixture.enable_derived_paths('locks/', on_base=True)
        self.fixture.cli('int', 'push')
        path = 'locks/codex.lock'
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / path).write_text('derived\n')
        self.fixture.git('add', '--', path)
        derived = self.fixture.commit_derived_projection(
            'coordinated-narrowing',
            [path],
            subject='test: coordinated derived before narrowing',
        )
        self.fixture.cli('int', 'push')

        self.fixture.git('switch', '-q', 'main')
        manifest = self.fixture.read_manifest()
        manifest['integration']['derived_paths'] = []
        self.fixture.write_manifest(manifest)
        self.fixture.git('add', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-q', '-m', 'test: empty derived paths')
        policy_commit = self.fixture.git('rev-parse', 'HEAD')
        self.fixture.git('push', '-q', 'origin', 'main')
        self.fixture.git('switch', '-q', 'main-integration')
        self.fixture.git('cherry-pick', policy_commit)

        validated = self.fixture.cli('validate', expected=1)
        status = self.fixture.cli('status')
        planned = self.fixture.cli('plan', '--json', expected=1)
        self.fixture.cli('int', 'push', '--dry-run')

        self.assertIn('derived-paths-narrowed', validated.stdout)
        self.assertIn('derived-paths-narrowed', status.stdout)
        self.assertEqual(
            next(
                item for item in json.loads(planned.stdout)
                if item['type'] == 'derived-paths-narrowed'
            )['commits'],
            [derived],
        )

        self.fixture.cli(
            'int',
            'rebuild',
            '--reason',
            'reconcile coordinated derived path removal',
        )
        self.fixture.cli('validate')
        self.fixture.cli('int', 'push')
        published = SYNCWHEEL.local_coordination_provenance_state(
            self.fixture.repo, self.fixture.read_manifest()
        )
        self.assertEqual(
            published['manifest']['integration']['derived_provenance'], []
        )

    def assert_provenance_commands_stay_executable(self, repo):
        outcomes = {}
        probes = (
            ('validate', ('validate',), True),
            ('status', ('status',), True),
            ('plan', ('plan', '--json'), True),
            ('handoff', ('handoff', '--no-fetch', '--json'), True),
            ('check', ('check',), True),
            ('reconcile', ('reconcile', '--json'), True),
            ('int rebuild', ('int', 'rebuild', '--dry-run', '--reason', 'inspect'), True),
            ('int push', ('int', 'push', '--dry-run'), False),
        )
        for name, argv, bounded in probes:
            result = run_cli(repo, *argv)
            outcomes[name] = result
            output = result.stdout + result.stderr
            self.assertNotIn('clone-local derived provenance conflicts', output, msg=name)
            self.assertNotIn('run syncwheel handoff and retry', output, msg=name)
            if bounded:
                self.assertIn(
                    result.returncode,
                    (0, 1),
                    msg=f'{name} exited {result.returncode}: {output}',
                )
        return outcomes

    def publish_a_competing_peer_record(self):
        """Leave this clone with a pending record the coordination snapshot has passed."""
        self.fixture.close()
        self.fixture = RevisionProviderRepository(coordination_mode='active-active')
        fixture = self.fixture
        fixture.enable_derived_paths('locks/', on_base=True)
        fixture.cli('int', 'push')
        peer = CoordinationPeer(fixture)
        path = 'locks/codex.lock'
        (fixture.repo / 'locks').mkdir()
        (fixture.repo / path).write_text('peer-a\n')
        fixture.git('add', '--', path)
        pending = fixture.commit_derived_projection(
            'peer-a-derived', [path], subject='test: peer A derived projection'
        )
        (peer.repo / 'locks').mkdir()
        (peer.repo / path).write_text('peer-b\n')
        peer.git('add', '--', path)
        published = peer.commit_derived_projection(
            'peer-b-derived', [path], subject='test: peer B derived projection'
        )
        peer.cli('int', 'push')
        fixture.git('fetch', '-q', 'origin')
        return path, pending, published

    def test_published_snapshot_supersedes_a_pending_peer_record(self):
        path, pending, published = self.publish_a_competing_peer_record()
        fixture = self.fixture

        outcomes = self.assert_provenance_commands_stay_executable(fixture.repo)

        effective, diverged = SYNCWHEEL.derived_provenance_snapshot(
            fixture.repo, fixture.read_manifest()
        )
        self.assertEqual([item['commit'] for item in effective], [published])
        self.assertEqual(
            diverged,
            [{
                'paths': [path],
                'base_commit': None,
                'local_commit': pending,
                'snapshot_commit': published,
            }],
        )
        self.assertIn('derived-provenance-diverged', outcomes['validate'].stdout)
        self.assertIn(pending, outcomes['validate'].stdout)
        self.assertIn(published, outcomes['validate'].stdout)
        self.assertIn(
            'syncwheel coordination provenance reset', outcomes['validate'].stdout
        )
        planned = next(
            item for item in json.loads(outcomes['plan'].stdout)
            if item['type'] == 'derived-provenance-diverged'
        )
        self.assertEqual(planned['paths'], [path])
        self.assertEqual(planned['local_commits'], [pending])
        self.assertEqual(planned['snapshot_commits'], [published])

        remedy = run_cli(
            fixture.repo, *shlex.split(planned['remedy'])[1:]
        )

        self.assertEqual(remedy.returncode, 0, msg=remedy.stdout + remedy.stderr)
        self.assertEqual(
            SYNCWHEEL.load_derived_provenance_store(fixture.repo)['overrides'], []
        )
        self.assertNotIn(
            'derived-provenance-diverged', run_cli(fixture.repo, 'validate').stdout
        )

    def test_a_new_local_record_rebinds_to_the_published_snapshot(self):
        path, _pending, published = self.publish_a_competing_peer_record()
        fixture = self.fixture
        self.assertEqual(
            len(SYNCWHEEL.derived_provenance_snapshot(
                fixture.repo, fixture.read_manifest()
            )[1]),
            1,
        )

        (fixture.repo / path).write_text('peer-a-again\n')
        fixture.git('add', '--', path)
        replacement = fixture.commit_derived_projection(
            'peer-a-rebound', [path], subject='test: peer A derived projection again'
        )

        effective, diverged = SYNCWHEEL.derived_provenance_snapshot(
            fixture.repo, fixture.read_manifest()
        )
        self.assertEqual(diverged, [])
        self.assertEqual([item['commit'] for item in effective], [replacement])
        self.assertEqual(
            [item['base_commit'] for item in SYNCWHEEL.load_derived_provenance_store(
                fixture.repo
            )['overrides']],
            [published],
        )
        self.assertNotIn(
            'derived-provenance-diverged', run_cli(fixture.repo, 'validate').stdout
        )

    def test_peer_provenance_resolution_keeps_a_pending_record_recoverable(self):
        self.fixture.close()
        self.fixture = RevisionProviderRepository(coordination_mode='active-active')
        fixture = self.fixture
        fixture.enable_derived_paths('locks/', on_base=True)
        fixture.cli('int', 'push')
        path = 'locks/codex.lock'
        (fixture.repo / 'locks').mkdir()
        (fixture.repo / path).write_text('published\n')
        fixture.git('add', '--', path)
        fixture.commit_derived_projection(
            'peer-a-published', [path], subject='test: peer A published projection'
        )
        fixture.cli('int', 'push')
        peer = CoordinationPeer(fixture)
        (fixture.repo / path).write_text('pending\n')
        fixture.git('add', '--', path)
        pending = fixture.commit_derived_projection(
            'peer-a-pending', [path], subject='test: peer A pending projection'
        )

        peer.git('switch', '-q', 'main')
        peer_manifest = peer.read_manifest()
        peer_manifest['integration']['derived_paths'] = []
        peer.write_manifest(peer_manifest)
        peer.git('add', '.syncwheel/manifest.json')
        peer.git('commit', '-q', '-m', 'test: empty derived paths')
        policy_commit = peer.git('rev-parse', 'HEAD')
        peer.git('push', '-q', 'origin', 'main')
        peer.git('switch', '-q', 'main-integration')
        peer.git('cherry-pick', policy_commit)
        peer.cli('validate', expected=1)
        peer.cli('int', 'rebuild', '--reason', SYNCWHEEL.DERIVED_PATHS_REBUILD_REASON)
        peer.cli('validate')
        peer.cli('int', 'push')
        published = SYNCWHEEL.local_coordination_provenance_state(
            peer.repo, peer.read_manifest()
        )
        self.assertEqual(
            published['manifest']['integration']['derived_provenance'], []
        )
        fixture.git('fetch', '-q', 'origin')

        outcomes = self.assert_provenance_commands_stay_executable(fixture.repo)

        effective, diverged = SYNCWHEEL.derived_provenance_snapshot(
            fixture.repo, fixture.read_manifest()
        )
        self.assertEqual(effective, [])
        self.assertEqual(
            [(item['paths'], item['local_commit'], item['snapshot_commit'])
             for item in diverged],
            [([path], pending, None)],
        )
        self.assertIn('derived-provenance-diverged', outcomes['validate'].stdout)
        self.assertIn(pending, outcomes['validate'].stdout)
        self.assertIn(
            'syncwheel coordination provenance reset', outcomes['validate'].stdout
        )

        remedy = run_cli(
            fixture.repo,
            'coordination', 'provenance', 'reset',
            '--reason', 'peer B resolved the published record',
        )

        self.assertEqual(remedy.returncode, 0, msg=remedy.stdout + remedy.stderr)
        self.assertEqual(
            SYNCWHEEL.load_derived_provenance_store(fixture.repo)['overrides'], []
        )
        self.assertEqual(
            [
                event['payload']['discarded'][0]['paths']
                for event in SYNCWHEEL.load_ledger_events(fixture.repo)
                if event['type'] == 'derived_provenance_reset'
            ],
            [[path]],
        )

    def test_common_provenance_writes_serialize_on_the_exclusive_store_lock(self):
        self.fixture.enable_derived_paths('locks/')
        manifest = self.fixture.read_manifest()
        record = {
            'operation_id': 'serialized-write',
            'commit': self.fixture.git('rev-parse', 'HEAD'),
            'paths': ['locks/codex.lock'],
            'paths_digest': '11' * 32,
            'composition_digest': '22' * 32,
        }
        lock_path = SYNCWHEEL.derived_provenance_store_lock_path(self.fixture.repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        written = threading.Event()
        failures = []

        def writer():
            try:
                SYNCWHEEL.record_common_derived_provenance(
                    self.fixture.repo, manifest, record
                )
            except Exception as exc:
                failures.append(exc)
            finally:
                written.set()

        with lock_path.open('a+b') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            thread = threading.Thread(target=writer)
            thread.start()
            serialized = not written.wait(3)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        thread.join(60)

        self.assertEqual(failures, [])
        self.assertTrue(written.wait(60))
        self.assertTrue(serialized, msg='a concurrent write ignored the store lock')
        self.assertEqual(
            [
                item['record']['operation_id']
                for item in SYNCWHEEL.load_derived_provenance_store(
                    self.fixture.repo
                )['overrides']
            ],
            ['serialized-write'],
        )

    def test_common_provenance_store_write_is_atomic_and_durable(self):
        store_path = SYNCWHEEL.derived_provenance_store_path(self.fixture.repo)
        first = {
            'version': SYNCWHEEL.DERIVED_PROVENANCE_STORE_VERSION,
            'overrides': [{
                'paths': ['locks/codex.lock'],
                'base_commit': None,
                'record': {
                    'operation_id': 'durable-first',
                    'commit': self.fixture.git('rev-parse', 'HEAD'),
                    'paths': ['locks/codex.lock'],
                    'paths_digest': '33' * 32,
                    'composition_digest': '44' * 32,
                },
            }],
        }
        store_path.parent.mkdir(parents=True, exist_ok=True)
        orphan = store_path.parent / f'.{store_path.name}.tmp-abandoned'
        orphan.write_bytes(b'{}')
        SYNCWHEEL.save_derived_provenance_store(self.fixture.repo, first)
        self.assertFalse(orphan.exists())
        original = store_path.read_bytes()
        real_fsync = os.fsync
        renames = []
        fsyncs = []

        def refuse_rename(source, target):
            renames.append((Path(source), Path(target)))
            raise OSError('rename refused')

        def record_fsync(descriptor):
            fsyncs.append(descriptor)
            return real_fsync(descriptor)

        with (
            mock.patch.object(SYNCWHEEL.os, 'replace', refuse_rename),
            mock.patch.object(SYNCWHEEL.os, 'fsync', record_fsync),
            self.assertRaises(OSError),
        ):
            SYNCWHEEL.save_derived_provenance_store(
                self.fixture.repo,
                SYNCWHEEL.default_derived_provenance_store(),
            )

        self.assertEqual(store_path.read_bytes(), original)
        self.assertEqual(len(renames), 1)
        source, target = renames[0]
        self.assertEqual(target, store_path)
        self.assertEqual(source.parent, store_path.parent)
        self.assertTrue(source.name.startswith(f'.{store_path.name}.tmp-'))
        self.assertTrue(fsyncs)
        self.assertEqual(
            sorted(store_path.parent.glob(f'.{store_path.name}.tmp-*')), []
        )

    def test_unreadable_common_provenance_store_names_an_executable_remedy(self):
        self.fixture.enable_derived_paths('locks/')
        store_path = SYNCWHEEL.derived_provenance_store_path(self.fixture.repo)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text('{"version": 1, "overrides"')

        blocked = run_cli(self.fixture.repo, 'validate')

        self.assertEqual(blocked.returncode, 2)
        self.assertIn('invalid derived provenance store', blocked.stderr)
        remedy = shlex.split(
            blocked.stderr.split('discard it with ', 1)[1].strip()
        )
        self.assertEqual(remedy[0], 'syncwheel')

        repaired = run_cli(self.fixture.repo, *remedy[1:])

        self.assertEqual(repaired.returncode, 0, msg=repaired.stdout + repaired.stderr)
        self.assertEqual(
            SYNCWHEEL.load_derived_provenance_store(self.fixture.repo),
            SYNCWHEEL.default_derived_provenance_store(),
        )
        self.fixture.cli('validate')

    def test_manifest_base_operation_resolves_a_stale_derived_record_end_to_end(self):
        self.fixture.enable_derived_paths('locks/')
        base = self.fixture.git('rev-parse', 'HEAD')
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('derived\n')
        self.fixture.git('add', 'locks/codex.lock')
        self.fixture.commit_derived_projection(
            'orphaned-derived',
            ['locks/codex.lock'],
            subject='test: orphaned derived projection',
        )
        self.fixture.git('reset', '--hard', base)
        self.fixture.cli('validate', expected=1)
        request = self.fixture.request(
            'preflight',
            operation_id='manifest-base-lock-replacement',
            path='locks/codex.lock',
            before=None,
            after_content='manifest-base\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('manifest-base\n')
        self.fixture.protocol_request(request)

        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )

        journal = json.loads(
            (
                self.fixture.provider_journal_root()
                / 'manifest-base-lock-replacement.json'
            ).read_text()
        )
        self.assertEqual(finalized['status'], 'verified')
        self.assertEqual(journal['projectionRoute'], 'manifest-base')
        self.assertEqual(
            SYNCWHEEL.derived_provenance_records(
                self.fixture.repo, self.fixture.read_manifest()
            ),
            [],
        )
        self.fixture.cli('validate')

    def test_rebuild_drops_derived_commit_and_reports_stale_blocker(self):
        self.fixture.enable_derived_paths('locks/', on_base=True)
        self.fixture.install_existing_stack(
            path='locks/codex.lock',
            content='first-owner\n',
            manifest_on_base=True,
        )
        request = self.fixture.request(
            'preflight', operation_id='derived-before-rebuild',
            path='locks/codex.lock', before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        self.fixture.protocol_request(request)
        finalized, _ = self.fixture.protocol_request(
            {**request, 'action': 'finalize'}
        )
        self.fixture.cli('int', 'rebuild')

        stale = self.fixture.cli('validate', expected=1)

        self.assertIn('derived-projection-stale', stale.stdout)
        self.assertIn('locks/codex.lock', stale.stdout)
        self.assertIn('run a new Agentwheel update', stale.stdout)
        replacement = self.fixture.request(
            'preflight', operation_id='derived-after-rebuild',
            path='locks/codex.lock', before=self.fixture.sha256('first-owner\n'),
            after_content='second-owner\n',
        )
        self.fixture.protocol_request(self.fixture.check_request(replacement))
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
        self.fixture.protocol_request(replacement)
        regenerated, _ = self.fixture.protocol_request(
            {**replacement, 'action': 'finalize'}
        )
        self.assertNotEqual(regenerated['productCommitSha'], finalized['productCommitSha'])
        validated = self.fixture.cli('validate')
        self.assertNotIn('derived-projection-stale', validated.stdout)

    def test_fresh_peer_reports_shared_derived_provenance_stale_after_rebuild(self):
        fixture = RevisionProviderRepository(coordination_mode='active-active')
        try:
            fixture.enable_derived_paths('locks/', on_base=True)
            fixture.cli('publish')
            fixture.install_existing_stack(
                path='locks/codex.lock',
                content='first-owner\n',
                manifest_on_base=True,
            )
            fixture.cli('publish')
            request = fixture.request(
                'preflight',
                operation_id='shared-derived-before-rebuild',
                path='locks/codex.lock',
                before=fixture.sha256('first-owner\n'),
                after_content='second-owner\n',
            )
            fixture.protocol_request(fixture.check_request(request))
            (fixture.repo / 'locks' / 'codex.lock').write_text('second-owner\n')
            fixture.protocol_request(request)
            finalized, _ = fixture.protocol_request(
                {**request, 'action': 'finalize'}
            )
            fixture.cli('int', 'push')

            peer = fixture.root / 'peer-b'
            subprocess.run(
                ['git', 'clone', '-q', str(fixture.remote), str(peer)],
                check=True,
            )
            subprocess.run(
                ['git', 'config', 'user.name', 'Revision Provider Peer B'],
                cwd=peer,
                check=True,
            )
            subprocess.run(
                ['git', 'config', 'user.email', 'peer-b@example.invalid'],
                cwd=peer,
                check=True,
            )
            subprocess.run(
                [
                    'git',
                    'switch',
                    '-q',
                    '-c',
                    'main-integration',
                    '--track',
                    'origin/main-integration',
                ],
                cwd=peer,
                check=True,
            )
            peer_manifest, _ = SYNCWHEEL.load_manifest(peer)
            state = SYNCWHEEL.coordination_state_from_commit(
                peer,
                'origin/syncwheel/state/revision-provider-test',
                'revision-provider-test',
            )
            self.assertEqual(SYNCWHEEL.load_ledger_events(peer), [])
            self.assertEqual(
                state['manifest']['integration']['derived_provenance'][0]['commit'],
                finalized['productCommitSha'],
            )
            self.assertEqual(
                SYNCWHEEL.validate_manifest(peer, peer_manifest)['errors'],
                [],
            )
            environment = os.environ.copy()
            environment['SYNCWHEEL_UPDATE_MODE'] = 'off'
            rebuilt = subprocess.run(
                ['python3', str(CLI), 'int', 'rebuild'],
                cwd=peer,
                text=True,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(
                rebuilt.returncode,
                0,
                msg=f'stdout={rebuilt.stdout}\nstderr={rebuilt.stderr}',
            )

            stale = subprocess.run(
                ['python3', str(CLI), 'validate'],
                cwd=peer,
                text=True,
                capture_output=True,
                env=environment,
            )

            self.assertEqual(stale.returncode, 1)
            self.assertIn('derived-projection-stale', stale.stdout)
            self.assertIn('locks/codex.lock', stale.stdout)
            self.assertIn('run a new Agentwheel update', stale.stdout)
        finally:
            fixture.close()

    def test_manifest_base_ownership_reconciles_a_stale_derived_path(self):
        self.fixture.enable_derived_paths('locks/')
        base = self.fixture.git('rev-parse', 'HEAD')
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('derived\n')
        self.fixture.git('add', 'locks/codex.lock')
        derived = self.fixture.commit_derived_projection(
            'orphaned-derived',
            ['locks/codex.lock'],
            subject='test: orphaned derived projection',
        )
        manifest = self.fixture.read_manifest()
        self.fixture.git('reset', '--hard', base)
        self.assertEqual(
            [
                item['path'] for item in SYNCWHEEL.stale_derived_projection_records(
                    self.fixture.repo, manifest, 'main-integration'
                )
            ],
            ['locks/codex.lock'],
        )

        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / 'locks' / 'codex.lock').write_text('manifest-base\n')
        self.fixture.git('add', 'locks/codex.lock')
        self.fixture.git('commit', '-q', '-m', 'test: manifest-base replacement')
        product = self.fixture.git('rev-parse', 'HEAD')
        SYNCWHEEL.resolve_common_derived_provenance(
            self.fixture.repo,
            manifest,
            ['locks/codex.lock'],
            expected_commit=derived,
        )
        SYNCWHEEL.append_ledger_event(
            self.fixture.repo,
            'manifest_saved',
            SYNCWHEEL.manifest_event_payload(
                self.fixture.manifest_path,
                manifest,
                'revision_provider_stack_ownership',
                {
                    'operation_id': 'manifest-base-replacement',
                    'product_commit': product,
                    'paths': ['locks/codex.lock'],
                },
            ),
            self.fixture.manifest_path,
        )

        self.assertEqual(
            SYNCWHEEL.stale_derived_projection_records(
                self.fixture.repo, manifest, 'main-integration'
            ),
            [],
        )

    def test_provider_requires_one_update_to_cover_every_stale_derived_path(self):
        self.fixture.enable_derived_paths('locks/')
        base = self.fixture.git('rev-parse', 'HEAD')
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / 'locks' / 'a.lock').write_text('a\n')
        (self.fixture.repo / 'locks' / 'b.lock').write_text('b\n')
        self.fixture.git('add', 'locks/a.lock', 'locks/b.lock')
        derived = self.fixture.commit_derived_projection(
            'orphaned-multi-path',
            ['locks/a.lock', 'locks/b.lock'],
            subject='test: orphaned multi-path projection',
        )
        self.fixture.git('reset', '--hard', base)
        request = self.fixture.request(
            'preflight',
            operation_id='partial-stale-repair',
            path='locks/a.lock',
            before=None,
            after_content='a\n',
        )

        self.fixture.protocol_request(self.fixture.check_request(request))
        (self.fixture.repo / 'locks').mkdir()
        (self.fixture.repo / 'locks' / 'a.lock').write_text('a\n')
        rejected, _ = self.fixture.protocol_request(request, expected=2)

        self.assertIn('derived-projection-stale', rejected['error'])
        self.assertIn('locks/a.lock', rejected['error'])
        self.assertIn('locks/b.lock', rejected['error'])


class RevisionProviderRecoveryTest(unittest.TestCase):
    def test_unowned_index_lock_is_never_removed_during_recovery(self):
        fixture = RevisionProviderRepository()
        try:
            payload = fixture.request(
                'preflight', operation_id='foreign-index-lock'
            )
            request = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(
                backend, protocol.parse_request(fixture.check_request(payload))
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            protocol.handle_request(backend, request)
            index_raw = fixture.git('rev-parse', '--git-path', 'index')
            index_path = Path(index_raw)
            if not index_path.is_absolute():
                index_path = fixture.repo / index_path
            lock_path = Path(f'{index_path}.lock')
            lock_path.write_bytes(b'foreign-writer-lock\n')
            with self.assertRaisesRegex(
                protocol.RevisionProviderError, 'locked by another writer'
            ):
                protocol.handle_request(
                    backend, replace(request, action='recover')
                )
            self.assertEqual(lock_path.read_bytes(), b'foreign-writer-lock\n')
            self.assertEqual(fixture.git('rev-parse', 'HEAD'), request.expected_head)
            self.assertEqual(
                fixture.git(
                    'rev-parse', '--verify',
                    f'refs/heads/{request.draft_branch}', check=False,
                ),
                '',
            )
            lock_path.unlink()
        finally:
            fixture.close()

    def test_sigkill_inside_owned_index_lock_recovers_without_staging_loss(self):
        child_source = r'''
import importlib.util
import json
from pathlib import Path
import sys
import time

scripts = Path(sys.argv[1])
sys.path.insert(0, str(scripts))
import syncwheel_revision_provider as protocol

spec = importlib.util.spec_from_file_location('syncwheel_sigkill_test', scripts / 'syncwheel.py')
syncwheel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(syncwheel)
request = protocol.parse_request(json.loads(Path(sys.argv[2]).read_text()))
marker = Path(sys.argv[3])
target = sys.argv[4]

class KillBackend(syncwheel.SyncwheelRevisionBackend):
    def checkpoint(self, phase):
        if phase == target:
            marker.write_text(phase + '\n')
            with marker.open('rb') as handle:
                import os
                os.fsync(handle.fileno())
            while True:
                time.sleep(1)

protocol.handle_request(KillBackend(protocol), request)
'''
        for kind in ('product', 'control'):
            with self.subTest(kind=kind):
                fixture = RevisionProviderRepository()
                process = None
                try:
                    payload = fixture.request(
                        'preflight', operation_id=f'sigkill-{kind}-index'
                    )
                    request = protocol.parse_request(payload)
                    backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
                    protocol.handle_request(
                        backend,
                        protocol.parse_request(fixture.check_request(payload)),
                    )
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    protocol.handle_request(backend, request)
                    finalize_payload = {**payload, 'action': 'finalize'}
                    request_path = fixture.root / f'{kind}-request.json'
                    marker = fixture.root / f'{kind}-index-lock-owned'
                    request_path.write_text(json.dumps(finalize_payload))
                    environment = os.environ.copy()
                    environment['SYNCWHEEL_UPDATE_MODE'] = 'off'
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            '-c',
                            child_source,
                            str(SCRIPTS),
                            str(request_path),
                            str(marker),
                            f'{kind}_index_lock_owned',
                        ],
                        cwd=fixture.repo,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=environment,
                    )
                    deadline = time.monotonic() + 30
                    while not marker.exists() and process.poll() is None:
                        if time.monotonic() >= deadline:
                            self.fail(
                                f'child did not enter {kind} index lock interval'
                            )
                        time.sleep(0.02)
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        self.fail(
                            f'child exited before {kind} index lock interval: '
                            f'{process.returncode}\nstdout={stdout}\nstderr={stderr}'
                        )

                    index_raw = fixture.git('rev-parse', '--git-path', 'index')
                    index_path = Path(index_raw)
                    if not index_path.is_absolute():
                        index_path = fixture.repo / index_path
                    lock_path = Path(f'{index_path}.lock')
                    backing_files = list(
                        (fixture.provider_journal_root() / 'index-alignment').glob(
                            f'{request.operation_id}-{kind}-*.index'
                        )
                    )
                    self.assertEqual(len(backing_files), 1)
                    self.assertTrue(lock_path.exists())
                    self.assertTrue(os.path.samefile(lock_path, backing_files[0]))

                    os.kill(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, -signal.SIGKILL)
                    process = None

                    recovered = protocol.handle_request(
                        SYNCWHEEL.SyncwheelRevisionBackend(protocol),
                        replace(request, action='recover'),
                    )
                    self.assertEqual(recovered['status'], 'verified')
                    self.assertFalse(lock_path.exists())
                    self.assertEqual(
                        list(
                            (fixture.provider_journal_root() / 'index-alignment').glob(
                                f'{request.operation_id}-{kind}-*.index'
                            )
                        ),
                        [],
                    )
                    self.assertEqual(fixture.git('status', '--porcelain'), '')
                    self.assertEqual(
                        fixture.git('write-tree'),
                        fixture.git('rev-parse', 'HEAD^{tree}'),
                    )
                finally:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.communicate(timeout=10)
                    fixture.close()

    def test_sigkill_process_group_inside_ref_transaction_requires_bounded_cleanup(self):
        child_source = r'''
import importlib.util
import json
from pathlib import Path
import sys
import time

scripts = Path(sys.argv[1])
sys.path.insert(0, str(scripts))
import syncwheel_revision_provider as protocol

spec = importlib.util.spec_from_file_location('syncwheel_ref_sigkill_test', scripts / 'syncwheel.py')
syncwheel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(syncwheel)
request = protocol.parse_request(json.loads(Path(sys.argv[2]).read_text()))
marker = Path(sys.argv[3])

class KillBackend(syncwheel.SyncwheelRevisionBackend):
    paused = False

    def checkpoint(self, phase):
        if phase == 'ref_transaction_prepared' and not self.paused:
            self.paused = True
            marker.write_text(phase + '\n')
            with marker.open('rb') as handle:
                import os
                os.fsync(handle.fileno())
            while True:
                time.sleep(1)

protocol.handle_request(KillBackend(protocol), request)
'''
        fixture = RevisionProviderRepository()
        process = None
        try:
            payload = fixture.request(
                'preflight', operation_id='sigkill-ref-transaction'
            )
            request = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(
                backend, protocol.parse_request(fixture.check_request(payload))
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            protocol.handle_request(backend, request)
            request_path = fixture.root / 'ref-transaction-request.json'
            marker = fixture.root / 'ref-transaction-prepared'
            request_path.write_text(json.dumps({**payload, 'action': 'finalize'}))
            environment = os.environ.copy()
            environment['SYNCWHEEL_UPDATE_MODE'] = 'off'
            process = subprocess.Popen(
                [
                    sys.executable,
                    '-c',
                    child_source,
                    str(SCRIPTS),
                    str(request_path),
                    str(marker),
                ],
                cwd=fixture.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            deadline = time.monotonic() + 30
            while not marker.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail('child did not enter prepared ref transaction')
                time.sleep(0.02)
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    'child exited before prepared ref transaction: '
                    f'{process.returncode}\nstdout={stdout}\nstderr={stderr}'
                )

            common = Path(fixture.git('rev-parse', '--git-common-dir'))
            if not common.is_absolute():
                common = fixture.repo / common
            alias_lock = common / 'refs/remotes/origin/HEAD.lock'
            referent_lock = common / 'refs/remotes/origin/main.lock'
            self.assertTrue(alias_lock.exists())
            self.assertTrue(referent_lock.exists())

            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            process = None

            ref_locks = sorted((common / 'refs').rglob('*.lock'))
            packed_lock = common / 'packed-refs.lock'
            if packed_lock.exists():
                ref_locks.append(packed_lock)
            head_lock = common / 'HEAD.lock'
            if head_lock.exists():
                ref_locks.append(head_lock)
            self.assertIn(alias_lock, ref_locks)
            self.assertIn(referent_lock, ref_locks)
            self.assertIn(head_lock, ref_locks)
            with self.assertRaisesRegex(
                protocol.RevisionProviderError,
                'automatic cleanup is forbidden',
            ) as rejected:
                protocol.handle_request(
                    backend, replace(request, action='recover')
                )
            self.assertIn(str(alias_lock), str(rejected.exception))
            self.assertIn(str(referent_lock), str(rejected.exception))
            self.assertTrue(alias_lock.exists())
            self.assertTrue(referent_lock.exists())

            for lock in ref_locks:
                lock.unlink()
            recovered = protocol.handle_request(
                backend, replace(request, action='recover')
            )
            self.assertEqual(recovered['status'], 'verified')
            self.assertEqual(fixture.git('status', '--porcelain'), '')
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            fixture.close()

    def test_sigkill_in_committed_reference_transaction_hook_gates_installed_candidate(self):
        child_source = r'''
import importlib.util
import json
from pathlib import Path
import sys

scripts = Path(sys.argv[1])
sys.path.insert(0, str(scripts))
import syncwheel_revision_provider as protocol

spec = importlib.util.spec_from_file_location(
    'syncwheel_ref_committed_sigkill_test', scripts / 'syncwheel.py'
)
syncwheel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(syncwheel)
request = protocol.parse_request(json.loads(Path(sys.argv[2]).read_text()))
protocol.handle_request(syncwheel.SyncwheelRevisionBackend(protocol), request)
'''
        rename_interposer_source = r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void pause_after_target_rename(const char *new_path) {
    const char *suffix = getenv("SYNCWHEEL_TEST_RENAME_DEST_SUFFIX");
    const char *marker = getenv("SYNCWHEEL_TEST_RENAME_MARKER");
    if (!new_path || !suffix || !marker) return;
    size_t path_length = strlen(new_path);
    size_t suffix_length = strlen(suffix);
    if (path_length < suffix_length) return;
    if (strcmp(new_path + path_length - suffix_length, suffix) != 0) return;
    int descriptor = open(marker, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (descriptor < 0) return;
    (void)write(descriptor, "renamed\n", 8);
    (void)fsync(descriptor);
    (void)close(descriptor);
    for (;;) pause();
}

int rename(const char *old_path, const char *new_path) {
    static int (*real_rename)(const char *, const char *);
    if (!real_rename) real_rename = dlsym(RTLD_NEXT, "rename");
    int result = real_rename(old_path, new_path);
    if (result == 0) pause_after_target_rename(new_path);
    return result;
}

int renameat(
    int old_directory, const char *old_path,
    int new_directory, const char *new_path
) {
    static int (*real_renameat)(int, const char *, int, const char *);
    if (!real_renameat) real_renameat = dlsym(RTLD_NEXT, "renameat");
    int result = real_renameat(
        old_directory, old_path, new_directory, new_path
    );
    if (result == 0) pause_after_target_rename(new_path);
    return result;
}
'''
        fixture = RevisionProviderRepository()
        process = None
        try:
            payload = fixture.request(
                'preflight', operation_id='sigkill-ref-committed'
            )
            request = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(
                backend, protocol.parse_request(fixture.check_request(payload))
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            protocol.handle_request(backend, request)

            common = Path(fixture.git('rev-parse', '--git-common-dir'))
            if not common.is_absolute():
                common = fixture.repo / common
            hook_log = common / 'reference-transaction-observed.log'
            hook_path = Path(
                fixture.git('rev-parse', '--git-path', 'hooks/reference-transaction')
            )
            if not hook_path.is_absolute():
                hook_path = fixture.repo / hook_path
            hook_path.write_text(
                '#!/bin/sh\n'
                f'printf "%s\\n" "$1" >> {shlex.quote(str(hook_log))}\n'
                'exit 0\n'
            )
            hook_path.chmod(0o755)

            interposer_source = fixture.root / 'rename-interposer.c'
            interposer_library = fixture.root / 'rename-interposer.so'
            interposer_source.write_text(rename_interposer_source)
            compile_result = subprocess.run(
                [
                    'cc', '-shared', '-fPIC', '-O2',
                    '-o', str(interposer_library), str(interposer_source), '-ldl',
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stderr or compile_result.stdout,
            )
            marker = fixture.root / 'target-ref-renamed'
            request_path = fixture.root / 'ref-committed-request.json'
            request_path.write_text(json.dumps({**payload, 'action': 'finalize'}))
            environment = os.environ.copy()
            environment.update(
                {
                    'SYNCWHEEL_UPDATE_MODE': 'off',
                    'LD_PRELOAD': str(interposer_library),
                    'SYNCWHEEL_TEST_RENAME_DEST_SUFFIX': (
                        f'refs/heads/{request.draft_branch}'
                    ),
                    'SYNCWHEEL_TEST_RENAME_MARKER': str(marker),
                }
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    '-c',
                    child_source,
                    str(SCRIPTS),
                    str(request_path),
                ],
                cwd=fixture.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            deadline = time.monotonic() + 30
            while not marker.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail('Git did not pause after the target ref rename')
                time.sleep(0.02)
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    'child exited before the post-rename pause: '
                    f'{process.returncode}\nstdout={stdout}\nstderr={stderr}'
                )

            journal = backend.load_journal(request)
            draft_ref = f'refs/heads/{request.draft_branch}'
            self.assertEqual(marker.read_text(), 'renamed\n')
            self.assertEqual(
                fixture.git('rev-parse', '--verify', draft_ref),
                journal['candidateDraftCommitSha'],
            )
            hook_states = hook_log.read_text().splitlines()
            self.assertIn('prepared', hook_states)
            self.assertNotIn('committed', hook_states)
            expected = backend._journal_ref_transaction_refs(request, journal)
            lock_paths = backend._ref_transaction_lock_paths(fixture.repo, expected)
            locks_at_pause = [
                path
                for path in lock_paths
                if path.exists() or path.is_symlink()
            ]
            self.assertTrue(
                locks_at_pause,
                'target rename must precede at least one transaction lock cleanup',
            )

            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            process = None
            self.assertEqual(
                fixture.git('rev-parse', '--verify', draft_ref),
                journal['candidateDraftCommitSha'],
            )
            stale = [
                path
                for path in lock_paths
                if path.exists() or path.is_symlink()
            ]
            self.assertEqual(stale, locks_at_pause)

            with self.assertRaisesRegex(
                protocol.RevisionProviderError,
                'automatic cleanup is forbidden',
            ) as rejected:
                protocol.handle_request(
                    backend, replace(request, action='recover')
                )
            for path in stale:
                self.assertIn(str(path), str(rejected.exception))
                self.assertTrue(path.exists() or path.is_symlink())

            for path in stale:
                path.unlink()
            recovered = protocol.handle_request(
                backend, replace(request, action='recover')
            )
            self.assertEqual(recovered['status'], 'verified')
            self.assertEqual(fixture.git('status', '--porcelain'), '')
            self.assertIn('committed', hook_log.read_text().splitlines())
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            fixture.close()

    def test_recovery_gate_covers_release_integration_final_and_verified_shortcuts(self):
        class FaultBackend(SYNCWHEEL.SyncwheelRevisionBackend):
            def __init__(self, provider_module, target):
                super().__init__(provider_module)
                self.target = target
                self.raised = False

            def checkpoint(self, phase):
                if phase == self.target and not self.raised:
                    self.raised = True
                    raise protocol.RevisionProviderError(
                        f'injected fault after {phase}'
                    )

        fixture = RevisionProviderRepository()
        try:
            payload = fixture.request(
                'preflight', operation_id='all-recovery-gates'
            )
            request = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(
                backend, protocol.parse_request(fixture.check_request(payload))
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            protocol.handle_request(backend, request)
            common = Path(fixture.git('rev-parse', '--git-common-dir'))
            if not common.is_absolute():
                common = fixture.repo / common
            head_lock = common / 'HEAD.lock'

            head_lock.write_bytes(b'unknown-ref-writer\n')
            with self.assertRaisesRegex(
                protocol.RevisionProviderError, 'automatic cleanup is forbidden'
            ):
                protocol.handle_request(
                    backend, replace(request, action='release')
                )
            self.assertEqual(head_lock.read_bytes(), b'unknown-ref-writer\n')
            head_lock.unlink()

            product_fault = FaultBackend(protocol, 'integration_product_cas')
            with self.assertRaisesRegex(
                protocol.RevisionProviderError,
                'injected fault after integration_product_cas',
            ):
                protocol.handle_request(
                    product_fault, replace(request, action='finalize')
                )
            journal = backend.load_journal(request)
            self.assertEqual(
                fixture.git('rev-parse', 'HEAD'),
                journal['candidateProductCommitSha'],
            )
            head_lock.write_bytes(b'unknown-ref-writer\n')
            with self.assertRaisesRegex(
                protocol.RevisionProviderError, 'automatic cleanup is forbidden'
            ):
                protocol.handle_request(
                    backend, replace(request, action='recover')
                )
            self.assertEqual(head_lock.read_bytes(), b'unknown-ref-writer\n')
            head_lock.unlink()

            final_fault = FaultBackend(protocol, 'control_committed')
            with self.assertRaisesRegex(
                protocol.RevisionProviderError,
                'injected fault after control_committed',
            ):
                protocol.handle_request(
                    final_fault, replace(request, action='recover')
                )
            self.assertEqual(backend.load_journal(request)['phase'], 'control_committed')
            head_lock.write_bytes(b'unknown-ref-writer\n')
            with self.assertRaisesRegex(
                protocol.RevisionProviderError, 'automatic cleanup is forbidden'
            ):
                protocol.handle_request(
                    backend, replace(request, action='recover')
                )
            self.assertEqual(head_lock.read_bytes(), b'unknown-ref-writer\n')
            head_lock.unlink()

            recovered = protocol.handle_request(
                backend, replace(request, action='recover')
            )
            self.assertEqual(recovered['status'], 'verified')
            head_lock.write_bytes(b'unknown-ref-writer\n')
            for action in ('recover', 'preflight'):
                with self.subTest(action=action):
                    with self.assertRaisesRegex(
                        protocol.RevisionProviderError,
                        'automatic cleanup is forbidden',
                    ):
                        protocol.handle_request(
                            backend, replace(request, action=action)
                        )
                    self.assertEqual(
                        head_lock.read_bytes(), b'unknown-ref-writer\n'
                    )
            head_lock.unlink()
            repeated = protocol.handle_request(
                backend, replace(request, action='recover')
            )
            self.assertEqual(repeated['status'], 'verified')
        finally:
            fixture.close()

    def test_index_cas_preserves_concurrent_product_and_control_staging(self):
        for kind in ('product', 'control'):
            with self.subTest(kind=kind):
                fixture = RevisionProviderRepository()
                try:
                    payload = fixture.request(
                        'preflight', operation_id=f'{kind}-index-race'
                    )
                    request = protocol.parse_request(payload)
                    backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
                    protocol.handle_request(
                        backend, protocol.parse_request(fixture.check_request(payload))
                    )
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    protocol.handle_request(backend, request)

                    class IndexRaceBackend(SYNCWHEEL.SyncwheelRevisionBackend):
                        raced = False
                        raced_index_sha256 = None

                        def checkpoint(self, phase):
                            if (
                                phase == f'before_{kind}_index_lock'
                                and not self.raced
                            ):
                                self.raced = True
                                (fixture.repo / 'outside.txt').write_text('concurrent\n')
                                fixture.git('add', '--', 'outside.txt')
                                self.raced_index_sha256 = self._index_sha256(
                                    fixture.repo
                                )

                    race_backend = IndexRaceBackend(protocol)
                    with self.assertRaisesRegex(
                        protocol.RevisionProviderError,
                        f'index lease was lost before {kind} alignment',
                    ):
                        protocol.handle_request(
                            race_backend,
                            replace(request, action='finalize'),
                        )
                    self.assertEqual(
                        race_backend._index_sha256(fixture.repo),
                        race_backend.raced_index_sha256,
                    )
                    self.assertIn(
                        'outside.txt',
                        fixture.git('diff', '--cached', '--name-only').splitlines(),
                    )
                    self.assertEqual(
                        fixture.git('show', ':outside.txt'), 'concurrent'
                    )
                    self.assertFalse(
                        (Path(fixture.git('rev-parse', '--git-path', 'index'))
                         if Path(fixture.git('rev-parse', '--git-path', 'index')).is_absolute()
                         else fixture.repo / fixture.git('rev-parse', '--git-path', 'index'))
                        .with_name('index.lock')
                        .exists()
                    )
                finally:
                    fixture.close()

    def test_pinned_local_base_ref_movement_between_phases_blocks_next_cas(self):
        fixture = RevisionProviderRepository(base_ref='main')
        try:
            payload = fixture.request('preflight', operation_id='base-between-phases')
            request = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(
                backend, protocol.parse_request(fixture.check_request(payload))
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            protocol.handle_request(backend, request)
            old_base = fixture.git('rev-parse', 'refs/heads/main')
            new_base = fixture.git(
                'commit-tree', f'{old_base}^{{tree}}', '-p', old_base,
                '-m', 'test: advance base between provider phases',
            )

            class BaseDriftBackend(SYNCWHEEL.SyncwheelRevisionBackend):
                moved = False

                def checkpoint(self, phase):
                    if phase == 'draft_ref_owned' and not self.moved:
                        self.moved = True
                        fixture.git(
                            'update-ref', 'refs/heads/main', new_base, old_base
                        )

            with self.assertRaisesRegex(
                protocol.RevisionProviderError, 'managed local ref lease was lost'
            ):
                protocol.handle_request(
                    BaseDriftBackend(protocol), replace(request, action='finalize')
                )
            self.assertEqual(fixture.git('rev-parse', 'HEAD'), payload['expectedHead'])
            self.assertRegex(
                fixture.git(
                    'rev-parse', '--verify',
                    'refs/heads/syncwheel/draft/agentwheel-base-between-phases',
                ),
                r'^[0-9a-f]{40}$',
            )
        finally:
            fixture.close()

    def test_existing_stack_movement_after_draft_cas_blocks_integration_cas(self):
        fixture = RevisionProviderRepository()
        try:
            fixture.install_existing_stack()
            payload = fixture.request('preflight', operation_id='post-draft-ref-drift')
            request = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(
                backend, protocol.parse_request(fixture.check_request(payload))
            )
            (fixture.repo / 'feature.txt').write_text('feature\n')
            protocol.handle_request(backend, request)
            old_stack = fixture.git('rev-parse', 'syncwheel/stack/existing')
            moved = fixture.git(
                'commit-tree', f'{old_stack}^{{tree}}', '-p', old_stack,
                '-m', 'test: drift after draft ownership',
            )

            class DriftBackend(SYNCWHEEL.SyncwheelRevisionBackend):
                def checkpoint(self, phase):
                    if phase == 'draft_ref_owned':
                        fixture.git(
                            'update-ref', 'refs/heads/syncwheel/stack/existing',
                            moved, old_stack,
                        )

            with self.assertRaisesRegex(
                protocol.RevisionProviderError, 'managed local ref lease was lost'
            ):
                protocol.handle_request(
                    DriftBackend(protocol), replace(request, action='finalize')
                )
            self.assertEqual(fixture.git('rev-parse', 'HEAD'), payload['expectedHead'])
            self.assertRegex(
                fixture.git(
                    'rev-parse', '--verify',
                    'refs/heads/syncwheel/draft/agentwheel-post-draft-ref-drift',
                ),
                r'^[0-9a-f]{40}$',
            )
        finally:
            fixture.close()

    def test_journaled_blob_is_not_rebound_after_a_symlink_swap(self):
        class FaultAfterObjects(SYNCWHEEL.SyncwheelRevisionBackend):
            def checkpoint(self, phase):
                if phase == 'product_objects_prepared':
                    raise protocol.RevisionProviderError('injected object preparation crash')

        fixture = RevisionProviderRepository()
        try:
            payload = fixture.request('preflight', operation_id='blob-swap')
            check = protocol.parse_request(fixture.check_request(payload))
            preflight = protocol.parse_request(payload)
            backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
            protocol.handle_request(backend, check)
            (fixture.repo / 'feature.txt').write_text('feature\n')
            protocol.handle_request(backend, preflight)
            with self.assertRaisesRegex(protocol.RevisionProviderError, 'injected'):
                protocol.handle_request(
                    FaultAfterObjects(protocol), replace(preflight, action='finalize')
                )
            target = fixture.repo / 'feature.txt'
            target.unlink()
            target.symlink_to(fixture.repo / 'base.txt')
            with self.assertRaisesRegex(protocol.RevisionProviderError, 'symbolic link'):
                protocol.handle_request(backend, replace(preflight, action='recover'))
            self.assertEqual(fixture.git('rev-parse', 'HEAD'), payload['expectedHead'])
            self.assertEqual(
                fixture.git(
                    'rev-parse', '--verify',
                    'refs/heads/syncwheel/draft/agentwheel-blob-swap', check=False,
                ),
                '',
            )
        finally:
            fixture.close()

    def test_fault_after_each_durable_phase_recovers_idempotently(self):
        class FaultBackend(SYNCWHEEL.SyncwheelRevisionBackend):
            def __init__(self, provider_module, target):
                super().__init__(provider_module)
                self.target = target
                self.raised = False

            def checkpoint(self, phase):
                if phase == self.target and not self.raised:
                    self.raised = True
                    raise protocol.RevisionProviderError(f'injected fault after {phase}')

        fault_points = (
            *protocol.PHASES,
            'product_objects_prepared',
            'route_decided',
            'product_hooks_validated',
            'draft_ref_cas',
            'ref_transaction_prepared',
            'draft_ref_owned',
            'integration_product_cas',
            'before_product_index_lock',
            'product_index_alignment_prepared',
            'product_index_backing_fsynced',
            'product_index_lock_owned',
            'product_index_cas',
            'integration_product_committed',
            'manifest_replace_written',
            'manifest_replaced',
            'ledger_event_written',
            'ledger_appended',
            'control_objects_prepared',
            'control_hooks_validated',
            'integration_control_cas',
            'before_control_index_lock',
            'control_index_alignment_prepared',
            'control_index_backing_fsynced',
            'control_index_lock_owned',
            'control_index_cas',
            'integration_control_committed',
        )
        for phase in fault_points:
            with self.subTest(phase=phase):
                fixture = RevisionProviderRepository()
                try:
                    payload = fixture.request(
                        'preflight', operation_id=f'fault-{phase.replace("_", "-")}'
                    )
                    check_request = protocol.parse_request(fixture.check_request(payload))
                    preflight_request = protocol.parse_request(payload)
                    fault_backend = FaultBackend(protocol, phase)
                    protocol.handle_request(fault_backend, check_request)
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    if phase == 'prepared':
                        with self.assertRaisesRegex(
                            protocol.RevisionProviderError, 'injected fault'
                        ):
                            protocol.handle_request(fault_backend, preflight_request)
                    else:
                        protocol.handle_request(fault_backend, preflight_request)
                    finalize_request = replace(preflight_request, action='finalize')
                    if phase != 'prepared':
                        with self.assertRaisesRegex(
                            protocol.RevisionProviderError, 'injected fault'
                        ):
                            protocol.handle_request(fault_backend, finalize_request)

                    normal_backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
                    journal_before = normal_backend.load_journal(preflight_request)
                    repeated_preflight = protocol.handle_request(
                        normal_backend, preflight_request
                    )
                    self.assertEqual(repeated_preflight['status'], 'prepared')
                    self.assertEqual(
                        normal_backend.load_journal(preflight_request), journal_before
                    )

                    recover = replace(preflight_request, action='recover')
                    response = protocol.handle_request(normal_backend, recover)
                    self.assertEqual(response['status'], 'verified')
                    repeated = protocol.handle_request(normal_backend, recover)
                    self.assertEqual(repeated, response)
                    self.assertEqual(fixture.git('status', '--porcelain'), '')
                finally:
                    fixture.close()


class RevisionProviderLedgerDurabilityTest(unittest.TestCase):
    def _fault_once(self, target):
        state = {'raised': False}

        def fault(stage):
            if stage == target and not state['raised']:
                state['raised'] = True
                raise RuntimeError(f'injected ledger fault at {stage}')

        return fault

    def test_incomplete_event_tail_is_recovered_deterministically(self):
        expectations = {
            'event_payload_half_written': ['survivor'],
            'event_payload_written': ['crashed', 'survivor'],
        }
        for stage, expected_names in expectations.items():
            with self.subTest(stage=stage):
                fixture = RevisionProviderRepository()
                original = SYNCWHEEL.ledger_io_checkpoint
                try:
                    SYNCWHEEL.ledger_io_checkpoint = self._fault_once(stage)
                    with self.assertRaisesRegex(RuntimeError, 'injected ledger fault'):
                        SYNCWHEEL.append_ledger_event(
                            fixture.repo, 'test_event', {'name': 'crashed'}
                        )
                    SYNCWHEEL.ledger_io_checkpoint = original
                    event = SYNCWHEEL.append_ledger_event(
                        fixture.repo, 'test_event', {'name': 'survivor'}
                    )
                    events = SYNCWHEEL.load_ledger_events(fixture.repo)
                    self.assertEqual(
                        [item['payload']['name'] for item in events], expected_names
                    )
                    self.assertEqual(
                        [item['seq'] for item in events], list(range(1, len(events) + 1))
                    )
                    self.assertEqual(event['seq'], len(events))
                    segment = next(
                        SYNCWHEEL.ledger_events_dir(fixture.repo).glob('*.jsonl')
                    )
                    self.assertTrue(segment.read_bytes().endswith(b'\n'))
                finally:
                    SYNCWHEEL.ledger_io_checkpoint = original
                    fixture.close()

    def test_checkpoint_write_fault_never_hides_a_durable_event(self):
        for stage in (
            'checkpoint_payload_half_written',
            'checkpoint_payload_written',
            'checkpoint_temp_fsynced',
            'checkpoint_replaced',
        ):
            with self.subTest(stage=stage):
                fixture = RevisionProviderRepository()
                original = SYNCWHEEL.ledger_io_checkpoint
                try:
                    SYNCWHEEL.ledger_io_checkpoint = self._fault_once(stage)
                    with self.assertRaisesRegex(RuntimeError, 'injected ledger fault'):
                        SYNCWHEEL.append_ledger_event(
                            fixture.repo, 'test_event', {'name': 'durable'}
                        )
                    SYNCWHEEL.ledger_io_checkpoint = original
                    state = SYNCWHEEL.load_ledger_state(fixture.repo)
                    self.assertEqual(state['last_seq'], 1)
                    self.assertEqual(state['event_count'], 1)
                    SYNCWHEEL.append_ledger_event(
                        fixture.repo, 'test_event', {'name': 'next'}
                    )
                    events = SYNCWHEEL.load_ledger_events(fixture.repo)
                    self.assertEqual([item['seq'] for item in events], [1, 2])
                    self.assertEqual(
                        [item['payload']['name'] for item in events],
                        ['durable', 'next'],
                    )
                    checkpoint = json.loads(
                        SYNCWHEEL.ledger_checkpoint_path(fixture.repo).read_text()
                    )
                    self.assertEqual(checkpoint, SYNCWHEEL.reduce_ledger_state(events))
                finally:
                    SYNCWHEEL.ledger_io_checkpoint = original
                    fixture.close()

    def test_provider_recovers_faults_inside_ownership_ledger_writes(self):
        for stage in (
            'event_payload_half_written',
            'event_payload_written',
            'checkpoint_payload_half_written',
            'checkpoint_replaced',
        ):
            with self.subTest(stage=stage):
                fixture = RevisionProviderRepository()
                original = SYNCWHEEL.ledger_io_checkpoint
                try:
                    payload = fixture.request(
                        'preflight', operation_id=f'ledger-{stage.replace("_", "-")}'
                    )
                    request = protocol.parse_request(payload)
                    backend = SYNCWHEEL.SyncwheelRevisionBackend(protocol)
                    protocol.handle_request(
                        backend, protocol.parse_request(fixture.check_request(payload))
                    )
                    (fixture.repo / 'feature.txt').write_text('feature\n')
                    protocol.handle_request(backend, request)
                    SYNCWHEEL.ledger_io_checkpoint = self._fault_once(stage)
                    with self.assertRaisesRegex(RuntimeError, 'injected ledger fault'):
                        protocol.handle_request(
                            backend, replace(request, action='finalize')
                        )
                    SYNCWHEEL.ledger_io_checkpoint = original
                    recovered = protocol.handle_request(
                        backend, replace(request, action='recover')
                    )
                    self.assertEqual(recovered['status'], 'verified')
                    ownership = [
                        event for event in SYNCWHEEL.load_ledger_events(fixture.repo)
                        if (event.get('payload', {}).get('context', {}).get(
                            'operation_id'
                        ) == request.operation_id)
                    ]
                    self.assertEqual(len(ownership), 1)
                finally:
                    SYNCWHEEL.ledger_io_checkpoint = original
                    fixture.close()


class RevisionProviderPackagingTest(unittest.TestCase):
    def test_wheel_contains_cli_and_revision_provider_modules(self):
        with tempfile.TemporaryDirectory(prefix='syncwheel-wheel-smoke-') as raw:
            root = Path(raw)
            source = root / 'source'
            wheelhouse = root / 'wheelhouse'
            source.mkdir()
            wheelhouse.mkdir()
            for name in ('pyproject.toml', 'README.md', 'LICENSE', 'VERSION'):
                shutil.copy2(REPO_ROOT / name, source / name)
            shutil.copytree(SCRIPTS, source / 'scripts')
            result = subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'pip',
                    'wheel',
                    '--no-deps',
                    '--no-build-isolation',
                    '--wheel-dir',
                    str(wheelhouse),
                    str(source),
                ],
                text=True,
                capture_output=True,
            )
            if result.returncode:
                self.fail(f'wheel build failed\n{result.stdout}\n{result.stderr}')
            wheel = next(wheelhouse.glob('syncwheel-*.whl'))
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
            self.assertIn('syncwheel.py', names)
            self.assertIn('syncwheel_revision_provider.py', names)


if __name__ == '__main__':
    unittest.main()
