#!/usr/bin/env python3
"""
sync-configs.py - Configuration synchronization tool for .ini files in a GitLab environment.
"""

import argparse
import configparser
import difflib
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Dict, Tuple

# -----------------------------
# Colored output replacement
# -----------------------------
def colored(text, color):
    """Prints colored text to the terminal using ANSI escape codes."""
    colors = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "blue": "\033[94m",
        "cyan": "\033[96m",
        "reset": "\033[0m",
    }
    return f"{colors.get(color, '')}{text}{colors['reset']}"

# -----------------------------
# INI file handling
# -----------------------------
def read_ini_file(file_path: Path) -> configparser.ConfigParser:
    """Reads an INI file, preserving case."""
    config = configparser.ConfigParser()
    config.optionxform = str  # Preserve case
    if file_path.exists():
        config.read(file_path)
    return config

def write_ini_file(file_path: Path, config: configparser.ConfigParser):
    """Writes a ConfigParser object to a file."""
    with open(file_path, 'w') as f:
        config.write(f)

# -----------------------------
# Git operations
# -----------------------------
def clone_repo(repo_url: str, token: str, branch: str, tmpdir: str):
    """Clones a git repository."""
    if token and repo_url.startswith("https://"):
        parts = repo_url.split("https://", 1)
        auth_url = f"https://oauth2:{token}@{parts[1]}"
    else:
        auth_url = repo_url
    
    cmd = ["git", "clone", "--branch", branch, auth_url, tmpdir]
    subprocess.run(cmd, check=True, capture_output=True)

def create_and_push_branch(repo_path: str, new_branch: str, commit_paths: List[str], commit_message: str):
    """Creates a new branch, commits changes, and pushes to the remote."""
    subprocess.run(["git", "checkout", "-b", new_branch], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "add"] + commit_paths, cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", f"{new_branch}:{new_branch}"], cwd=repo_path, check=True, capture_output=True)

# -----------------------------
# Main sync logic
# -----------------------------
def compare_and_merge_ini(source_config: configparser.ConfigParser, target_config: configparser.ConfigParser, ignore_keys: List[str], strategy: str, csv_strategy: str) -> Tuple[configparser.ConfigParser, List[str]]:
    """Compares and merges two ConfigParser objects."""
    changes = []
    merged_config = target_config

    if 'Default' not in source_config:
        return merged_config, changes

    if 'Default' not in merged_config:
        merged_config['Default'] = {}

    for key, source_value in source_config['Default'].items():
        if any(fnmatch.fnmatchcase(key, pattern) for pattern in ignore_keys):
            continue

        target_value = merged_config['Default'].get(key)

        is_csv = ',' in source_value or (target_value and ',' in target_value)

        if is_csv and csv_strategy == 'union':
            source_list = [item.strip() for item in source_value.split(',') if item.strip()]
            target_list = [item.strip() for item in (target_value or '').split(',') if item.strip()]
            
            # Preserve order of target list, then add new items from source list
            union_list = target_list + [item for item in source_list if item not in target_list]

            new_value = ','.join(union_list)

            if new_value != target_value:
                merged_config['Default'][key] = new_value
                changes.append(f"Updated {key} (union): '{target_value}' -> '{new_value}'")
        elif source_value != target_value:
            if strategy == 'replace':
                merged_config['Default'][key] = source_value
                changes.append(f"Updated {key}: '{target_value}' -> '{source_value}'")
            elif strategy == 'keep' and target_value is not None:
                pass # Keep target value
            else: # Keep when target is None
                merged_config['Default'][key] = source_value
                changes.append(f"Added {key}: '{source_value}'")

    return merged_config, changes

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(description="INI Configuration Sync Tool")
    parser.add_argument("--prod", action="store_true", help="Use production GitLab group.")
    parser.add_argument("--services", nargs="+", required=True, help="List of service names (repository names).")
    parser.add_argument("--source-env", required=True, help="Source environment file name (e.g., DEV-ABC.ini).")
    parser.add_argument("--target-env", required=True, help="Target environment file name (e.g., SIT-ABC.ini).")
    parser.add_argument("--sync-within-repo", action="store_true", help="Sync between files in the same repository.")
    parser.add_argument("--config-file", default="sync-config.ini", help="Path to the configuration file for the script.")
    parser.add_argument("--gitlab-token-env", default="GITLAB_TOKEN", help="Environment variable for GitLab token.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing files or creating MRs.")
    parser.add_argument("--branch-prefix", default="feature/config-sync")
    parser.add_argument("--commit-message", default="chore(config): Automated configuration sync")
    args = parser.parse_args()

    # --- Load script configuration ---
    script_config = configparser.ConfigParser()
    if not Path(args.config_file).exists():
        print(colored(f"ERROR: Script config file not found at '{args.config_file}'", "red"))
        sys.exit(1)
    script_config.read(args.config_file)
    
    gitlab_url = script_config.get('gitlab', 'url', fallback='https://gitlab.com')
    group_id = script_config.get('gitlab', 'prod_group_id' if args.prod else 'non_prod_group_id')
    ignore_keys = [key.strip() for key in script_config.get('ignore', 'keys', fallback='').split('\n') if key.strip()]
    default_strategy = script_config.get('defaults', 'strategy', fallback='replace')
    csv_strategy = script_config.get('defaults', 'csv_strategy', fallback='union')

    token = os.environ.get(args.gitlab_token_env)
    if not token:
        print(colored(f"ERROR: GitLab token not found in env var '{args.gitlab_token_env}'", "red"))
        sys.exit(1)

    tmpdir_base = tempfile.mkdtemp(prefix="config-sync-")

    try:
        for service in args.services:
            print(colored(f"\n=== Processing service: {service} ===", "cyan"))
            
            repo_url = f"{gitlab_url}/{group_id}/{service}.git"
            tmpdir = Path(tmpdir_base) / service
            
            print("Cloning repository...")
            try:
                # For sync-within-repo, we might need to decide which branch to clone.
                # For now, let's assume we work on a feature branch or a default branch.
                # This part might need more logic based on user workflow.
                # Let's assume 'main' for now. A real implementation might need a --branch argument.
                clone_repo(repo_url, token, "main", str(tmpdir))
            except subprocess.CalledProcessError as e:
                print(colored(f"ERROR cloning repo for service {service}: {e.stderr.decode()}", "red"))
                continue

            source_file = tmpdir / args.source_env
            target_file = tmpdir / args.target_env

            if not source_file.exists():
                print(colored(f"Source file not found: {source_file}", "yellow"))
                continue

            source_config = read_ini_file(source_file)
            target_config = read_ini_file(target_file)
            
            original_target_content = target_file.read_text() if target_file.exists() else ""

            merged_config, changes = compare_and_merge_ini(source_config, target_config, ignore_keys, default_strategy, csv_strategy)

            if not changes:
                print(colored("No changes to apply.", "green"))
                continue

            print("Changes detected:")
            for change in changes:
                print(f"  - {change}")

            # Write merged content to a temporary file to generate a diff
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_f:
                merged_config.write(temp_f)
                temp_f.flush()
                merged_content = Path(temp_f.name).read_text()

            diff = difflib.unified_diff(
                original_target_content.splitlines(keepends=True),
                merged_content.splitlines(keepends=True),
                fromfile=str(target_file.relative_to(tmpdir)),
                tofile=str(target_file.relative_to(tmpdir))
            )
            print(colored("Diff:\n" + "".join(diff), "yellow"))

            if args.dry_run:
                print(colored("DRY-RUN: No files will be written or MRs created.", "yellow"))
                continue

            # Here you would implement the logic to create a branch, commit, and create an MR.
            # This is a simplified example.
            new_branch_name = f"{args.branch_prefix}/{service}-{random.randint(1000, 9999)}"
            print(f"Creating and pushing new branch: {new_branch_name}")
            
            write_ini_file(target_file, merged_config)
            
            try:
                create_and_push_branch(str(tmpdir), new_branch_name, [str(target_file.relative_to(tmpdir))], args.commit_message)
                print(colored(f"Successfully pushed changes for service {service} to branch {new_branch_name}", "green"))
                # Here you would add the GitLab MR creation logic, similar to the old script.
            except subprocess.CalledProcessError as e:
                print(colored(f"ERROR during git operations for {service}: {e.stderr.decode()}", "red"))

    finally:
        shutil.rmtree(tmpdir_base)

if __name__ == "__main__":
    main()
