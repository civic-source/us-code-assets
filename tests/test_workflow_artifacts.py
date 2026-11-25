"""Prepared artifact identities survive failed-only job reruns without collisions."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from publisher.cli import workflow_outputs, artifact_names


class WorkflowArtifactControls(unittest.TestCase):
    def prepared_outputs(self, attempt):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'GITHUB_RUN_ID': '101', 'GITHUB_RUN_ATTEMPT': str(attempt)}):
                workflow_outputs({'found': True, 'requestId': 'a' * 64})
            return dict(line.split('=', 1) for line in output.read_text().splitlines())

    def test_full_rerun_produces_unique_immutable_artifact_names(self):
        first = self.prepared_outputs(1)
        second = self.prepared_outputs(2)
        self.assertEqual(first['pagesArtifactName'], 'github-pages-101-1')
        self.assertEqual(first['proofArtifactName'], 'publication-proof-101-1')
        self.assertNotEqual(first['pagesArtifactName'], second['pagesArtifactName'])
        self.assertNotEqual(first['proofArtifactName'], second['proofArtifactName'])

    def test_failed_only_publish_rerun_consumes_original_prepared_output_names(self):
        workflow = Path('.github/workflows/publish-assets.yml').read_text()
        prepare, publish = workflow.split('  publish-assets:', 1)
        self.assertIn('pagesArtifactName: ${{ steps.prepare.outputs.pagesArtifactName }}', prepare)
        self.assertIn('proofArtifactName: ${{ steps.prepare.outputs.proofArtifactName }}', prepare)
        self.assertIn('name: ${{ steps.prepare.outputs.pagesArtifactName }}', prepare)
        self.assertIn('name: ${{ steps.prepare.outputs.proofArtifactName }}', prepare)
        self.assertIn('artifact_name: ${{ needs.prepare.outputs.pagesArtifactName }}', publish)
        self.assertIn('PROOF_ARTIFACT: ${{ needs.prepare.outputs.proofArtifactName }}', publish)
        self.assertIn('--name "$PROOF_ARTIFACT"', publish)
        self.assertNotIn('--name publication-proof', publish)
        self.assertNotIn('github.run_attempt', publish)

    def test_artifact_names_reject_untrusted_path_or_output_syntax(self):
        for value in ['0', '01', '-1', '1/../../foreign', '1\nname=foreign', '١']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                artifact_names('101', value)
