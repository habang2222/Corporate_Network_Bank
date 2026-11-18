"""Create local-only development credentials once; never log secret values."""
import json
import os
import secrets
from pathlib import Path

root = Path('/secrets')
root.mkdir(exist_ok=True)
os.umask(0o077)
users = {
    'alice': {'role': 'customer', 'customer_id': 'c001'},
    'bob': {'role': 'customer', 'customer_id': 'c002'},
    'teller': {'role': 'teller', 'customer_id': None},
    'auditor': {'role': 'auditor', 'customer_id': None},
    'operator': {'role': 'operator', 'customer_id': None},
}
for user in users.values():
    user['password'] = secrets.token_urlsafe(24)
for name, value in {
    'users.json': json.dumps(users),
    'db-password': secrets.token_urlsafe(32),
    'service-key': secrets.token_urlsafe(32),
}.items():
    path = root / name
    if not path.exists():
        with path.open('x') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    if name == 'db-password':
        # Official PostgreSQL entrypoint reads this after dropping to its own UID.
        path.chmod(0o444)
print('Development credentials ready; existing values preserved.')
