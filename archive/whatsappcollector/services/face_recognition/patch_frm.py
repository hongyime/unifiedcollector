"""
Patch face_recognition_models/__init__.py to use os.path instead of pkg_resources.
setuptools 70+ no longer exposes pkg_resources as a top-level module on Python 3.12-slim.
"""
import os
import site

OLD = "from pkg_resources import resource_filename"
NEW = (
    "import os as _os\n"
    "def resource_filename(pkg, path): "
    "return _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), path)"
)

for d in site.getsitepackages():
    f = os.path.join(d, "face_recognition_models", "__init__.py")
    if not os.path.exists(f):
        continue
    src = open(f).read()
    if OLD not in src:
        print(f"Already patched or not found in {f}")
        break
    open(f, "w").write(src.replace(OLD, NEW))
    print(f"Patched {f}")
    break
