"""Finite publisher commands; public requests cannot supply commands or URLs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from publisher import protocol as p
from publisher.consumer import read_bytes
from publisher.github import PublicReader, ASSETS, ASSET_BASE, NoRedirect, predecessor, source_proof, own_receipt_bytes, current_head
from publisher.pipeline import prepare


def proof_at(path, expected_hash):
    data = read_bytes(Path(path).parent, Path(path).name)
    if not p.digest(expected_hash) or p.sha256(data) != expected_hash:
        raise ValueError('Publisher proof artifact pin mismatch')
    proof = p.strict_json(data)
    p.exact(proof, {'schemaVersion', 'request', 'result', 'descriptor', 'bindingSha256', 'samples'})
    if proof['schemaVersion'] != 'usct.static-assets-proof/v1':
        raise ValueError('Unknown publisher proof schema')
    p.request(proof['request'])
    p.generation(proof['descriptor'], proof['request']['locator'])
    if not p.digest(proof['bindingSha256']):
        raise ValueError('Invalid publisher binding proof')
    return proof


def verify_https(proof):
    identity = proof['request']['locator']['generationId']
    base = ASSET_BASE + identity + '/'
    expected = (p.canonical(proof['descriptor']) + '\n').encode()
    opener = urllib.request.build_opener(NoRedirect())
    checks = [('generation.json', len(expected), p.sha256(expected)), ('publication-binding.json', 65536, proof['bindingSha256'])]
    for sample in proof['samples']:
        prefix = 'g/' + identity + '/'
        if not sample['path'].startswith(prefix):
            raise ValueError('Foreign HTTPS sample namespace')
        relative = sample['path'].removeprefix(prefix)
        p.safe_path(relative)
        if not p.integer(sample['byteLength'], 0, 16 * 1024 * 1024) or not p.digest(sample['sha256']):
            raise ValueError('Invalid bounded HTTPS sample')
        checks.append((relative, sample['byteLength'], sample['sha256']))
    for path, maximum, expected_hash in checks:
        request = urllib.request.Request(base + path + '?request=' + proof['request']['requestId'], headers={'Cache-Control': 'no-cache', 'User-Agent': 'civic-source-assets', 'Accept-Encoding': 'identity'})
        with opener.open(request, timeout=30) as response:
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise ValueError('Unexpected HTTPS readback status or encoding')
            digest = hashlib.sha256()
            total = 0
            while data := response.read(65536):
                total += len(data)
                if total > maximum:
                    raise ValueError('HTTPS readback exceeds its bound')
                digest.update(data)
            if digest.hexdigest() != expected_hash:
                raise ValueError('HTTPS generation/sample digest mismatch')
    return [path for path, _, _ in checks]


def own_gh(args):
    result = subprocess.run(['gh', *args, '--repo', ASSETS], check=True, capture_output=True, text=True, timeout=60)
    return result.stdout


def publish_receipt(proof, receipt_path):
    request = p.request(proof['request'])
    reader = PublicReader()
    current_head(reader, ASSETS, os.environ['GITHUB_SHA'])
    source_proof(reader, request)
    predecessor(reader, request)
    samples = verify_https(proof)
    current_head(reader, ASSETS, os.environ['GITHUB_SHA'])
    current_head(reader, 'civic-source/us-code-tracker', request['sourceCommitSha'])
    predecessor(reader, request)
    receipt = {'schemaVersion': 'usct.static-assets-ready/v1', 'requestId': request['requestId'],
               'locator': request['locator'], 'sourceCommitSha': request['sourceCommitSha'],
               'sourceRunId': request['sourceRunId'], 'sourceRunAttempt': request['sourceRunAttempt'],
               'publisherSourceCommitSha': os.environ['GITHUB_SHA'], 'publisherRunId': int(os.environ['GITHUB_RUN_ID']),
               'publisherRunAttempt': int(os.environ['GITHUB_RUN_ATTEMPT']), 'archiveScope': 'complete-main-and-assets-archive-closure',
               'publicScope': 'sampled-HTTPS-readback', 'verifiedPublicPaths': samples,
               'machineManifestSha256': request['machineManifestSha256'], 'mainArchiveSha256': request['mainArchiveSha256'], 'assetsArchiveSha256': request['assetsArchiveSha256']}
    target = Path(receipt_path)
    if target.name != 'receipt.json' or not p.digest(os.environ['GITHUB_SHA'], p.SHA40):
        raise ValueError('Invalid fixed receipt asset/publisher identity')
    with target.open('xb') as output:
        output.write((p.canonical(receipt) + '\n').encode())
    tag = 'static-assets-ready-' + request['requestId']
    own_gh(['release', 'create', tag, '--draft', '--target', os.environ['GITHUB_SHA'], '--title', tag, '--notes', 'Verified asset archive deployment; no statutory-data acceptance.', '--latest=false'])
    own_gh(['release', 'upload', tag, str(target)])
    own_gh(['release', 'edit', tag, '--draft=false'])
    release = reader.api(f'/repos/{ASSETS}/releases/tags/{tag}')
    if release.get('immutable') is not True or release.get('draft') is not False:
        raise ValueError('Published asset receipt is not immutable; main publication stays held')
    if own_receipt_bytes(request['requestId']) != (p.canonical(receipt) + '\n').encode():
        raise ValueError('Immutable receipt readback mismatch; main publication stays held')
    return receipt


def workflow_outputs(result):
    output = os.environ.get('GITHUB_OUTPUT')
    if not output:
        return
    lines = ['found=' + str(result['found']).lower()]
    for key in ['requestId', 'generationId', 'assetsDir', 'proofPath', 'proofSha256']:
        if key in result:
            value = str(result[key])
            if '\n' in value or '\r' in value:
                raise ValueError('Invalid workflow output')
            lines.append(key + '=' + value)
    with open(output, 'a', encoding='utf-8') as destination:
        destination.write('\n'.join(lines) + '\n')


def run_action(args):
    if args.action == 'prepare':
        if args.work_dir is None:
            raise ValueError('--work-dir is required')
        result = prepare(args.work_dir, args.request_id)
        workflow_outputs(result)
        return result
    if args.proof is None or args.proof_sha256 is None:
        raise ValueError('--proof and --proof-sha256 are required')
    proof = proof_at(args.proof, args.proof_sha256)
    if args.action == 'receipt':
        if args.receipt is None:
            raise ValueError('--receipt is required')
        return publish_receipt(proof, args.receipt)
    reader = PublicReader()
    source_proof(reader, proof['request'])
    predecessor(reader, proof['request'])
    current_head(reader, ASSETS, os.environ['GITHUB_SHA'])
    current_head(reader, 'civic-source/us-code-tracker', proof['request']['sourceCommitSha'])
    return {'state': 'predeploy-gates-verified', 'requestId': proof['request']['requestId']}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'predeploy', 'receipt'])
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--request-id')
    parser.add_argument('--proof', type=Path)
    parser.add_argument('--proof-sha256')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_action(args), sort_keys=True))
        return 0
    except Exception as cause:
        print(f'Publication held: {cause}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
