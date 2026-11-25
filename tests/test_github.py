"""Protected-run proof binds an exact request, not its self-asserted run ID."""
import copy
import tempfile
from pathlib import Path
import unittest
import urllib.error
from publisher.github import source_proof, predecessor, TRACKER, ASSETS
from fixtures import fixture


class Reader:
    def __init__(self, request):
        sha = request['sourceCommitSha']
        prefix = '/repos/' + TRACKER
        self.values = {
            prefix + '/git/ref/heads/main': {'object': {'type': 'commit', 'sha': sha}},
            '/repos/' + ASSETS + '/git/ref/heads/main': {'object': {'type': 'commit', 'sha': 'b' * 40}},
            prefix + '/actions/runs/17': {'id': 17, 'run_attempt': 1, 'head_sha': sha, 'head_branch': 'main',
                                         'head_repository': {'full_name': TRACKER}, 'path': '.github/workflows/deploy-site.yml',
                                         'event': 'push', 'status': 'in_progress'},
            prefix + '/actions/runs/17/attempts/1/jobs?per_page=100': {'total_count': 1, 'jobs': [
                {'name': 'prepare-publication', 'head_sha': sha, 'status': 'completed', 'conclusion': 'success', 'steps': [
                    {'name': 'Bind publication request ' + request['requestId'], 'status': 'completed', 'conclusion': 'success'},
                    {'name': 'Post Checkout', 'status': 'completed', 'conclusion': 'success'},
                    {'name': 'Complete job', 'status': 'completed', 'conclusion': 'success'}]}]},
            prefix + f'/commits/{sha}/check-runs?check_name=build&filter=latest&per_page=100': {'total_count': 1, 'check_runs': [
                {'name': 'build', 'head_sha': sha, 'status': 'completed', 'conclusion': 'success', 'app': {'slug': 'github-actions'},
                 'details_url': f'https://github.com/{TRACKER}/actions/runs/42/job/43'}]},
            prefix + '/actions/runs/42': {'path': '.github/workflows/ci.yml', 'head_sha': sha, 'head_branch': 'main',
                                         'event': 'push', 'status': 'completed', 'conclusion': 'success'},
        }
        self.current = None
        self.context = None

    def api(self, path):
        return self.values[path]

    def json(self, url, maximum=65536):
        if self.context is None:
            raise urllib.error.HTTPError(url, 404, 'Absent', {}, None)
        return copy.deepcopy(self.context)

    def locator(self, request_id):
        return self.current


class SourceProofControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.request = fixture(Path(self.temp.name))[0]
        self.reader = Reader(self.request)

    def test_accepts_completed_prepare_job_while_overall_main_workflow_waits(self):
        source_proof(self.reader, self.request)

    def test_rejects_wrong_attempt_fork_workflow_branch_or_failed_ci(self):
        for field, value in [('run_attempt', 2), ('head_branch', 'foreign'), ('path', '.github/workflows/other.yml'),
                             ('head_repository', {'full_name': 'foreign/repo'})]:
            with self.subTest(field=field):
                reader = copy.deepcopy(self.reader)
                reader.values['/repos/' + TRACKER + '/actions/runs/17'][field] = value
                with self.assertRaises(ValueError):
                    source_proof(reader, self.request)
        self.reader.values['/repos/' + TRACKER + '/actions/runs/42']['conclusion'] = 'failure'
        with self.assertRaises(ValueError):
            source_proof(self.reader, self.request)

    def test_refuses_forged_stamp_and_unknown_authored_step_after_stamp(self):
        jobs = self.reader.values['/repos/' + TRACKER + '/actions/runs/17/attempts/1/jobs?per_page=100']['jobs']
        original = copy.deepcopy(jobs[0]['steps'])
        for steps in [original[1:], original + [{'name': 'Publish changed bytes', 'status': 'completed', 'conclusion': 'success'}],
                      [dict(original[0], name='Bind publication request ' + 'f' * 64), *original[1:]]]:
            jobs[0]['steps'] = steps
            with self.assertRaises(ValueError):
                source_proof(self.reader, self.request)

    def test_refuses_changed_predecessor_and_does_not_treat_error_as_initial_404(self):
        predecessor(self.reader, self.request)
        self.reader.current = dict(self.request['locator'], generationId='f' * 64)
        with self.assertRaises(ValueError):
            predecessor(self.reader, self.request)


    def test_disambiguates_ci_build_from_a_different_owned_workflow_named_build(self):
        key = '/repos/' + TRACKER + f'/commits/{self.request["sourceCommitSha"]}/check-runs?check_name=build&filter=latest&per_page=100'
        extra = copy.deepcopy(self.reader.values[key]['check_runs'][0])
        extra['details_url'] = f'https://github.com/{TRACKER}/actions/runs/44/job/45'
        self.reader.values[key]['check_runs'].append(extra)
        self.reader.values[key]['total_count'] = 2
        self.reader.values['/repos/' + TRACKER + '/actions/runs/44'] = dict(self.reader.values['/repos/' + TRACKER + '/actions/runs/42'], path='.github/workflows/deploy-site.yml')
        source_proof(self.reader, self.request)


    def test_refuses_a_previously_successful_request_after_main_has_advanced(self):
        self.reader.values['/repos/' + TRACKER + '/git/ref/heads/main']['object']['sha'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'Current protected main'):
            source_proof(self.reader, self.request)
