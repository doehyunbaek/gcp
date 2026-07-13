# gitcp

`gitcp` is a uv/uvx-friendly CLI that syncs local files and directories with files in a GitHub repository.

## Commands

```bash
uvx gitcp setting                 # interactive login/logout/status/repo/branch settings
uvx gitcp setting login           # non-interactive login options are also available
uvx gitcp setting branch feature  # change the configured branch for the active account
uvx gitcp <file-or-dir>           # upload only if local differs from GitHub
uvx gitcp <local-file-or-dir> :<remote>  # upload to default repo if different
uvx gitcp :<remote> <local>       # download from default repo
uvx gitcp status                  # show current settings
```

GitHub paths use scp-like prefixes:

- `:path` uses the configured default repo, e.g. `doehyunbaek/private:path`
- `repo:path` uses a repo under the default owner, e.g. `privatee:path` -> `doehyunbaek/privatee:path`
- `owner/repo:path` uses an explicit repo, e.g. `doehyunbaekk/privatee:path`
- `github:path` and `gh:path` are aliases for `:path`

In one-argument upload mode, absolute local paths keep their full path inside the repository without the leading slash; for example `~/.pi/agent/multicodex.json` syncs to `home/you/.pi/agent/multicodex.json`. Passing a directory uploads every file under that directory while preserving relative paths; changed files are packed with `git` and pushed as one commit. In one-argument download mode, `:path` copies from the configured repo/branch to an absolute local output path; `:~/.pi/agent/multicodex.json` reads `home/you/.pi/agent/multicodex.json` from GitHub and writes to `~/.pi/agent/multicodex.json`.

## Setup flow

The `uvx gitcp setting` flow is modeled after `gh auth login/logout`:

- pick GitHub.com or another GitHub Enterprise hostname
- choose `Login with a web browser` by default, matching `gh auth login`
- alternatively paste a token, read one with `--with-token`, or use `GITCP_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN`
- validate the token by reading the current GitHub user
- choose the sync repository (`owner/repo`), branch, and optional remote directory
- logout removes only local config; it does not revoke GitHub tokens

Token scope needed for private repositories and writes: `repo`.

Directory uploads use the local `git` executable to create and push one packed commit. Local blob hashes are cached under `~/.cache/gitcp` (or `GITCP_CACHE_DIR`) so repeated no-change syncs avoid rereading every file.

## Examples

```bash
# Install/run from this checkout during development
uv run --project . gitcp --help
uv run --project . gitcp setting

# Login with a browser and configure a repo
uv run --project . gitcp setting login --web -r owner/repo -b main

# Store a token from stdin and configure a repo
printf '%s' "$GH_TOKEN" | uv run --project . gitcp setting login --with-token -r owner/repo -b main

# Change only the configured branch
uv run --project . gitcp setting branch feature

# Push README.md to owner/repo:README.md only if different
uv run --project . gitcp README.md

# Push a directory recursively
uv run --project . gitcp ~/AutoGPT

# Push to an explicit remote path
uv run --project . gitcp README.md :README.md

# Pull README.md from GitHub to ./README.md
uv run --project . gitcp :README.md README.md --force

# Short forms
uv run --project . gitcp local.txt privatee:notes/local.txt
uv run --project . gitcp doehyunbaekk/privatee:notes/local.txt local.txt
```

## Configuration

Config is stored at:

```text
~/.config/gitcp/config.json
```

Override it with `GITCP_CONFIG` or `GITCP_CONFIG_DIR`.

## Testing

```bash
uv run --project . python -m unittest discover -s tests -v
```
