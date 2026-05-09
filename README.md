# Github-backed file sync

`gcp` is a uv/uvx-friendly CLI that syncs individual local files with files in a GitHub repository.

## Commands

```bash
uvx gcp setting                 # interactive login/logout/status/repo settings
uvx gcp setting login           # non-interactive login options are also available
uvx gcp push <file_name>        # upload local file to GitHub
uvx gcp <file_name>             # download GitHub file to local path
uvx gcp status                  # show current settings
```

`uvx gcp <file_name>` pulls by default. `uvx gcp push <file_name>` pushes.

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

# Push README.md to owner/repo:README.md
uv run --project . gcp push README.md

# Pull README.md from GitHub to ./README.md
uv run --project . gcp README.md --force

# Use a different remote path
uv run --project . gcp push local.txt --remote-path notes/local.txt
uv run --project . gcp local.txt --remote-path notes/local.txt
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
