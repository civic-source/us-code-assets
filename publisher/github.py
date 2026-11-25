"""Bounded public GitHub reads; all write authority stays in the owning repository."""
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from publisher import protocol as p

TRACKER = 'civic-source/us-code-tracker'
ASSETS = 'civic-source/us-code-assets'
API = 'https://api.github.com'
MAIN_LOCATOR = 'https://civic-source.github.io/us-code-tracker/static-assets.json'
ASSET_BASE = 'https://civic-source.github.io/us-code-assets/g/'
EPILOGUES = {'Post Checkout', 'Post Setup pnpm', 'Post Setup Node.js', 'Post Restore diff cache',
             'Post Restore Turborepo cache', 'Complete job'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected metadata redirect')


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'} or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError('Untrusted release asset redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicReader:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())

    def api(self, path):
        if not path.startswith(('/repos/' + TRACKER + '/', '/repos/' + ASSETS + '/')):
            raise ValueError('Foreign API repository')
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'civic-source-assets', 'Accept-Encoding': 'identity', 'X-GitHub-Api-Version': '2022-11-28'}
        token = os.environ.get('GH_TOKEN')
        if token:
            headers['Authorization'] = 'Bearer ' + token
        return self.json(API + path, 4 * 1024 * 1024, headers)

    def json(self, url, maximum=65536, headers=None):
        request = urllib.request.Request(url, headers=headers or {'User-Agent': 'civic-source-assets', 'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
        with self.opener.open(request, timeout=30) as response:
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise ValueError('Unexpected metadata status or encoding')
            data = response.read(maximum + 1)
            return p.strict_json(data, maximum)

    def locator(self, request_id=''):
        if request_id and not p.digest(request_id):
            raise ValueError('Invalid locator request identity')
        try:
            return p.locator(self.json(MAIN_LOCATOR + ('?request=' + request_id if request_id else '')))
        except urllib.error.HTTPError as cause:
            if cause.code == 404:
                return None
            raise


def successful_job(job):
    return job.get('status') == 'completed' and job.get('conclusion') == 'success'


def current_head(reader, repository, expected_sha):
    if repository not in {TRACKER, ASSETS} or not p.digest(expected_sha, p.SHA40):
        raise ValueError('Invalid protected main identity')
    value = reader.api(f'/repos/{repository}/git/ref/heads/main').get('object', {})
    if value.get('type') != 'commit' or value.get('sha') != expected_sha:
        raise ValueError('Current protected main HEAD changed; publication must be rebuilt/reconciled')


def source_proof(reader, request):
    current_head(reader, TRACKER, request['sourceCommitSha'])
    run_id = request['sourceRunId']
    attempt = request['sourceRunAttempt']
    run = reader.api(f'/repos/{TRACKER}/actions/runs/{run_id}')
    if (run.get('id') != run_id or run.get('run_attempt') != attempt or run.get('head_sha') != request['sourceCommitSha']
            or run.get('path') != '.github/workflows/deploy-site.yml' or run.get('head_branch') != 'main'
            or run.get('event') not in {'push', 'workflow_dispatch'} or run.get('head_repository', {}).get('full_name') != TRACKER
            or run.get('status') not in {'in_progress', 'completed'}
            or run.get('status') == 'completed' and run.get('conclusion') != 'success'):
        raise ValueError('Source run/attempt/workflow is not approved')
    jobs = reader.api(f'/repos/{TRACKER}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
    if jobs.get('total_count', 101) > 100:
        raise ValueError('Source job count exceeds its bound')
    matching = [job for job in jobs.get('jobs', []) if job.get('name') == 'prepare-publication']
    if len(matching) != 1 or not successful_job(matching[0]) or matching[0].get('head_sha') != request['sourceCommitSha']:
        raise ValueError('Source preparation job is not completed successfully')
    bind_step(matching[0].get('steps', []), request['requestId'])
    ci_proof(reader, request['sourceCommitSha'])


def bind_step(steps, request_id):
    if not isinstance(steps, list) or len(steps) > 128:
        raise ValueError('Invalid source job step count')
    names = [step.get('name') for step in steps]
    expected = 'Bind publication request ' + request_id
    if names.count(expected) != 1:
        raise ValueError('Request is not bound by the protected source job')
    index = names.index(expected)
    if not successful_job(steps[index]) or any(step.get('name') not in EPILOGUES for step in steps[index + 1:]):
        raise ValueError('Request stamp is not the final authored successful step')


def ci_run_for_check(reader, check):
    match = re.fullmatch(r'https://github\.com/civic-source/us-code-tracker/actions/runs/([1-9][0-9]*)/job/[1-9][0-9]*(?:\?[^#]*)?', check.get('details_url', ''))
    if not match:
        raise ValueError('Required CI does not belong to the trusted repository')
    return reader.api(f'/repos/{TRACKER}/actions/runs/{int(match[1])}')


def ci_proof(reader, source_sha):
    checks = reader.api(f'/repos/{TRACKER}/commits/{source_sha}/check-runs?check_name=build&filter=latest&per_page=100')
    if checks.get('total_count', 101) > 100:
        raise ValueError('Required CI check count exceeds its bound')
    candidates = [check for check in checks.get('check_runs', []) if check.get('name') == 'build' and check.get('app', {}).get('slug') == 'github-actions']
    if len(candidates) > 10:
        raise ValueError('Ambiguous source build-check count')
    matching = []
    for check in candidates:
        run = ci_run_for_check(reader, check)
        if run.get('path') == '.github/workflows/ci.yml':
            matching.append((check, run))
    if len(matching) != 1:
        raise ValueError('Required CI workflow is absent or ambiguous')
    check, run = matching[0]
    if (not successful_job(check) or check.get('head_sha') != source_sha or run.get('head_sha') != source_sha
            or run.get('head_branch') != 'main' or run.get('event') != 'push' or not successful_job(run)):
        raise ValueError('Required source CI build is not successful at the exact SHA')


def predecessor(reader, request):
    actual = reader.locator(request['requestId'])
    actual_id = actual['generationId'] if actual is not None else None
    if actual_id != request['previousGenerationId']:
        raise ValueError('Main predecessor changed; publication must be reconciled')
    return actual


def release(reader, request_id):
    if not p.digest(request_id):
        raise ValueError('Invalid release request ID')
    tag = 'static-assets-request-' + request_id
    value = reader.api(f'/repos/{TRACKER}/releases/tags/{tag}')
    if value.get('tag_name') != tag or value.get('draft') is not False or value.get('immutable') is not True:
        raise ValueError('Request release is not published and immutable')
    expected_names = {'request.json', 'publication.json', 'main.tar.gz', 'assets.tar.gz'}
    assets = value.get('assets', [])
    if len(assets) != 4 or {asset.get('name') for asset in assets} != expected_names:
        raise ValueError('Release asset closure mismatch')
    return value


def download_asset(request_id, name, maximum, destination):
    if not p.digest(request_id) or name not in {'request.json', 'publication.json', 'main.tar.gz', 'assets.tar.gz'}:
        raise ValueError('Invalid fixed release asset identity')
    url = f'https://github.com/{TRACKER}/releases/download/static-assets-request-{request_id}/{name}'
    opener = urllib.request.build_opener(AssetRedirect())
    deadline = time.monotonic() + 900
    from publisher import safeio
    parent = safeio.directory(destination.parent)
    try:
        safeio.capacity(parent, maximum, 1)
        fd = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            total = 0
            with opener.open(urllib.request.Request(url, headers={'User-Agent': 'civic-source-assets', 'Accept-Encoding': 'identity'}), timeout=30) as response:
                if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise ValueError('Unexpected archive status or encoding')
                while data := response.read(65536):
                    total += len(data)
                    if total > maximum or time.monotonic() > deadline:
                        raise ValueError('Asset download exceeds its byte/time bound')
                    safeio.write_all(fd, data, parent)
            if total != maximum:
                raise ValueError('Release download did not match its declared immutable size')
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)
    finally:
        os.close(parent)


def own_receipt_bytes(request_id):
    if not p.digest(request_id):
        raise ValueError('Invalid own receipt identity')
    url = f'https://github.com/{ASSETS}/releases/download/static-assets-ready-{request_id}/receipt.json'
    request = urllib.request.Request(url, headers={'User-Agent': 'civic-source-assets', 'Accept-Encoding': 'identity'})
    with urllib.request.build_opener(AssetRedirect()).open(request, timeout=30) as response:
        if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise ValueError('Unexpected receipt readback status or encoding')
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError('Receipt readback exceeds its bound')
        return data
