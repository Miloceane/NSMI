import os
import tempfile

# Must be set before the app modules are imported.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
