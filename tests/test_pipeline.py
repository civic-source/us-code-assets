"""Public pipeline/receipt lifecycle tests mock network boundaries only."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from publisher import protocol as p
from publisher import pipeline, cli
from publisher.github import TRACKER, ASSETS
from fixtures import fixture
from test_github import Reader


class PipelineControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request, self.machine, _, _ = fixture(self.root, context=True)
        self.raw = {'request.json': (p.canonical(self.request) + '\n').encode(), 'publication.json': self.machine,
                    'main.tar.gz': (self.root / 'main.tar.gz').read_bytes(), 'assets.tar.gz': (self.root / 'assets.tar.gz').read_bytes()}
        self.reader = Reader(self.request)
        self.reader.values[f'/repos/{TRACKER}/releases/tags/static-assets-request-{self.request["requestId"]}'] = {
            'tag_name': 'static-assets-request-' + self.request['requestId'], 'draft': False, 'immutable': True,
            'assets': [{'name': name, 'size': len(data)} for name, data in self.raw.items()]}
        self.reader.values[f'/repos/{TRACKER}/git/ref/tags/static-assets-request-{self.request["requestId"]}'] = {
            'object': {'type': 'commit', 'sha': self.request['sourceCommitSha']}}

    def download(self, identity, name, maximum, destination):
        self.assertEqual(identity, self.request['requestId'])
        self.assertEqual(maximum, len(self.raw[name]))
        destination.write_bytes(self.raw[name])

    def prepare(self):
        with patch('publisher.pipeline.receipt_exists', return_value=False), patch('publisher.pipeline.download_asset', side_effect=self.download):
            return pipeline.prepare(self.root / 'download', self.request['requestId'], self.reader)

    def test_whole_pipeline_downloads_both_archives_and_pins_small_proof(self):
        result = self.prepare()
        self.assertTrue(result['found'])
        proof = cli.proof_at(result['proofPath'], result['proofSha256'])
        self.assertEqual(proof['result']['scope'], 'complete-main-and-assets-archive-closure')
        self.assertEqual(proof['request'], self.request)
        self.assertEqual(proof['result']['publicationContext']['locator'], self.request['locator'])
        self.assertEqual(proof['result']['retainedLocators'], {})
        self.assertLess((self.root / 'download/proof.json').stat().st_size, 65536)
        with self.assertRaises(ValueError):
            cli.proof_at(result['proofPath'], 'f' * 64)

    def test_source_binding_failure_refuses_before_large_archive_download(self):
        self.reader.values[f'/repos/{TRACKER}/actions/runs/17']['run_attempt'] = 2
        with patch('publisher.pipeline.receipt_exists', return_value=False), patch('publisher.pipeline.download_asset', side_effect=self.download) as download:
            with self.assertRaises(ValueError):
                pipeline.prepare(self.root / 'download', self.request['requestId'], self.reader)
        self.assertEqual([call.args[1] for call in download.call_args_list], ['request.json'])

    def test_receipt_is_separate_immutable_own_release_after_https_and_full_readback(self):
        result = self.prepare()
        proof = cli.proof_at(result['proofPath'], result['proofSha256'])
        tag = 'static-assets-ready-' + self.request['requestId']
        self.reader.values[f'/repos/{ASSETS}/releases/tags/{tag}'] = {'draft': False, 'immutable': True}
        target = self.root / 'receipt.json'
        environment = {'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '101', 'GITHUB_RUN_ATTEMPT': '1'}
        with patch.dict(os.environ, environment), patch('publisher.cli.PublicReader', return_value=self.reader), \
                patch('publisher.cli.verify_https', return_value=['generation.json', 'publication-binding.json']), \
                patch('publisher.cli.own_gh') as gh, patch('publisher.cli.own_receipt_bytes', side_effect=lambda _: target.read_bytes()):
            receipt = cli.publish_receipt(proof, target)
        self.assertEqual(receipt['mainArchiveSha256'], self.request['mainArchiveSha256'])
        self.assertEqual(receipt['archiveScope'], 'complete-main-and-assets-archive-closure')
        self.assertEqual(receipt['publicScope'], 'sampled-HTTPS-readback')
        self.assertEqual(gh.call_args_list[0].args[0][:3], ['release', 'create', tag])
        self.assertIn('--draft', gh.call_args_list[0].args[0])
        self.assertEqual(gh.call_args_list[-1].args[0], ['release', 'edit', tag, '--draft=false'])
        self.assertNotIn('deploymentId', receipt)

    def test_https_failure_never_creates_receipt_release(self):
        result = self.prepare()
        proof = cli.proof_at(result['proofPath'], result['proofSha256'])
        with patch.dict(os.environ, {'GITHUB_SHA': 'b' * 40}), patch('publisher.cli.PublicReader', return_value=self.reader), \
                patch('publisher.cli.verify_https', side_effect=ValueError('corrupt HTTPS bytes')), patch('publisher.cli.own_gh') as gh:
            with self.assertRaises(ValueError):
                cli.publish_receipt(proof, self.root / 'receipt.json')
        gh.assert_not_called()

    def test_mutable_ready_release_is_not_claimed_as_accepted(self):
        self.reader.values[f'/repos/{ASSETS}/releases/tags/static-assets-ready-{self.request["requestId"]}'] = {'draft': False, 'immutable': False}
        with self.assertRaises(ValueError):
            pipeline.receipt_exists(self.reader, self.request['requestId'])

    def test_request_unknown_fields_and_duplicates_do_not_gain_authority(self):
        raw = dict(self.request, unexpected='never accepted')
        with self.assertRaises(ValueError):
            p.request(raw)
        with self.assertRaises(ValueError):
            p.strict_json(b'{"sourceRunAttempt":1,"sourceRunAttempt":2}')

    def test_predeploy_rechecks_live_context_and_strict_internal_proof_evidence(self):
        result = self.prepare()
        args = SimpleNamespace(action='predeploy', proof=Path(result['proofPath']), proof_sha256=result['proofSha256'])
        environment = {'GITHUB_SHA': 'b' * 40}
        with patch.dict(os.environ, environment), patch('publisher.cli.PublicReader', return_value=self.reader):
            self.assertEqual(cli.run_action(args)['state'], 'predeploy-gates-verified')
            self.reader.context = {'schemaVersion': 'usct.static-publication-context/v1',
                'locator': self.request['locator'], 'previousLocator': None}
            with self.assertRaises(ValueError):
                cli.run_action(args)
            self.reader.context = None
            forged = p.strict_json(args.proof.read_bytes())
            forged['result']['retainedLocators'] = []
            args.proof = self.root / 'forged-proof.json'
            args.proof.write_bytes((p.canonical(forged) + '\n').encode())
            args.proof_sha256 = p.sha256(args.proof.read_bytes())
            with self.assertRaises(ValueError):
                cli.run_action(args)
