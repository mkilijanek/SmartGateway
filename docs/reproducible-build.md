# Reproducible build (optional)

A plain `docker build .` works as before. For a build that gives the same result tomorrow, use the strict mode:

```bash
./tools/strict-build.sh            # image smartgateway:strict
```

What it changes:

- **Base images by digest.** `node` and `python` tags are resolved to `@sha256:...` at build time and printed, so you can record them.
  Override the tags with `NODE_IMAGE` / `PYTHON_IMAGE`, or pass a digest directly via `--build-arg`.
- **Hash-locked Python dependencies.** `backend/requirements.lock` lists every package, including transitive ones, with sha256 hashes.
  pip refuses anything that does not match, and `pip` itself is not upgraded.
- **SBOM.** If [`syft`](https://github.com/anchore/syft) is installed, an SPDX SBOM is written next to the build.

The frontend always installs from `package-lock.json` (`npm ci`), in both modes.

## Keeping the lock current

After changing `backend/requirements.txt`, run `tools/relock.sh` (needs `pip-tools`) and commit `requirements.lock`.
Dependabot updates `requirements.txt`; regenerate the lock in the same pull request.

## Vulnerability scans

The *Audit* workflow (weekly, and on demand) runs `pip-audit` against `requirements.lock` and `npm audit` for production
dependencies. It does not run on pull requests, so a newly published CVE does not block unrelated work.
