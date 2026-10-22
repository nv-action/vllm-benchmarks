"""Verify a downloaded source archive against the committed tag/hash baseline."""
import hashlib
import json
from pathlib import Path
import sys


def verify(component, tag, archive):
    lock = json.loads(Path(__file__).with_name('source-lock.json').read_text())
    expected = lock[component]
    if tag != expected['tag']:
        raise ValueError(f'{component}: unexpected tag {tag}')
    digest = hashlib.sha256()
    with Path(archive).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != expected['sha256']:
        raise ValueError(f'{component}: SHA-256 mismatch: {digest.hexdigest()}')
    print(f'{component} {tag}: SHA-256 verified')


if __name__ == '__main__':
    try:
        verify(*sys.argv[1:])
    except (ValueError, KeyError, OSError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
