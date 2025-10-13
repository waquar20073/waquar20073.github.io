# Configuration Sync Tool

This is an advanced command-line tool for synchronizing configuration files (INI format) between different branches or even different GitLab projects. It supports 3-way merging to prevent data loss, a user-friendly interactive mode, and a flag-based mode for automation.

## Features

- **3-Way and 2-Way Merging**: Safely merges changes by using Git history to find a common ancestor. Automatically falls back to a 2-way merge for unrelated files.
- **Interactive Mode**: A user-friendly menu-driven interface for guided syncing.
- **Automated Mode**: Full support for command-line flags for use in scripts and CI/CD pipelines.
- **Cross-Repository Sync**: Ability to sync configurations between two entirely different GitLab projects (e.g., non-prod to prod).
- **Local Project Caching**: Fetches and caches your GitLab projects locally for faster use in interactive mode.

## Configuration

The script is configured using the `sync-config.json` file.

```json
{
  "gitlab": {
    "url": "https://gitlab.com",
    "token": "YOUR_GITLAB_TOKEN",
    "prod_group_id": "YOUR_PROD_GROUP_ID",
    "non_prod_group_id": "YOUR_NON_PROD_GROUP_ID"
  },
  "defaults": {
    "csv_strategy": "union"
  },
  "ignore": {
    "keys": [
      "JAVA_OPTS",
      "*.secret"
    ]
  },
  "projects_prod": {},
  "projects_non_prod": {}
}
```

- **`gitlab` section**:
  - `url`: The base URL of your GitLab instance.
  - `token`: Your personal GitLab access token with `api` scope.
  - `prod_group_id` / `non_prod_group_id`: The numeric IDs for the GitLab groups that hold your projects. These are used by the `--update` command.
- **`defaults` section**:
  - `csv_strategy`: Strategy for handling CSV values (default: "union").
- **`ignore` section**:
  - `keys`: List of keys to ignore during sync (supports wildcards).
- **`projects_prod` / `projects_non_prod`**:
  - These sections are automatically populated by the `--update` command.

## How to Use

The script has three modes of operation.

### 1. Update Project Cache (First-Time Setup and Updates)

Before using the interactive mode, you need to build a local cache of your projects. This is also required when new projects are added to your GitLab groups.

To update the project cache, run:

```bash
python sync-configs.py --update
```

**What this does:**
1. Connects to your GitLab instance using the token from `sync-config.json`
2. Fetches all projects from the configured `prod_group_id` and `non_prod_group_id`
3. Updates the `projects_prod` and `projects_non_prod` objects in your config
4. **Safely preserves** all other fields including `ignore` and `defaults`
5. Creates a backup of your config at `sync-config.json.bak`

**Important Notes:**
- The script will never modify your `ignore` object or any custom fields you've added
- A backup is created before any changes are made
- You should run this command whenever new projects are added to your GitLab groups
- The configuration file is now in JSON format for better structure and type safety

### 2. Interactive Mode (Recommended for Manual Use)

For a user-friendly, guided experience, run the script with no arguments:

```bash
python sync-configs.py
```

The script will launch a menu that walks you through selecting the environment, projects, branches, and files to sync.

### 3. Automated / Flag-Based Mode

For scripting and automation, you can pass all the necessary information as command-line flags.

**Example 1: Sync different files in the same branch**

(e.g., `DEV-ABC.ini` -> `SIT-ABC.ini` on the `main` branch)

```bash
python sync-configs.py \
    --target-project-id 12345 \
    --source-branch main \
    --target-branch main \
    --source-env DEV-ABC.ini \
    --target-envs SIT-ABC.ini \
    --config sync-config.json
```

**Example 2: Sync a file across different branches in the same project**

(e.g., `config.ini` from `develop` -> `release/v1.0`)

```bash
python sync-configs.py \
    --target-project-id 12345 \
    --source-branch develop \
    --target-branch release/v1.0 \
    --source-env config.ini \
    --target-envs config.ini
```

**Example 3: Sync a file between two different projects (Non-Prod to Prod)**
```bash
python sync-configs.py \
    --source-project-id 12345 \
    --target-project-id 67890 \
    --source-branch main \
    --target-branch main \
    --source-env DEV-ABC.json \
    --target-envs SIT-ABC.json \
    --config sync-config.json
```
## Command-Line Arguments

- `--update`: Update the local cache of GitLab projects and exit.
{{ ... }}
- `-sp`, `--source-project-id`: GitLab Project ID to sync FROM. If omitted, uses target-project-id.
- `-tb`, `--target-branch`: Target branch for sync.
- `-sb`, `--source-branch`: Source branch name (default: `main`).
- `-sc`, `--source-commit`: Source commit hash (overrides source-branch).
- `-se`, `--source-env`: Source environment file name.
- `-te`, `--target-envs`: List of one or more target environment files.
- `-c`, `--config-file`: Path to the script\'s configuration file (default: `sync-config.ini`).
- `-d`, `--dry-run`: Show changes without writing files or creating an MR.
- `-p`, `--branch-prefix`: Prefix for the automatically created branch name.
- `-m`, `--commit-message`: The commit message to use.
