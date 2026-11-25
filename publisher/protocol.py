"""Versioned publication identities; no remote field is executable authority."""
import hashlib
import json
import re

MAX_PROJECT_BYTES = 900_000_000
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_FILES = 1_200_000
SHA256 = re.compile(r'[a-f0-9]{64}\Z')
SHA40 = re.compile(r'[a-f0-9]{40}\Z')
LOCATOR_KEYS = {'schemaVersion', 'generationId', 'acceptedDataCommitSha', 'acceptedDataTag', 'publicationId'}
REQUEST_KEYS = {'schemaVersion', 'requestId', 'sourceCommitSha', 'sourceRunId', 'sourceRunAttempt', 'sourceJobName', 'locator',
                'previousGenerationId', 'machineManifestSha256', 'mainArchiveSha256', 'assetsArchiveSha256'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def reject_float(_):
    raise ValueError('Noninteger JSON number')


def strict_json(data, maximum=65536):
    if len(data) > maximum:
        raise ValueError('JSON exceeds its byte bound')
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                      parse_float=reject_float, parse_constant=reject_float)


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('Unknown or missing protocol fields')


def integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def digest(value, pattern=SHA256):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def locator(value):
    exact(value, LOCATOR_KEYS)
    if (value['schemaVersion'] != 'usct.static-assets/v1' or not digest(value['generationId'])
            or not digest(value['acceptedDataCommitSha'], SHA40)
            or not isinstance(value['acceptedDataTag'], str)
            or re.fullmatch(r'pl-[1-9][0-9]*-[1-9][0-9]*(?:-rev[1-9][0-9]*)?', value['acceptedDataTag']) is None
            or len(value['acceptedDataTag']) > 128
            or not isinstance(value['publicationId'], str)
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value['publicationId']) is None):
        raise ValueError('Invalid publication locator')
    return value


def request(value):
    exact(value, REQUEST_KEYS)
    locator(value['locator'])
    if (value['schemaVersion'] != 'usct.static-publication-request/v1'
            or not digest(value['sourceCommitSha'], SHA40)
            or not integer(value['sourceRunId'], 1, 2 ** 53 - 1)
            or not integer(value['sourceRunAttempt'], 1, 2 ** 31 - 1)
            or value['sourceJobName'] != 'prepare-publication'
            or value['previousGenerationId'] is not None and not digest(value['previousGenerationId'])
            or any(not digest(value[key]) for key in ['requestId', 'machineManifestSha256', 'mainArchiveSha256', 'assetsArchiveSha256'])):
        raise ValueError('Invalid publication request identity')
    identity = {key: item for key, item in value.items() if key != 'requestId'}
    if value['requestId'] != sha256(('usct.static-publication-request/v1\n' + canonical(identity)).encode()):
        raise ValueError('Publication request hash mismatch')
    return value


def file_table(value):
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise ValueError('Invalid file table count')
    rows = {}
    total = 0
    for row in value:
        exact(row, {'path', 'byteLength', 'sha256'})
        path = safe_path(row['path'])
        if path in rows or not integer(row['byteLength'], 0, MAX_PROJECT_BYTES) or not digest(row['sha256']):
            raise ValueError('Invalid or duplicate file-table entry')
        rows[path] = row
        total += row['byteLength']
        if total > MAX_PROJECT_BYTES:
            raise ValueError('Complete project exceeds 900000000 bytes')
    return rows


def safe_path(value):
    if (not isinstance(value, str) or len(value) > 512 or len(value.split('/')) > 32
            or any(part in {'', '.', '..'} or re.fullmatch(r'[A-Za-z0-9_@.+-]{1,255}', part) is None
                   for part in value.split('/'))):
        raise ValueError('Noncanonical archive path')
    return value


def generation_id(rows):
    table = [dict(path=path, byteLength=row['byteLength'], sha256=row['sha256']) for path, row in sorted(rows.items())]
    return sha256(('usct.static-assets-generation/v1\n' + canonical(table)).encode())


def generation(value, expected):
    exact(value, LOCATOR_KEYS | {'payloadFileCount', 'payloadBytes', 'sourceCommitSha'})
    locator({key: value[key] for key in LOCATOR_KEYS})
    if (any(value[key] != expected[key] for key in LOCATOR_KEYS)
            or not integer(value['payloadFileCount'], 1, 300000)
            or not integer(value['payloadBytes'], 1, MAX_PROJECT_BYTES)
            or not digest(value['sourceCommitSha'], SHA40)):
        raise ValueError('Invalid generation descriptor')
    return value
