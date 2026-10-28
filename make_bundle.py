"""Build the reproducible source patch and independent installation archive."""
import difflib
import json
from pathlib import Path
import subprocess
import tempfile

from patch_manager import FILES, PATCH_ID, PROJECT_NAME, VERSION, archive, digest, validate

root = Path(__file__).resolve().parent
manifest = {'project': PROJECT_NAME, 'patch_id': PATCH_ID, 'version': VERSION, 'retry_delay_ms': 0,
            'retry_limit': None, 'created_date': '2026-10-28',
            'files': {name: digest((root / name).read_bytes()) for name in FILES}}
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
validate(root)
out = root / 'dist'
out.mkdir(exist_ok=True)
stem = PROJECT_NAME + '-' + manifest['version']
package = out / (stem + '.tar.gz')
package.write_bytes(archive(root))
chunks = []
for name in FILES + ('manifest.json',):
    target = PROJECT_NAME + '/' + name
    chunks += ['diff --git a/' + target + ' b/' + target + '\n',
               'new file mode ' + ('100755' if name.endswith('.py') else '100644') + '\n']
    chunks.extend(difflib.unified_diff([], (root / name).read_text().splitlines(keepends=True),
                                     fromfile='/dev/null', tofile='b/' + target))
patch = out / (stem + '.patch')
patch.write_text(''.join(chunks))
with tempfile.TemporaryDirectory(prefix='codex-capacity-patch-check-') as folder:
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=folder, check=True)
    subprocess.run(['git', 'apply', str(patch)], cwd=folder, check=True)
    validate(Path(folder) / PROJECT_NAME)
(out / 'SHA256SUMS').write_text(''.join(digest(p.read_bytes()) + '  ' + p.name + '\n'
                                      for p in [package, patch]))
print(json.dumps({'package': str(package), 'patch': str(patch),
                  'checksums': str(out / 'SHA256SUMS'), 'source_patch_verified': True}))
