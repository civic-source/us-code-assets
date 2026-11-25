"""Poll immutable public requests and prepare only authenticated bounded artifacts."""
import os
from pathlib import Path
import urllib.error
from publisher import protocol as p
from publisher import safeio
from publisher.context import live_context, measure_online_context
from publisher.consumer import consume_publication, read_bytes
from publisher.github import PublicReader, TRACKER, ASSETS, release, download_asset, source_proof

LIMITS = {'request.json': 65536, 'publication.json': p.MAX_MANIFEST_BYTES, 'main.tar.gz': 1_000_000_000, 'assets.tar.gz': 1_000_000_000}


def request_asset_sizes(value):
    result = {}
    for asset in value['assets']:
        name = asset.get('name')
        size = asset.get('size')
        if name not in LIMITS or not p.integer(size, 1, LIMITS[name]):
            raise ValueError('Release asset size exceeds its declared protocol bound')
        result[name] = size
    return result


def receipt_exists(reader, request_id):
    try:
        value = reader.api(f'/repos/{ASSETS}/releases/tags/static-assets-ready-{request_id}')
    except urllib.error.HTTPError as cause:
        if cause.code == 404:
            return False
        raise
    if value.get('draft') is not False or value.get('immutable') is not True:
        raise ValueError('Existing publisher receipt is incomplete or mutable; reconcile before retry')
    return True


def discover(reader):
    values = reader.api(f'/repos/{TRACKER}/releases?per_page=30')
    if not isinstance(values, list) or len(values) > 30:
        raise ValueError('Unexpected release discovery response')
    for value in values:
        tag = value.get('tag_name', '')
        if not tag.startswith('static-assets-request-'):
            continue
        request_id = tag.removeprefix('static-assets-request-')
        if p.digest(request_id) and not receipt_exists(reader, request_id):
            return request_id
    return None


def load_request(reader, request_id, directory):
    published = release(reader, request_id)
    sizes = request_asset_sizes(published)
    download_asset(request_id, 'request.json', sizes['request.json'], directory / 'request.json')
    request = p.request(p.strict_json(read_bytes(directory, 'request.json')))
    if request['requestId'] != request_id:
        raise ValueError('Release tag and request ID disagree')
    ref = reader.api(f'/repos/{TRACKER}/git/ref/tags/static-assets-request-{request_id}')
    if ref.get('object', {}).get('type') != 'commit' or ref.get('object', {}).get('sha') != request['sourceCommitSha']:
        raise ValueError('Immutable release tag does not pin the protected source SHA')
    source_proof(reader, request)
    live_context(reader, request)
    return request, sizes


def prepare(work_dir, request_id=None, reader=None):
    reader = reader or PublicReader()
    selected = request_id or discover(reader)
    if selected is None:
        return {'state': 'idle', 'found': False}
    if not p.digest(selected):
        raise ValueError('Invalid request selector')
    if receipt_exists(reader, selected):
        return {'state': 'already-receipted', 'found': False, 'requestId': selected}
    safeio.fresh_directory(work_dir)
    root = Path(work_dir)
    request, sizes = load_request(reader, selected, root)
    for name in ['publication.json', 'main.tar.gz', 'assets.tar.gz']:
        download_asset(selected, name, sizes[name], root / name)
    machine = read_bytes(root, 'publication.json', p.MAX_MANIFEST_BYTES)
    result = consume_publication(request=request, machine_manifest=machine,
                                 main_archive=root / 'main.tar.gz', assets_archive=root / 'assets.tar.gz', output_dir=root / 'stage')
    result = measure_online_context(reader, request, result)
    proof = {'schemaVersion': 'usct.static-assets-proof/v1', 'request': request, 'result': result,
             'descriptor': result['descriptor'], 'bindingSha256': result['bindingSha256'], 'samples': result['samples']}
    proof_bytes = (p.canonical(proof) + '\n').encode()
    fd = safeio.directory(root)
    try:
        output = os.open('proof.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        try:
            safeio.write_all(output, proof_bytes, fd)
            os.fsync(output)
        finally:
            os.close(output)
        os.fsync(fd)
    finally:
        os.close(fd)
    return dict(result, found=True, state='prepared', proofPath=str(root / 'proof.json'), proofSha256=p.sha256(proof_bytes))
