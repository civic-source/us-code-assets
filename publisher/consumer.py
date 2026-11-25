"""Complete offline archive verification; this never selects a live publication."""
import os
from pathlib import Path
from publisher import protocol as p
from publisher import safeio

MACHINE_KEYS = {'schemaVersion', 'sourceCommitSha', 'locator', 'descriptor', 'main', 'assets', 'retention'}
BINDING_KEYS = {'schemaVersion', 'recipeVersion', 'sourceCommitSha', 'acceptedDataCommitSha', 'acceptedDataTag',
                'publicationId', 'publicationReceiptSha256', 'producer'}
PRODUCER_KEYS = {'pagefindVersion', 'pagefindEntrySha256', 'diffManifestSha256', 'diffProducerVersion', 'diffProducerSha256', 'diffProductLayout'}


def read_bytes(root, path, maximum=65536):
    p.safe_path(path)
    fd = safeio.regular_file(Path(root) / path)
    try:
        before = os.fstat(fd)
        if before.st_size > maximum:
            raise ValueError('Metadata exceeds its byte bound')
        chunks = []
        total = 0
        while data := os.read(fd, min(65536, maximum + 1 - total)):
            chunks.append(data)
            total += len(data)
            if total > maximum:
                raise ValueError('Metadata grew beyond its bound')
        if safeio.identity(before) != safeio.identity(os.fstat(fd)):
            raise ValueError('Metadata identity changed')
        return b''.join(chunks)
    finally:
        os.close(fd)


def metadata(root, path, maximum=65536):
    return p.strict_json(read_bytes(root, path, maximum), maximum)


def parse_machine(raw, request):
    if p.sha256(raw) != request['machineManifestSha256']:
        raise ValueError('Machine manifest hash mismatch')
    value = p.strict_json(raw, p.MAX_MANIFEST_BYTES)
    p.exact(value, MACHINE_KEYS)
    if (value['schemaVersion'] != 'usct.static-assets-export/v1'
            or value['sourceCommitSha'] != request['sourceCommitSha'] or value['locator'] != request['locator']):
        raise ValueError('Machine publication binding mismatch')
    p.generation(value['descriptor'], request['locator'])
    if value['descriptor']['sourceCommitSha'] != value['sourceCommitSha']:
        raise ValueError('Machine descriptor source mismatch')
    retained = value['retention']
    if (not isinstance(retained, list) or len(retained) > 2 or any(not p.digest(item) for item in retained)
            or len(set(retained)) != len(retained) or request['locator']['generationId'] in retained):
        raise ValueError('Invalid retained generation chain')
    previous = request['previousGenerationId']
    if previous is not None and previous != request['locator']['generationId'] and previous not in retained:
        raise ValueError('Current main generation was not retained')
    return value, p.file_table(value['main']), p.file_table(value['assets'])


def generation_tables(table, expected_ids):
    result = {identity: {} for identity in expected_ids}
    for path, row in table.items():
        parts = path.split('/')
        if len(parts) < 3 or parts[0] != 'g' or parts[1] not in result:
            raise ValueError('Unexpected asset project namespace')
        relative = '/'.join(parts[2:])
        if not (relative.startswith(('pagefind/', 'diffs/')) or relative in {'generation.json', 'publication-binding.json'}):
            raise ValueError('Unexpected generation payload path')
        result[parts[1]][relative] = dict(row, path=relative)
    return result


def verify_producer(root, binding):
    producer = binding['producer']
    p.exact(producer, PRODUCER_KEYS)
    if any(not p.digest(producer[key]) for key in ['pagefindEntrySha256', 'diffManifestSha256', 'diffProducerSha256']):
        raise ValueError('Invalid producer digests')
    entry = read_bytes(root, 'pagefind/pagefind-entry.json')
    manifest = read_bytes(root, 'diffs/manifest.json', 16 * 1024 * 1024)
    if p.sha256(entry) != producer['pagefindEntrySha256'] or p.sha256(manifest) != producer['diffManifestSha256']:
        raise ValueError('Producer source metadata hash mismatch')
    parsed = p.strict_json(manifest, 16 * 1024 * 1024)
    if (p.strict_json(entry)['version'] != producer['pagefindVersion']
            or producer['diffProducerVersion'] != 'usct.diff-generator/v3' or producer['diffProductLayout'] != 'source-path/v1'
            or parsed['producerVersion'] != producer['diffProducerVersion'] or parsed['producerSha256'] != producer['diffProducerSha256']
            or parsed['productLayout'] != producer['diffProductLayout']):
        raise ValueError('Producer identity mismatch')


def verify_generation(root, identity, table):
    raw_descriptor = metadata(root, 'generation.json')
    expected = {key: raw_descriptor[key] for key in p.LOCATOR_KEYS}
    expected['generationId'] = identity
    descriptor = p.generation(raw_descriptor, expected)
    payload = {path: row for path, row in table.items() if path != 'generation.json'}
    if (p.generation_id(payload) != identity or descriptor['payloadFileCount'] != len(payload)
            or descriptor['payloadBytes'] != sum(row['byteLength'] for row in payload.values())
            or read_bytes(root, 'generation.json') != (p.canonical(descriptor) + '\n').encode()):
        raise ValueError('Generation payload closure mismatch')
    binding = metadata(root, 'publication-binding.json')
    p.exact(binding, BINDING_KEYS)
    if (binding['schemaVersion'] != 'usct.static-assets-binding/v1' or binding['recipeVersion'] != 'usct.static-assets-generation/v1'
            or any(binding[key] != descriptor[key] for key in ['sourceCommitSha', 'acceptedDataCommitSha', 'acceptedDataTag', 'publicationId'])
            or not p.digest(binding['publicationReceiptSha256'])):
        raise ValueError('Generation publication binding mismatch')
    if 'pagefind/pagefind.js' not in table:
        raise ValueError('Missing Pagefind runtime')
    verify_producer(root, binding)
    return descriptor, binding


def preflight(parent, tables, manifest_size):
    fd = safeio.directory(parent)
    try:
        unit = os.fstatvfs(fd).f_frsize or 4096
        files = [row for table in tables for row in table.values()]
        dirs = set()
        for lane, table in enumerate(tables):
            for path in table:
                parts = path.split('/')[:-1]
                while parts:
                    dirs.add((lane, '/'.join(parts)))
                    parts.pop()
        allocated = sum(((row['byteLength'] + unit - 1) // unit) * unit for row in files)
        safeio.capacity(fd, allocated + (len(dirs) + 8) * unit + manifest_size + 65536,
                        len(files) + len(dirs) + 8)
    finally:
        os.close(fd)


def verify_materialized(stage, request, machine, main, assets):
    if metadata(stage / 'main', 'static-assets.json') != request['locator']:
        raise ValueError('Main locator mismatch')
    groups = generation_tables(assets, [request['locator']['generationId'], *machine['retention']])
    current_binding = None
    for identity, table in groups.items():
        descriptor, binding = verify_generation(stage / 'assets' / 'g' / identity, identity, table)
        if identity == request['locator']['generationId']:
            if descriptor != machine['descriptor']:
                raise ValueError('Current machine descriptor mismatch')
            current_binding = binding
    receipt_bytes = read_bytes(stage / 'main', 'publication-receipt.json')
    receipt = p.strict_json(receipt_bytes)
    expected = request['locator']
    if (current_binding is None or p.sha256(receipt_bytes) != current_binding['publicationReceiptSha256']
            or any(receipt.get(key) != expected[key] for key in ['acceptedDataCommitSha', 'acceptedDataTag', 'publicationId'])
            or receipt.get('trackerSubmodulePinSha') != expected['acceptedDataCommitSha']
            or receipt.get('validationPassed') is not True or receipt.get('requiredScopesComplete') is not True):
        raise ValueError('Main accepted-publication receipt mismatch')


def consume_publication(*, request, machine_manifest, main_archive, assets_archive, output_dir):
    request = p.request(request)
    machine, main, assets = parse_machine(machine_manifest, request)
    stage = Path(os.path.abspath(output_dir))
    preflight(stage.parent, [main, assets], len(machine_manifest))
    safeio.fresh_directory(stage)
    try:
        for name in ['main', 'assets']:
            safeio.fresh_directory(stage / name, mode=0o755)
        safeio.extract_ustar(main_archive, stage / 'main', main, request['mainArchiveSha256'])
        safeio.extract_ustar(assets_archive, stage / 'assets', assets, request['assetsArchiveSha256'])
        safeio.verify_tree(stage / 'main', main)
        safeio.verify_tree(stage / 'assets', assets)
        verify_materialized(stage, request, machine, main, assets)
        prefix = 'g/' + request['locator']['generationId'] + '/'
        sample_paths = [prefix + 'pagefind/pagefind.js']
        diffs = [path for path in sorted(assets) if path.startswith(prefix + 'diffs/') and path.endswith('.json.gz') and assets[path]['byteLength'] <= 16 * 1024 * 1024]
        if diffs:
            sample_paths.append(diffs[0])
        return {'requestId': request['requestId'], 'generationId': request['locator']['generationId'],
                'assetsDir': str(stage / 'assets'), 'mainBytes': sum(row['byteLength'] for row in main.values()),
                'assetsBytes': sum(row['byteLength'] for row in assets.values()), 'scope': 'complete-main-and-assets-archive-closure',
                'descriptor': machine['descriptor'], 'bindingSha256': assets[prefix + 'publication-binding.json']['sha256'],
                'samples': [assets[path] for path in sample_paths]}
    except BaseException as cause:
        raise ValueError(f'Incomplete publication stage retained at {stage}: {cause}') from cause
