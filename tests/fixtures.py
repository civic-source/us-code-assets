"""Synthetic byte fixtures following the actual TypeScript generation contract."""
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
from publisher.protocol import canonical, generation_id


def encoded(value):
    return (canonical(value) + '\n').encode()


def row(path, data):
    return {'path': path, 'byteLength': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def archive(path, files, extra=None):
    with Path(path).open('wb') as destination:
        with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0, compresslevel=6) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.USTAR_FORMAT, encoding='ascii', errors='strict') as output:
                for name, data in sorted(files.items()):
                    item = tarfile.TarInfo(name)
                    item.size = len(data)
                    output.addfile(item, io.BytesIO(data))
                if extra:
                    output.addfile(*extra)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture(root, *, context=False):
    identity = {'acceptedDataCommitSha': 'a' * 40, 'acceptedDataTag': 'pl-119-142', 'publicationId': 'pub-pl-119-142-aaaaaaaaaaaa'}
    receipt = encoded(dict(identity, schemaVersion='usct.publication-receipt/v1', trackerSubmodulePinSha='a' * 40,
                           validationPassed=True, requiredScopesComplete=True))
    entry = encoded({'version': '1.5.2'})
    diff = encoded({'producerVersion': 'usct.diff-generator/v3', 'productLayout': 'source-path/v1', 'producerSha256': 'e' * 64, 'pairs': []})
    binding = dict(identity, schemaVersion='usct.static-assets-binding/v1', recipeVersion='usct.static-assets-generation/v1',
                   sourceCommitSha='c' * 40, publicationReceiptSha256=hashlib.sha256(receipt).hexdigest(), producer={
                       'pagefindVersion': '1.5.2', 'pagefindEntrySha256': hashlib.sha256(entry).hexdigest(),
                       'diffManifestSha256': hashlib.sha256(diff).hexdigest(), 'diffProducerVersion': 'usct.diff-generator/v3',
                       'diffProducerSha256': 'e' * 64, 'diffProductLayout': 'source-path/v1'})
    payload = {'pagefind/pagefind.js': b'export const init=async()=>{};', 'pagefind/pagefind-entry.json': entry,
               'diffs/manifest.json': diff, 'publication-binding.json': encoded(binding)}
    rows = {path: row(path, data) for path, data in payload.items()}
    generation = generation_id(rows)
    locator = dict(identity, schemaVersion='usct.static-assets/v1', generationId=generation)
    descriptor = dict(locator, sourceCommitSha='c' * 40, payloadBytes=sum(len(data) for data in payload.values()), payloadFileCount=len(payload))
    assets = {f'g/{generation}/{path}': data for path, data in payload.items()}
    assets[f'g/{generation}/generation.json'] = encoded(descriptor)
    main = {'publication-receipt.json': receipt, 'static-assets.json': encoded(locator), 'index.html': b'<h1>Reader</h1>'}
    if context:
        main['static-publication-context.json'] = encoded({'schemaVersion': 'usct.static-publication-context/v1',
            'locator': locator, 'previousLocator': None})
    machine = encoded({'schemaVersion': 'usct.static-assets-export/v1', 'sourceCommitSha': 'c' * 40, 'locator': locator,
                       'descriptor': descriptor, 'main': [row(path, data) for path, data in sorted(main.items())],
                       'assets': [row(path, data) for path, data in sorted(assets.items())], 'retention': []})
    request = {'schemaVersion': 'usct.static-publication-request/v1', 'sourceCommitSha': 'c' * 40, 'sourceRunId': 17,
               'sourceRunAttempt': 1, 'sourceJobName': 'prepare-publication', 'locator': locator, 'previousGenerationId': None,
               'machineManifestSha256': hashlib.sha256(machine).hexdigest(),
               'mainArchiveSha256': archive(root / 'main.tar.gz', main), 'assetsArchiveSha256': archive(root / 'assets.tar.gz', assets)}
    request['requestId'] = hashlib.sha256(('usct.static-publication-request/v1\n' + canonical(request)).encode()).hexdigest()
    return request, machine, main, assets
