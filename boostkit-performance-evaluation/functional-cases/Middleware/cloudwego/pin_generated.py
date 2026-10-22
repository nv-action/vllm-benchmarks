"""Pin generated Volo projects to the source and versions under test."""
from pathlib import Path
import re
import shutil
import sys
import tomllib

project, source = map(Path, sys.argv[1:])
lock = tomllib.loads((source / 'Cargo.lock').read_text())
versions = {p['name']: p['version'] for p in lock['package']}
manifest = project / 'Cargo.toml'
text = manifest.read_text()
for name in ('volo', 'volo-thrift', 'volo-build'):
    path = (source / name).as_posix()
    text, count = re.subn(rf'^{name} = "\*"$', f'{name} = {{ path = "{path}" }}', text, flags=re.M)
    if count != 1:
        raise ValueError(f'expected one {name} wildcard dependency')
for name in ('pilota', 'anyhow', 'tokio'):
    text, count = re.subn(rf'^{name} = "(?:\*|1)"$', f'{name} = "={versions[name]}"', text, flags=re.M)
    if count != 1:
        raise ValueError(f'expected one {name} dependency')
manifest.write_text(text)
shutil.copyfile(source / 'Cargo.lock', project / 'Cargo.lock')
