# Github-backed file sync

`gcp` is a uv/uvx-friendly CLI that syncs local files and directories with files in a GitHub repository.

## Commands

```bash
uvx gcp setting                 # interactive login/logout/status/repo/branch settings
uvx gcp setting login           # non-interactive login options are also available
uvx gcp setting branch feature  # change the configured branch for the active account
uvx gcp <file-or-dir>           # upload only if local differs from GitHub
uvx gcp <local-file-or-dir> :<remote>  # upload to default repo if different
uvx gcp :<remote> <local>       # download from default repo
uvx gcp status                  # show current settings
```

GitHub paths use scp-like prefixes:

- `:path` uses the configured default repo, e.g. `doehyunbaek/private:path`
- `repo:path` uses a repo under the default owner, e.g. `privatee:path` -> `doehyunbaek/privatee:path`
- `owner/repo:path` uses an explicit repo, e.g. `doehyunbaekk/privatee:path`
- `github:path` and `gh:path` are aliases for `:path`

In one-argument upload mode, paths under your home directory keep their home-relative path; for example `~/.pi/agent/multicodex.json` syncs to `.pi/agent/multicodex.json`. Passing a directory uploads every file under that directory while preserving relative paths. In one-argument download mode, `:path` copies from the configured repo/branch to `path`; `:~/.pi/agent/multicodex.json` reads `.pi/agent/multicodex.json` from GitHub and writes to `~/.pi/agent/multicodex.json`.

## Setup flow

The `uvx gcp setting` flow is modeled after `gh auth login/logout`:

- pick GitHub.com or another GitHub Enterprise hostname
- choose `Login with a web browser` by default, matching `gh auth login`
- alternatively paste a token, read one with `--with-token`, or use `GCP_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN`
- validate the token by reading the current GitHub user
- choose the sync repository (`owner/repo`), branch, and optional remote directory
- logout removes only local config; it does not revoke GitHub tokens

Token scope needed for private repositories and writes: `repo`.

## Examples

```bash
# Install/run from this checkout during development
uv run --project . gcp --help
uv run --project . gcp setting

# Login with a browser and configure a repo
uv run --project . gcp setting login --web -r owner/repo -b main

# Store a token from stdin and configure a repo
printf '%s' "$GH_TOKEN" | uv run --project . gcp setting login --with-token -r owner/repo -b main

# Change only the configured branch
uv run --project . gcp setting branch feature

# Push README.md to owner/repo:README.md only if different
uv run --project . gcp README.md

# Push a directory recursively
uv run --project . gcp ~/AutoGPT

# Push to an explicit remote path
uv run --project . gcp README.md :README.md

# Pull README.md from GitHub to ./README.md
uv run --project . gcp :README.md README.md --force

# Short forms
uv run --project . gcp local.txt privatee:notes/local.txt
uv run --project . gcp doehyunbaekk/privatee:notes/local.txt local.txt
```

## Configuration

Config is stored at:

```text
~/.config/gcp/config.json
```

Override it with `GCP_CONFIG` or `GCP_CONFIG_DIR`.

## Testing

```bash
uv run --project . python -m unittest discover -s tests -v
```
