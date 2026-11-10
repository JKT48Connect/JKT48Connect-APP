"""Publish verified binary payloads; the application's source remains in its private repository."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ['GITHUB_REPOSITORY']
assert REPO == 'JKT48Connect/JKT48Connect-APP'
ROOT = Path(__file__).resolve().parents[2]
manifest = json.loads((ROOT / 'payload/manifest.json').read_text())
version = manifest['version']
assert re.fullmatch(r'\d+\.\d+\.\d+', version)
assert re.fullmatch(r'[0-9a-f]{64}', manifest['sha256'])
assert 0 < manifest['size'] < 150 * 1024 * 1024
HEADERS = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}

def api(path, method='GET', body=None, optional=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request('https://api.github.com/repos/' + REPO + path, data=data, method=method,
        headers={**HEADERS, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return None
        raise

def info(tag):
    found = api('/releases/tags/' + tag, optional=True)
    if found:
        return found
    return next((r for r in api('/releases?per_page=100') if r['tag_name'] == tag), None)

def upload(release, path, name):
    url = release['upload_url'].split('{')[0] + '?name=' + urllib.parse.quote(name)
    assert urllib.parse.urlparse(url).hostname == 'uploads.github.com'
    content = path.read_bytes()
    req = urllib.request.Request(url, data=content, method='POST', headers={**HEADERS, 'Content-Type': 'application/octet-stream'})
    with urllib.request.urlopen(req, timeout=180) as response:
        asset = json.load(response)
    assert asset['name'] == name and asset['size'] == len(content)
    if asset.get('digest'):
        assert asset['digest'] == 'sha256:' + hashlib.sha256(content).hexdigest()
    return asset

with tempfile.TemporaryDirectory() as tmp:
    apk = Path(tmp) / 'hikari.apk'
    digest = hashlib.sha256()
    if manifest.get('archive_url'):
        # One-time signed artifact transport; only the APK is retained in Releases.
        from zipfile import ZipFile
        url = urllib.parse.urlparse(manifest['archive_url'])
        assert url.scheme == 'https' and url.hostname.endswith('.oaiusercontent.com')
        archive = Path(tmp) / 'release.zip'
        with urllib.request.urlopen(manifest['archive_url'], timeout=180) as response:
            archive.write_bytes(response.read(150 * 1024 * 1024))
        with ZipFile(archive) as bundle:
            member = bundle.getinfo('Hikari-' + version + '.apk')
            assert 0 < member.file_size < 150 * 1024 * 1024
            apk.write_bytes(bundle.read(member))
        digest.update(apk.read_bytes())
    with apk.open('ab' if manifest.get('archive_url') else 'wb') as out:
        for part in manifest.get('parts', []):
            path = (ROOT / part).resolve()
            assert path.is_relative_to(ROOT / 'payload') and path.is_file()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk); out.write(chunk)
    assert apk.stat().st_size == manifest['size']
    assert digest.hexdigest() == manifest['sha256']
    body = manifest['notes'] + '\n\nSHA-256 APK: `' + manifest['sha256'] + '`'
    for tag, filename, alias in [('hikari-v' + version, 'Hikari-' + version + '.apk', False), ('hikari-latest', 'Hikari.apk', True)]:
        release = info(tag)
        if alias and release:
            old = re.search(r'\d+\.\d+\.\d+', release['name'])
            assert not old or tuple(map(int, version.split('.'))) >= tuple(map(int, old[0].split('.'))), 'Refusing to downgrade public Hikari'
        if not release:
            release = api('/releases', 'POST', {'tag_name': tag, 'target_commitish': 'main', 'name': 'Hikari ' + version,
                'body': body, 'draft': True, 'prerelease': True, 'make_latest': 'false'})
        checksum = Path(tmp) / 'checksum.txt'
        checksum.write_text(manifest['sha256'] + '  ' + filename + '\n')
        for path, name in [(apk, filename), (checksum, filename + '.sha256')]:
            previous = next((a for a in release['assets'] if a['name'] == name), None)
            expected = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
            if previous and not alias:
                assert previous['size'] == path.stat().st_size
                assert not previous.get('digest') or previous['digest'] == expected
                continue
            if previous:
                api('/releases/assets/' + str(previous['id']), 'DELETE')
            upload(release, path, name)
        published = api('/releases/' + str(release['id']), 'PATCH', {'name': 'Hikari ' + version, 'body': body,
            'draft': False, 'prerelease': True, 'make_latest': 'false'})
        print(published['html_url'])
    # The main JKT48Connect Android channel must remain GitHub's latest release.
    assert api('/releases/latest')['tag_name'] != 'hikari-latest'
