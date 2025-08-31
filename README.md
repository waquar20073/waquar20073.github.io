# Config Sync Tool

A Python-based CLI tool to **safely synchronize configuration files across multiple services and environments** using GitLab Merge Requests (MRs).
It includes schema validation, per-key merge strategies, MR auto-creation, and warning output for sensitive properties.

---

## ✨ Features

* **Multi-service & multi-environment sync** in a single run.
* **GitLab integration**:

  * Fetch and update files directly via GitLab REST API (`requests`).
  * Create branches.
  * Open Merge Requests automatically with a diff summary.
* **Config management**:

  * Ignore environment-specific properties (e.g., `dbUrl`).
  * Show warnings in **red/yellow** for sensitive property changes.
* **Per-key strategies**:

  * `replace`: overwrite the target value.
  * `union`: merge list/dict values from both source and target.
* **Validation**:

  * JSON Schema validation of configuration files (blocks commits on failure).
* **Merge safety**:

  * 3-way merge using Git history to detect divergent changes.
* **Multi-branch updates**:

  * `--apply-to-multiple-target-branches` mode for backporting or forward-porting changes.

---

## 📂 Project Structure

```
.
├── sync-config.json       # Sync rules & service definitions
├── config-schema.json     # JSON schema for validating configs
├── sync.py                # Main Python CLI script
└── README.md              # Documentation
```

---

## ⚙️ Configuration

### `sync-config.json`

Defines services, environments, and merge rules.

```json
{
  "gitlab": {
    "url": "https://gitlab.com/api/v4",
    "token_env": "GITLAB_TOKEN",
    "default_project_id": "12345678",
    "default_source_branch": "develop"
  },
  "services": {
    "accounts-service": {
      "project_id": "12345678",
      "config_file": "src/main/resources/application.properties",
      "ignore_keys": ["dbUrl", "dbUser", "dbPassword"],
      "merge_strategy": {
        "allowedHosts": "union",
        "spring.profiles.active": "replace"
      },
      "target_branches": ["staging", "production"]
    },
    "expenses-service": {
      "project_id": "23456789",
      "config_file": "src/main/resources/application.properties",
      "ignore_keys": ["dbUrl", "dbUser", "dbPassword"],
      "merge_strategy": {
        "allowedIPs": "union",
        "log.level": "replace"
      },
      "target_branches": ["staging", "uat", "production"]
    }
  }
}
```

---

### `config-schema.json`

Defines validation rules for all configuration files.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "dbUrl": { "type": "string", "format": "uri" },
    "dbUser": { "type": "string" },
    "dbPassword": { "type": "string" },
    "allowedHosts": { "type": "array", "items": { "type": "string" } },
    "allowedIPs": { "type": "array", "items": { "type": "string" } },
    "spring.profiles.active": {
      "type": "string",
      "enum": ["dev", "staging", "uat", "production"]
    },
    "log.level": {
      "type": "string",
      "enum": ["DEBUG", "INFO", "WARN", "ERROR"]
    }
  },
  "required": ["dbUrl", "dbUser", "dbPassword"]
}
```

---

## 🚀 Usage

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Export your GitLab token

```bash
export GITLAB_TOKEN="your_token_here"
```

### 3. Run the sync

```bash
python sync.py --service accounts-service --source-branch develop --target-branches staging production
```

### 4. Apply to multiple target branches (auto mode)

```bash
python sync.py --service accounts-service --apply-to-multiple-target-branches
```

---

## 📖 Script Walkthrough

### Main Functions

#### `load_config()`

* Loads `sync-config.json`.
* Returns service definitions, ignore keys, and strategies.

#### `validate_config(path, schema_path)`

* Validates a config file against `config-schema.json`.
* Exits with error (red output) if invalid.

#### `three_way_merge(base, source, target)`

* Detects conflicts using Git history.
* Applies per-key strategies (union vs replace).
* Warns on ignored property changes.

#### `push_file_to_gitlab(url, project_id, branch, filepath, content, token)`

* Updates or creates config files via GitLab API.

#### `create_merge_request(project_id, branch, target_branch, title, body)`

* Creates MR via GitLab API.
* Includes a **diff summary** in MR body.

---

## 🖌️ Color-coded Warnings

* **Yellow**: Ignored property updated in `develop` branch.
* **Red**: Validation failed → commit blocked.

---

## Example Run

```bash
python sync.py --service accounts-service --source-branch develop --target-branches release/v1.0.0
```

Output:

```
[INFO] Validating configs with schema...
[OK]   accounts-service/application.properties is valid.
[WARN] dbUrl updated in develop. Please manually verify in staging/prod.
[INFO] Creating MR: Sync configs from develop → release/v1.0.0
[OK]   MR created: https://gitlab.com/org/accounts-service/-/merge_requests/42
```

---

## 🔮 Future Enhancements

* Teams notifications on MR creation.
* Auto-resolve safe conflicts.
* GUI.
---