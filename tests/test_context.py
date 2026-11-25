"""Live main metadata alone authorizes retained generations for fresh publication."""
import copy
from pathlib import Path
import tempfile
import unittest
import urllib.error
from publisher import protocol as p
from publisher.consumer import consume_publication
from publisher.context import publication_context, measure_online_context, validate_online_context
from fixtures import fixture, archive, row, encoded


class ContextReader:
    def __init__(self, live=None):
        self.live = live

    def locator(self, _=''):
        return None if self.live is None else copy.deepcopy(self.live['locator'])

    def json(self, url, maximum=65536):
        if self.live is None:
            raise urllib.error.HTTPError(url, 404, 'Absent', {}, None)
        return copy.deepcopy(self.live)


class ContextControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def publication(self, name):
        root = self.root / name
        root.mkdir()
        request, machine, main, assets = fixture(root)
        # Changing the producer-bound source yields a distinct valid payload identity.
        old = request['locator']['generationId']
        binding_path = f'g/{old}/publication-binding.json'
        binding = p.strict_json(assets[binding_path])
        binding['publicationId'] = 'pub-' + name
        receipt = p.strict_json(main['publication-receipt.json'])
        receipt['publicationId'] = binding['publicationId']
        main['publication-receipt.json'] = encoded(receipt)
        binding['publicationReceiptSha256'] = p.sha256(main['publication-receipt.json'])
        payload = {path.removeprefix(f'g/{old}/'): data for path, data in assets.items() if not path.endswith('/generation.json')}
        payload['publication-binding.json'] = encoded(binding)
        identity = p.generation_id({path: row(path, data) for path, data in payload.items()})
        locator = dict(request['locator'], generationId=identity, publicationId=binding['publicationId'])
        descriptor = dict(p.strict_json(assets[f'g/{old}/generation.json']), **locator,
                          payloadBytes=sum(len(data) for data in payload.values()))
        assets = {f'g/{identity}/{path}': data for path, data in payload.items()}
        assets[f'g/{identity}/generation.json'] = encoded(descriptor)
        return {'root': root, 'request': request, 'locator': locator, 'descriptor': descriptor, 'main': main, 'assets': assets}

    def consume(self, candidate, live=None, retained=(), previous=None, marker=True):
        locator = candidate['locator']
        main = dict(candidate['main'], **{'static-assets.json': encoded(locator)})
        if marker:
            main['static-publication-context.json'] = encoded({'schemaVersion': 'usct.static-publication-context/v1',
                'locator': locator, 'previousLocator': previous})
        assets = dict(candidate['assets'])
        for old in retained:
            assets.update(old['assets'])
        machine = encoded({'schemaVersion': 'usct.static-assets-export/v1', 'sourceCommitSha': 'c' * 40,
            'locator': locator, 'descriptor': candidate['descriptor'], 'main': [row(path, data) for path, data in sorted(main.items())],
            'assets': [row(path, data) for path, data in sorted(assets.items())], 'retention': [old['locator']['generationId'] for old in retained]})
        root = candidate['root']
        request = dict(candidate['request'], locator=locator, previousGenerationId=None if live is None else live['locator']['generationId'],
            machineManifestSha256=p.sha256(machine), mainArchiveSha256=archive(root / 'main.tar.gz', main),
            assetsArchiveSha256=archive(root / 'assets.tar.gz', assets))
        request['requestId'] = p.sha256(('usct.static-publication-request/v1\n' + p.canonical({k: v for k, v in request.items() if k != 'requestId'})).encode())
        result = consume_publication(request=request, machine_manifest=machine, main_archive=root / 'main.tar.gz',
            assets_archive=root / 'assets.tar.gz', output_dir=root / 'stage')
        return request, result

    def test_initial_requires_marker_and_both_actual_404(self):
        candidate = self.publication('new')
        request, result = self.consume(candidate)
        measured = measure_online_context(ContextReader(), request, result)
        self.assertIsNone(measured['publicationContext']['previousLocator'])
        validate_online_context(ContextReader(), request, measured)
        missing = self.publication('legacy')
        request, result = self.consume(missing, marker=False)
        with self.assertRaises((ValueError, OSError)):
            measure_online_context(ContextReader(), request, result)

    def test_A_to_B_and_same_B_rebuild_preserve_distinct_predecessor(self):
        a, b = self.publication('a'), self.publication('b')
        live_a = {'schemaVersion': 'usct.static-publication-context/v1', 'locator': a['locator'], 'previousLocator': None}
        request, result = self.consume(b, live_a, [a], a['locator'])
        measured = measure_online_context(ContextReader(live_a), request, result)
        validate_online_context(ContextReader(live_a), request, measured)
        live_b = measured['publicationContext']
        b['root'] = self.root / 'rebuild'; b['root'].mkdir()
        request, result = self.consume(b, live_b, [a], a['locator'])
        measured = measure_online_context(ContextReader(live_b), request, result)
        self.assertEqual(measured['publicationContext'], live_b)
        self.assertEqual(measured['retainedLocators'], {a['locator']['generationId']: a['locator']})
        b['root'] = self.root / 'lost-predecessor'; b['root'].mkdir()
        request, result = self.consume(b, live_b, [a], previous=None)
        with self.assertRaises(ValueError):
            measure_online_context(ContextReader(live_b), request, result)

    def test_current_and_distinct_previous_full_binding_required_archive_cannot_choose_Z(self):
        a, c, n, z = [self.publication(name) for name in ['a', 'c', 'n', 'z']]
        live = {'schemaVersion': 'usct.static-publication-context/v1', 'locator': c['locator'], 'previousLocator': a['locator']}
        request, result = self.consume(n, live, [c, z], c['locator'])
        with self.assertRaises((ValueError, OSError)):
            measure_online_context(ContextReader(live), request, result)
        n = self.publication('next')
        request, result = self.consume(n, live, [c, a], c['locator'])
        measured = measure_online_context(ContextReader(live), request, result)
        for saved in [a, c]:
            for field, value in [('acceptedDataTag', 'pl-119-73'), ('acceptedDataCommitSha', 'f' * 40), ('publicationId', 'pub-foreign')]:
                forged = copy.deepcopy(measured)
                forged['retainedLocators'][saved['locator']['generationId']][field] = value
                with self.subTest(generation=saved['locator']['generationId'], field=field), self.assertRaises(ValueError):
                    validate_online_context(ContextReader(live), request, forged)
        wrong = copy.deepcopy(measured)
        wrong['publicationContext']['previousLocator'] = z['locator']
        with self.assertRaises(ValueError):
            validate_online_context(ContextReader(live), request, wrong)
        bad = self.publication('bad-pointer')
        request, result = self.consume(bad, live, [c, a], z['locator'])
        with self.assertRaises(ValueError):
            measure_online_context(ContextReader(live), request, result)

    def test_live_context_missing_or_incoherent_is_never_initial(self):
        candidate = self.publication('candidate')
        request, result = self.consume(candidate)
        orphan = ContextReader({'schemaVersion': 'usct.static-publication-context/v1', 'locator': candidate['locator'], 'previousLocator': None})
        orphan.locator = lambda _: None
        with self.assertRaises(ValueError):
            measure_online_context(orphan, request, result)
        with self.assertRaises(ValueError):
            publication_context({'schemaVersion': 'usct.static-publication-context/v1', 'locator': candidate['locator'],
                                 'previousLocator': candidate['locator']}, candidate['locator'])

    def test_successful_null_or_error_context_is_not_initial_absence(self):
        candidate = self.publication('nullcase')
        request, result = self.consume(candidate)
        for response in [None, {}, {'error': 'unavailable'}]:
            reader = ContextReader()
            reader.json = lambda *_, value=response: value
            with self.subTest(response=response), self.assertRaises(ValueError):
                measure_online_context(reader, request, result)
        reader = ContextReader()
        def unavailable(*_):
            raise urllib.error.HTTPError('https://civic-source.github.io', 503, 'Unavailable', {}, None)
        reader.json = unavailable
        with self.assertRaises(urllib.error.HTTPError):
            measure_online_context(reader, request, result)
