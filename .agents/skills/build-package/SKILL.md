---
name: build-package
description: >-
  Builds the OctoPrint-OctocamRtc .zip distribution archive in dist/ and runs the unit
  test suite. Use this skill whenever the user asks to create a build, package the
  plugin, or prepare a release.
---

# Build OctoPrint-OctocamRtc Package

The local Python environment may not have `setuptools` or `build` installed (`python setup.py sdist` will fail with `ModuleNotFoundError: No module named 'setuptools'`). Always use the bundled standard-library build script to generate the `.zip` distribution archive.

## Steps

1. **Run the unit test suite** to verify all tests pass before packaging:
   ```powershell
   python -m unittest discover -s tests -v
   ```

2. **Generate the `.zip` distribution archive** in `dist/` using the helper script:
   [build_dist.py](./scripts/build_dist.py)
   ```powershell
   python .agents/skills/build-package/scripts/build_dist.py
   ```

3. **Verify the output**:
   Confirm that `dist/OctoPrint-OctocamRtc-<version>.zip` is printed with a non-zero byte size.
