#!/usr/bin/env python3
"""
sync-configs.py - Advanced configuration synchronization tool for .ini files.

This script supports 3-way merging between branches in the same or different
repositories, 2-way merging for different files in the same branch, 
multi-environment sync, and automatic GitLab Merge Request creation.
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
import fnmatch
import requests
from typing import List, Dict, Tuple, Set

# -----------------------------
# Utilities
# -----------------------------

def colored(text: str, color: str) -> str:
    colors = {
        "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
        "blue": "\033[94m", "cyan": "\033[96m", "reset": "\033[0m",
    }
    return f"{colors.get(color, '')}{text}{colors['reset']}"

def unified_diff_str(a: str, b: str, fromfile: str, tofile: str) -> str:
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = difflib.unified_diff(a_lines, b_lines, fromfile=fromfile, tofile=tofile)
    return "".join(diff)

# -----------------------------
# INI File Handling
# -----------------------------

def parse_ini_from_string(content: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read_string(content)
    return config

def config_to_string(config: configparser.ConfigParser) -> str:
    with tempfile.NamedTemporaryFile(mode='w', delete=True, encoding='utf-8') as temp:
        config.write(temp)
        temp.flush()
        return Path(temp.name).read_text(encoding='utf-8')

# -----------------------------
# GitLab API & Git Operations
# -----------------------------

def get_project_info(gitlab_url: str, project_id: str, token: str) -> Dict:
    api_url = f"{gitlab_url}/api/v4/projects/{project_id}"
    try:
        response = requests.get(api_url, headers={"PRIVATE-TOKEN": token})
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(colored(f"ERROR fetching repository info for project {project_id}: {e}", "red"))
        sys.exit(1)

def clone_repo(repo_url: str, token: str, branch: str, tmpdir: str):
    auth_url = repo_url
    if token and repo_url.startswith("https://"):
        parts = repo_url.split("https://", 1)
        auth_url = f"https://oauth2:{token}@{parts[1]}"
    subprocess.run(["git", "clone", "--branch", branch, auth_url, tmpdir], check=True, capture_output=True, text=True)

def get_file_content_from_ref(repo_path: str, ref: str, file_path: str) -> str:
    if not ref: return ""
    try:
        return subprocess.run(["git", "show", f"{ref}:{file_path}"], cwd=repo_path, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError: return ""

def compute_three_way_ancestor(repo_path: str, source_ref: str, target_ref: str) -> str:
    try:
        return subprocess.run(["git", "merge-base", source_ref, target_ref], cwd=repo_path, capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError: return None

def create_and_push_branch(repo_path: str, new_branch: str, commit_message: str):
    subprocess.run(["git", "checkout", "-b", new_branch], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", new_branch], cwd=repo_path, check=True, capture_output=True)

# -----------------------------
# Merge Logic
# -----------------------------

def merge_ini_two_way(
    source_config: configparser.ConfigParser, target_config: configparser.ConfigParser,
    ignore_keys: List[str], csv_strategy: str
) -> Tuple[configparser.ConfigParser, List[str]]:
    changes = []
    merged_config = configparser.ConfigParser(interpolation=None)
    merged_config.optionxform = str
    merged_config.read_dict(target_config)

    if 'Default' not in source_config: return merged_config, changes
    if 'Default' not in merged_config: merged_config.add_section('Default')

    src = source_config['Default']
    for key, src_val in src.items():
        if any(fnmatch.fnmatchcase(key, pattern) for pattern in ignore_keys): continue

        tgt_val = merged_config['Default'].get(key)
        if src_val == tgt_val: continue

        is_csv = (src_val and ',' in src_val) or (tgt_val and ',' in tgt_val)
        if is_csv and csv_strategy == 'union':
            src_list = {s.strip() for s in (src_val or '').split(',') if s.strip()}
            tgt_list = {s.strip() for s in (tgt_val or '').split(',') if s.strip()}
            final_val = ",".join(sorted(list(tgt_list | src_list)))
        else:
            final_val = src_val
        
        if final_val != tgt_val:
            merged_config.set('Default', key, final_val)
            changes.append(f"Updated key '{key}': '{tgt_val}' -> '{final_val}'")

    return merged_config, changes

def merge_ini_three_way(
    ancestor_config: configparser.ConfigParser, source_config: configparser.ConfigParser,
    target_config: configparser.ConfigParser, ignore_keys: List[str], csv_strategy: str
) -> Tuple[configparser.ConfigParser, List[str], List[str]]:
    changes, conflicts = [], []
    merged_config = configparser.ConfigParser(interpolation=None)
    merged_config.optionxform = str
    merged_config.read_dict(target_config)

    anc = ancestor_config['Default'] if 'Default' in ancestor_config else {}
    src = source_config['Default'] if 'Default' in source_config else {}
    tgt = target_config['Default'] if 'Default' in target_config else {}
    
    if 'Default' not in merged_config: merged_config.add_section('Default')

    all_keys: Set[str] = set(anc.keys()) | set(src.keys()) | set(tgt.keys())

    for key in sorted(list(all_keys)):
        if any(fnmatch.fnmatchcase(key, pattern) for pattern in ignore_keys): continue

        anc_val, src_val, tgt_val = anc.get(key), src.get(key), tgt.get(key)
        src_changed, tgt_changed = src_val != anc_val, tgt_val != anc_val

        if src_changed and tgt_changed and src_val != tgt_val:
            conflicts.append(f"Conflict on key '{key}': source='{src_val}', target='{tgt_val}'")
            continue

        final_val, changed = tgt_val, False
        if src_changed and src_val != tgt_val:
            is_csv = (src_val and ',' in src_val) or (tgt_val and ',' in tgt_val)
            if is_csv and csv_strategy == 'union':
                src_list = {s.strip() for s in (src_val or '').split(',') if s.strip()}
                tgt_list = {s.strip() for s in (tgt_val or '').split(',') if s.strip()}
                anc_list = {s.strip() for s in (anc_val or '').split(',') if s.strip()}
                merged_set = tgt_list | (src_list - anc_list)
                final_val = ",".join(sorted(list(merged_set)))
                changed = final_val != tgt_val
            else:
                final_val, changed = src_val, True
        
        if src_val is None and anc_val is not None and not tgt_changed:
            if key in merged_config['Default']:
                del merged_config['Default'][key]
                changes.append(f"Removed key '{key}' as it was removed in source.")
            continue

        if changed:
            changes.append(f"Updated key '{key}': '{tgt_val}' -> '{final_val}'")
            merged_config.set('Default', key, str(final_val))
        elif final_val is not None and key not in merged_config['Default']:
             merged_config.set('Default', key, str(final_val))
             changes.append(f"Added key '{key}': '{final_val}'")

    return merged_config, changes, conflicts

# -----------------------------
# GitLab MR Creation
# -----------------------------

def create_gitlab_mr(
    gitlab_url: str, project_id: str, token: str, source_branch: str,
    target_branch: str, title: str, description: str
):
    headers = {"PRIVATE-TOKEN": token}
    payload = {
        "source_branch": source_branch, "target_branch": target_branch,
        "title": title, "description": description, "remove_source_branch": True,
    }
    mr_url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests"
    try:
        response = requests.post(mr_url, headers=headers, json=payload)
        response.raise_for_status()
        mr_data = response.json()
        print(colored(f"Successfully created MR: {mr_data['web_url']}", "green"))
    except requests.exceptions.RequestException as e:
        print(colored(f"ERROR creating MR: {e.response.text if e.response else e}", "red"))
        print(colored("Please create the MR manually.", "yellow"))

# -----------------------------
# Main Execution
# -----------------------------

def main():
    parser = argparse.ArgumentParser(description="Advanced INI Configuration Sync Tool")
    parser.add_argument("--target-project-id", required=True, help="GitLab Project ID to sync TO.")
    parser.add_argument("--source-project-id", help="GitLab Project ID to sync FROM. If omitted, uses target-project-id.")
    parser.add_argument("--target-branch", required=True, help="Target branch for sync.")
    parser.add_argument("--source-branch", default="main", help="Source branch name.")
    parser.add_argument("--source-commit", help="Source commit hash (overrides source-branch).")
    parser.add_argument("--source-env", required=True, help="Source environment file name (e.g., DEV-ABC.ini).")
    parser.add_argument("--target-envs", nargs='+', required=True, help="List of target environment files (e.g., SIT-ABC.ini UAT-ABC.ini).")
    parser.add_argument("--config-file", default="sync-config.ini", help="Path to the script's configuration file.")
    parser.add_argument("--gitlab-token-env", default="GITLAB_TOKEN", help="Environment variable for GitLab token.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing files or creating MRs.")
    parser.add_argument("--branch-prefix", default="feature/auto-config-sync")
    parser.add_argument("--commit-message", default="chore(config): Automated configuration sync")
    args = parser.parse_args()

    if not args.source_project_id: args.source_project_id = args.target_project_id

    script_config = configparser.ConfigParser()
    script_config.read(args.config_file)
    gitlab_url = script_config.get('gitlab', 'url', fallback='https://gitlab.com')
    ignore_keys = script_config.get('ignore', 'keys', fallback='').split()
    csv_strategy = script_config.get('defaults', 'csv_strategy', fallback='union')

    token = os.environ.get(args.gitlab_token_env)
    if not token: print(colored(f"ERROR: GitLab token not found in env var '{args.gitlab_token_env}'", "red")); sys.exit(1)

    target_project_info = get_project_info(gitlab_url, args.target_project_id, token)
    target_repo_url = target_project_info["http_url_to_repo"]
    tmpdir = tempfile.mkdtemp(prefix="config-sync-")

    try:
        print(f"Cloning target repo {target_project_info['path_with_namespace']} into {tmpdir}...")
        clone_repo(target_repo_url, token, args.target_branch, tmpdir)

        target_ref = f"origin/{args.target_branch}"
        is_same_repo_sync = args.source_project_id == args.target_project_id

        if not is_same_repo_sync:
            source_project_info = get_project_info(gitlab_url, args.source_project_id, token)
            source_repo_url = source_project_info["http_url_to_repo"]
            print(f"Adding source remote for {source_project_info['path_with_namespace']}...")
            subprocess.run(["git", "remote", "add", "source", source_repo_url], cwd=tmpdir, check=True)
            subprocess.run(["git", "fetch", "source", args.source_branch], cwd=tmpdir, check=True, capture_output=True)
            source_ref = args.source_commit or f"source/{args.source_branch}"
        else:
            if not args.source_commit:
                subprocess.run(["git", "fetch", "origin", args.source_branch], cwd=tmpdir, check=True, capture_output=True)
            source_ref = args.source_commit or f"origin/{args.source_branch}"

        is_same_branch_sync = is_same_repo_sync and args.source_branch == args.target_branch
        ancestor_commit = None if is_same_branch_sync else compute_three_way_ancestor(tmpdir, source_ref, target_ref)
        
        if not is_same_branch_sync and not ancestor_commit:
            print(colored(f"Warning: Could not find common ancestor. Proceeding with 2-way merge logic for safety.", "yellow"))

        all_changes, all_conflicts, all_diffs, files_to_commit = [], [], [], []
        for target_env_file in args.target_envs:
            print(colored(f"\n--- Processing {args.source_env} -> {target_env_file} ---", "blue"))
            
            src_content = get_file_content_from_ref(tmpdir, source_ref, args.source_env)
            tgt_content = get_file_content_from_ref(tmpdir, target_ref, target_env_file)

            if not src_content: print(colored(f"Warning: Source file '{args.source_env}' not found. Skipping.", "yellow")); continue

            if is_same_branch_sync or not ancestor_commit:
                # 2-Way Merge: Different files in same branch, or no common ancestor found
                merged_config, changes = merge_ini_two_way(
                    parse_ini_from_string(src_content), parse_ini_from_string(tgt_content), ignore_keys, csv_strategy
                )
                conflicts = []
            else:
                # 3-Way Merge: Syncing same file across different branches
                anc_content = get_file_content_from_ref(tmpdir, ancestor_commit, target_env_file)
                merged_config, changes, conflicts = merge_ini_three_way(
                    parse_ini_from_string(anc_content), parse_ini_from_string(src_content),
                    parse_ini_from_string(tgt_content), ignore_keys, csv_strategy
                )

            if conflicts: all_conflicts.extend([f"[{target_env_file}] {c}" for c in conflicts])
            if changes: all_changes.extend([f"[{target_env_file}] {c}" for c in changes])
            
            merged_content = config_to_string(merged_config)
            if tgt_content != merged_content:
                all_diffs.append(unified_diff_str(tgt_content, merged_content, fromfile=target_env_file, tofile=target_env_file))
                if not args.dry_run: (Path(tmpdir) / target_env_file).write_text(merged_content)
                files_to_commit.append(target_env_file)

        if all_conflicts: 
            print(colored("\nConflicts detected! Aborting.", "red"))
            for c in all_conflicts: print(f"  - {c}")
            sys.exit(1)

        if not files_to_commit: 
            print(colored("\nNo changes to sync.", "green"))
            sys.exit(0)

        print(colored("\nDiff:", "yellow")); print("\n".join(all_diffs))

        if args.dry_run: 
            print(colored("\nDRY-RUN: No files written, no MR created.", "yellow"))
            sys.exit(0)

        new_branch = f"{args.branch_prefix}-{random.randint(1000, 9999)}"
        create_and_push_branch(tmpdir, new_branch, args.commit_message)
        mr_title = f"chore(config): Automated configuration sync from {args.source_env}"
        mr_desc = f"Automated configuration sync from `{args.source_project_id}:{args.source_branch}:{args.source_env}` to `{args.target_project_id}:{args.target_branch}`.\n\n**Changes:**\n- " + "\n- ".join(all_changes)
        create_gitlab_mr(
            gitlab_url, args.target_project_id, token, new_branch, args.target_branch, mr_title, mr_desc
        )

    finally:
        shutil.rmtree(tmpdir)

if __name__ == "__main__":
    main()