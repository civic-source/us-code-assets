"""Fresh main-writer context authorizes online retention, never archived pointers."""
import os
from pathlib import Path
import urllib.error
from publisher import protocol as p
from publisher import safeio
from publisher.consumer import read_bytes
from publisher.github import predecessor

MAIN_CONTEXT = 'https://civic-source.github.io/us-code-tracker/static-publication-context.json'


def publication_context(value, expected_locator):
    p.exact(value, {'schemaVersion', 'locator', 'previousLocator'})
    if value['schemaVersion'] != 'usct.static-publication-context/v1':
        raise ValueError('Unknown main publication context schema')
    if p.locator(value['locator']) != p.locator(expected_locator):
        raise ValueError('Main publication context does not match the locator')
    previous = value['previousLocator']
    if previous is not None:
        p.locator(previous)
        if previous['generationId'] == value['locator']['generationId']:
            raise ValueError('Main publication context cannot retain itself')
    return value


def live_context(reader, request):
    current = predecessor(reader, request)
    absent = False
    try:
        value = reader.json(MAIN_CONTEXT + '?request=' + request['requestId'], 65536)
    except urllib.error.HTTPError as cause:
        if cause.code != 404:
            raise
        if current is not None:
            raise ValueError('Live main locator lacks its publication context') from cause
        value = None
        absent = True
    if not absent:
        if current is None:
            raise ValueError('Main context exists without a matching live locator')
        publication_context(value, current)
    if reader.locator(request['requestId']) != current:
        raise ValueError('Main publication changed while reading its context')
    return value


def expected_context(request, live):
    candidate = request['locator']
    previous = None
    if live is not None:
        if candidate['generationId'] == live['locator']['generationId']:
            if candidate != live['locator']:
                raise ValueError('Same-generation main publication binding differs')
            previous = live['previousLocator']
        else:
            previous = live['locator']
    return {'schemaVersion': 'usct.static-publication-context/v1', 'locator': candidate, 'previousLocator': previous}


def expected_retention(request, live):
    if live is None:
        return {}
    candidates = [live['locator'], live['previousLocator']]
    return {value['generationId']: value for value in candidates if value is not None
            and value['generationId'] != request['locator']['generationId']}


def validate_evidence(request, result, live):
    if not isinstance(result, dict):
        raise ValueError('Invalid internal publisher result')
    descriptor = p.generation(result.get('descriptor'), request['locator'])
    if descriptor['sourceCommitSha'] != request['sourceCommitSha']:
        raise ValueError('Internal descriptor source differs from the request')
    context = publication_context(result.get('publicationContext'), request['locator'])
    if context != expected_context(request, live):
        raise ValueError('Candidate main context does not preserve the live predecessor')
    expected = expected_retention(request, live)
    retained = result.get('retainedLocators')
    p.exact(retained, set(expected))
    for identity, locator in retained.items():
        if p.locator(locator) != expected[identity]:
            raise ValueError('Retained generation full publication binding differs from live main')
    if result.get('generationId') != request['locator']['generationId'] or result.get('requestId') != request['requestId']:
        raise ValueError('Internal publisher result differs from the request')
    return result


def validate_online_context(reader, request, result):
    p.request(request)
    return validate_evidence(request, result, live_context(reader, request))


def measure_online_context(reader, request, result):
    p.request(request)
    live = live_context(reader, request)
    assets = Path(result['assetsDir'])
    context = p.strict_json(read_bytes(assets.parent / 'main', 'static-publication-context.json'))
    expected = expected_retention(request, live)
    descriptor = safeio.directory(assets / 'g')
    try:
        if set(os.listdir(descriptor)) != {request['locator']['generationId'], *expected}:
            raise ValueError('Archive retention differs from live main current and predecessor')
    finally:
        os.close(descriptor)
    retained = {}
    for identity, locator in expected.items():
        value = p.strict_json(read_bytes(assets / 'g' / identity, 'generation.json'))
        p.generation(value, locator)
        retained[identity] = {key: value[key] for key in p.LOCATOR_KEYS}
    measured = dict(result, publicationContext=context, retainedLocators=retained)
    validate_evidence(request, measured, live)
    if live_context(reader, request) != live:
        raise ValueError('Main publication context changed while measuring the archive')
    return measured
