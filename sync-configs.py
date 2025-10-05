#!/usr/bin/env python3
"""
sync-configs.py - Advanced configuration synchronization tool for .ini files.

Supports multiple modes of operation:
- Interactive, menu-driven mode for guided syncing.
- Non-interactive (flag-based) mode for automation.
- A cache update mode to refresh local project lists from GitLab.
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
import time
from pathlib import Path
import fnmatch
import requests
from typing import List, Dict, Tuple, Set, Any, Optional

# ------------------------------------------------------------------------------
# SECTION 1: Core Utilities & Helpers
# ------------------------------------------------------------------------------

def colored(text: str, color: str) -> str:
    colors = {"red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m", "blue": "\033[94m", "cyan": "\033[96m", "reset": "\033[0m"}
    return f"{colors.get(color, '')}{text}{colors['reset']}"

def unified_diff_str(a: str, b: str, fromfile: str, tofile: str) -> str:
    return "".join(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True), fromfile=fromfile, tofile=tofile))

def parse_ini_from_string(content: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read_string(content)
    return config

def config_to_string(config: configparser.ConfigParser) -> str:
    with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as temp:
        config.write(temp)
    with open(temp.name, 'r', encoding='utf-8') as f:
        content = f.read()
    os.remove(temp.name)
    return content

def select_from_list(prompt: str, options: List[Any]) -> Any:
    print(prompt)
    for i, option in enumerate(options, 1):
        print(f"  {i}) {option}")
    while True:
        try:
            choice = int(input("Enter number: "))
            if 1 <= choice <= len(options):
                return options[choice - 1]
            else:
                print(colored("Invalid number, please try again.", "yellow"))
        except ValueError:
            print(colored("Please enter a valid number.", "yellow"))

def force_rmtree(path: str, max_retries: int = 3, delay: float = 0.7):
    """A wrapper for shutil.rmtree that retries on PermissionError, common on Windows."""
    for i in range(max_retries):
        try:
            if Path(path).exists():
                shutil.rmtree(path)
            return
        except PermissionError:
            time.sleep(delay)
        except FileNotFoundError:
            return # Already gone
    print(colored(f"Failed to delete temporary directory {path} after {max_retries} retries.", "red"))

# ------------------------------------------------------------------------------
# SECTION 2: GitLab API & Git Operations
# ------------------------------------------------------------------------------

def get_gitlab_api_paged(url: str, token: str, params: Optional[Dict] = None) -> List[Dict]:
    """Fetch paginated results from GitLab API.
    
    Args:
        url: API endpoint URL
        token: GitLab access token
        params: Optional query parameters
        
    Returns:
        List of results from all pages
    """
    headers = {"PRIVATE-TOKEN": token}
    results: List[Dict] = []
    
    try:
        while url:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            if not isinstance(data, list):
                print(colored(f"Unexpected API response format. Expected list, got {type(data).__name__}", "red"))
                print(f"Response: {data}")
                return []
                
            results.extend(data)
            url = response.links.get('next', {}).get('url')
            
            # Reset params after first request as they're included in the next URL
            params = None
            
    except requests.exceptions.RequestException as e:
        print(colored(f"GitLab API Error: {e}", "red"))
        if hasattr(e, 'response') and e.response is not None:
            print(f"Status code: {e.response.status_code}")
            try:
                print(f"Response: {e.response.text}")
            except:
                pass
        return []
        
    return results

def get_project_info(gitlab_url: str, project_id: str, token: str) -> Dict:
    """Get project information from GitLab API.
    
    Args:
        gitlab_url: Base URL of the GitLab instance
        project_id: ID or URL-encoded path of the project
        token: GitLab access token
        
    Returns:
        Dict containing project information or None if not found
    """
    url = f"{gitlab_url}/api/v4/projects/{project_id}"
    headers = {"PRIVATE-TOKEN": token}
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(colored(f"GitLab API Error when fetching project {project_id}: {e}", "red"))
        if hasattr(e, 'response') and e.response is not None:
            print(f"Status code: {e.response.status_code}")
            try:
                print(f"Response: {e.response.text}")
            except:
                pass
        return None

def get_group_projects(gitlab_url: str, group_id: str, token: str) -> List[Dict]:
    """Get all projects from a GitLab group, including subgroups.
    
    Args:
        gitlab_url: Base URL of the GitLab instance
        group_id: ID or path of the group
        token: GitLab access token
        
    Returns:
        List of project dictionaries
    """
    url = f"{gitlab_url}/api/v4/groups/{group_id}/projects"
    params = {
        "include_subgroups": "true",
        "per_page": "100",  # Maximum allowed by GitLab
        "simple": "true"     # Only include essential fields
    }
    return get_gitlab_api_paged(url, token, params)

def get_repo_branches(gitlab_url: str, project_id: str, token: str) -> List[str]:
    """Get all branch names for a GitLab repository.
    
    Args:
        gitlab_url: Base URL of the GitLab instance
        project_id: ID or URL-encoded path of the project
        token: GitLab access token
        
    Returns:
        List of branch names, or empty list on error
    """
    try:
        branches = get_gitlab_api_paged(
            f"{gitlab_url}/api/v4/projects/{project_id}/repository/branches",
            token,
            {"per_page": "100"}  # Maximum allowed by GitLab
        )
        return [b['name'] for b in branches if isinstance(b, dict) and 'name' in b]
    except Exception as e:
        print(colored(f"Error getting branches for project {project_id}: {e}", "red"))
        return []

def get_repo_ini_files(repo_url: str, branch: str, token: str) -> List[str]:
    tmpdir = tempfile.mkdtemp(prefix="ini-list-")
    try:
        print(f"Cloning repo to list .ini files...", end='\r')
        clone_repo(repo_url, token, branch, tmpdir)
        print(" "*50, end='\r') # Clear line
        return sorted([str(p.relative_to(tmpdir)) for p in Path(tmpdir).rglob('*.ini')])
    finally:
        force_rmtree(tmpdir)

def clone_repo(repo_url: str, token: str, branch: str, tmpdir: str):
    auth_url = f"https://oauth2:{token}@{repo_url.split('https://')[1]}" if token and repo_url.startswith('https://') else repo_url
    subprocess.run(["git", "clone", "--branch", branch, "--depth", "1", auth_url, tmpdir], check=True, capture_output=True, text=True)

def get_file_content_from_ref(repo_path: str, ref: str, file_path: str) -> str:
    if not ref: return ""
    try:
        return subprocess.run(["git", "show", f"{ref}:{file_path}"], cwd=repo_path, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError: return ""

def compute_three_way_ancestor(repo_path: str, source_ref: str, target_ref: str) -> Optional[str]:
    try:
        return subprocess.run(["git", "merge-base", source_ref, target_ref], cwd=repo_path, capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError: return None

def create_and_push_branch(repo_path: str, new_branch: str, commit_message: str):
    subprocess.run(["git", "checkout", "-b", new_branch], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", new_branch], cwd=repo_path, check=True, capture_output=True)

def create_gitlab_mr(gitlab_url: str, project_id: str, token: str, source_branch: str, target_branch: str, title: str, description: str) -> bool:
    """Create a merge request in GitLab.
    
    Args:
        gitlab_url: Base URL of the GitLab instance
        project_id: ID or URL-encoded path of the project
        token: GitLab access token
        source_branch: Source branch name
        target_branch: Target branch name
        title: MR title
        description: MR description
        
    Returns:
        bool: True if MR was created successfully, False otherwise
    """
    api_url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests"
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    payload = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": True,
        "squash": True
    }
    
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        print(colored(f"Successfully created merge request: {response.json().get('web_url')}", "green"))
        return True
    except requests.exceptions.RequestException as e:
        error_msg = f"Failed to create merge request: {e}"
        if hasattr(e, 'response') and e.response is not None:
            error_msg += f"\nStatus code: {e.response.status_code}"
            try:
                error_msg += f"\nResponse: {e.response.text}"
            except:
                pass
        print(colored(error_msg, "red"))
        return False
    try:
        response = requests.post(api_url, headers={"PRIVATE-TOKEN": token}, json=payload)
        response.raise_for_status()
        print(colored(f"Successfully created MR: {response.json()['web_url']}", "green"))
    except requests.exceptions.RequestException as e:
        print(colored(f"ERROR creating MR: {e.response.text if e.response else e}", "red"))

# ------------------------------------------------------------------------------
# SECTION 3: Merge Logic
# ------------------------------------------------------------------------------

def merge_ini_two_way(source_config, target_config, ignore_keys, csv_strategy):
    changes = []
    merged_config = configparser.ConfigParser(interpolation=None); merged_config.optionxform = str
    merged_config.read_dict(target_config)
    if 'Default' not in source_config: return merged_config, changes
    if 'Default' not in merged_config: merged_config.add_section('Default')
    src = source_config['Default']
    for key, src_val in src.items():
        if any(fnmatch.fnmatchcase(key, p) for p in ignore_keys): continue
        tgt_val = merged_config['Default'].get(key)
        if src_val == tgt_val: continue
        is_csv = (src_val and ',' in src_val) or (tgt_val and ',' in tgt_val)
        final_val = src_val
        if is_csv and csv_strategy == 'union':
            src_list = {s.strip() for s in (src_val or '').split(',') if s.strip()}
            tgt_list = {s.strip() for s in (tgt_val or '').split(',') if s.strip()}
            final_val = ",".join(sorted(list(tgt_list | src_list)))
        if final_val != tgt_val:
            merged_config.set('Default', key, final_val)
            changes.append(f"Updated key '{key}': '{tgt_val}' -> '{final_val}'")
    return merged_config, changes

def merge_ini_three_way(ancestor_config, source_config, target_config, ignore_keys, csv_strategy):
    changes, conflicts = [], []
    merged_config = configparser.ConfigParser(interpolation=None); merged_config.optionxform = str
    merged_config.read_dict(target_config)
    anc = ancestor_config['Default'] if 'Default' in ancestor_config else {}
    src = source_config['Default'] if 'Default' in source_config else {}
    tgt = target_config['Default'] if 'Default' in target_config else {}
    if 'Default' not in merged_config: merged_config.add_section('Default')
    all_keys: Set[str] = set(anc.keys()) | set(src.keys()) | set(tgt.keys())
    for key in sorted(list(all_keys)):
        if any(fnmatch.fnmatchcase(key, p) for p in ignore_keys): continue
        anc_val, src_val, tgt_val = anc.get(key), src.get(key), tgt.get(key)
        src_changed, tgt_changed = src_val != anc_val, tgt_val != anc_val
        if src_changed and tgt_changed and src_val != tgt_val:
            conflicts.append(f"Conflict on key '{key}': source='{src_val}', target='{tgt_val}'"); continue
        final_val, changed = tgt_val, False
        if src_changed and src_val != tgt_val:
            is_csv = (src_val and ',' in src_val) or (tgt_val and ',' in tgt_val)
            if is_csv and csv_strategy == 'union':
                src_list = {s.strip() for s in (src_val or '').split(',') if s.strip()}
                tgt_list = {s.strip() for s in (tgt_val or '').split(',') if s.strip()}
                anc_list = {s.strip() for s in (anc_val or '').split(',') if s.strip()}
                final_val = ",".join(sorted(list(tgt_list | (src_list - anc_list))))
                changed = final_val != tgt_val
            else:
                final_val, changed = src_val, True
        if src_val is None and anc_val is not None and not tgt_changed:
            if key in merged_config['Default']: del merged_config['Default'][key]; changes.append(f"Removed key '{key}'")
            continue
        if changed:
            merged_config.set('Default', key, str(final_val)); changes.append(f"Updated key '{key}': '{tgt_val}' -> '{final_val}'")
        elif final_val is not None and key not in merged_config['Default']:
             merged_config.set('Default', key, str(final_val)); changes.append(f"Added key '{key}': '{final_val}'")
    return merged_config, changes, conflicts

# ------------------------------------------------------------------------------
# SECTION 4: Application Modes
# ------------------------------------------------------------------------------

def update_projects_cache(config_file: str, gitlab_url: str, token: str):
    print("Updating project cache from GitLab...")
    config = configparser.ConfigParser(interpolation=None); config.optionxform = str
    config.read(config_file)
    if not config.has_section('gitlab'):
        print(colored("ERROR: [gitlab] section not in config!", "red"))
        sys.exit(1)
    for env in ['prod', 'non_prod']:
        group_id = config.get('gitlab', f'{env}_group_id', fallback=None)
        if not group_id: continue
        section = f'projects_{env}'
        print(f"Fetching projects for '{env}' group ({group_id})...")
        projects = get_group_projects(gitlab_url, group_id, token)
        if config.has_section(section): config.remove_section(section)
        config.add_section(section)
        for proj in sorted(projects, key=lambda p: p['name_with_namespace']):
            config.set(section, proj['name_with_namespace'], str(proj['id']))
    with open(config_file, 'w') as f:
        config.write(f)
    print(colored("Project cache updated successfully!", "green"))

def run_sync_operation(args: argparse.Namespace, token: str):
    config = configparser.ConfigParser(); config.read(args.config_file)
    gitlab_url = config.get('gitlab', 'url', fallback='https://gitlab.com')
    ignore_keys = config.get('ignore', 'keys', fallback='').split()
    csv_strategy = config.get('defaults', 'csv_strategy', fallback='union')
    target_info = get_project_info(gitlab_url, args.target_project_id, token)
    tmpdir = tempfile.mkdtemp(prefix="config-sync-")
    try:
        print(f"Cloning target repo {target_info['path_with_namespace']}...")
        clone_repo(target_info["http_url_to_repo"], token, args.target_branch, tmpdir)
        target_ref, is_same_repo = f"origin/{args.target_branch}", args.source_project_id == args.target_project_id
        if not is_same_repo:
            source_info = get_project_info(gitlab_url, args.source_project_id, token)
            print(f"Adding source remote for {source_info['path_with_namespace']}...")
            subprocess.run(["git", "remote", "add", "source", source_info["http_url_to_repo"]], cwd=tmpdir, check=True)
            subprocess.run(["git", "fetch", "source", args.source_branch], cwd=tmpdir, check=True, capture_output=True)
            source_ref = args.source_commit or f"source/{args.source_branch}"
        else:
            if not args.source_commit: subprocess.run(["git", "fetch", "origin", args.source_branch], cwd=tmpdir, check=True, capture_output=True)
            source_ref = args.source_commit or f"origin/{args.source_branch}"
        is_same_branch = is_same_repo and args.source_branch == args.target_branch
        ancestor = None if is_same_branch else compute_three_way_ancestor(tmpdir, source_ref, target_ref)
        if not is_same_branch and not ancestor:
            print(colored("Warning: No common ancestor. Using 2-way merge.", "yellow"))
        all_changes, all_conflicts, all_diffs, files_to_commit = [], [], [], []
        for target_env in args.target_envs:
            print(colored(f"\n--- Processing {args.source_env} -> {target_env} ---", "blue"))
            src_content = get_file_content_from_ref(tmpdir, source_ref, args.source_env)
            tgt_content = get_file_content_from_ref(tmpdir, target_ref, target_env)
            if not src_content:
                print(colored(f"Warning: Source file not found. Skipping.", "yellow"))
                continue
            if is_same_branch or not ancestor:
                merged_cfg, changes = merge_ini_two_way(parse_ini_from_string(src_content), parse_ini_from_string(tgt_content), ignore_keys, csv_strategy)
                conflicts = []
            else:
                anc_content = get_file_content_from_ref(tmpdir, ancestor, target_env)
                merged_cfg, changes, conflicts = merge_ini_three_way(parse_ini_from_string(anc_content), parse_ini_from_string(src_content), parse_ini_from_string(tgt_content), ignore_keys, csv_strategy)
            if conflicts: all_conflicts.extend([f"[{target_env}] {c}" for c in conflicts])
            if changes: all_changes.extend([f"[{target_env}] {c}" for c in changes])
            merged_content = config_to_string(merged_cfg)
            if tgt_content != merged_content:
                all_diffs.append(unified_diff_str(tgt_content, merged_content, fromfile=target_env, tofile=target_env))
                if not args.dry_run: (Path(tmpdir) / target_env).write_text(merged_content)
                files_to_commit.append(target_env)
        if all_conflicts:
            print(colored("\nConflicts detected! Aborting.", "red"))
            for c in all_conflicts:
                print(f"  - {c}")
            sys.exit(1)
        if not files_to_commit:
            print(colored("\nNo changes to sync.", "green"))
            sys.exit(0)
        print(colored("\nDiff:", "yellow"))
        print("\n".join(all_diffs))
        if args.dry_run:
            print(colored("\nDRY-RUN: No files written, no MR created.", "yellow"))
            sys.exit(0)
        new_branch = f"{args.branch_prefix}-{random.randint(1000, 9999)}"
        create_and_push_branch(tmpdir, new_branch, args.commit_message)
        mr_title = f"chore(config): Automated configuration sync from {args.source_env}"
        mr_desc = f"Automated sync from `{args.source_project_id}:{args.source_branch}:{args.source_env}` to `{args.target_project_id}:{args.target_branch}`.\n\n**Changes:**\n- " + "\n- ".join(all_changes)
        create_gitlab_mr(gitlab_url, args.target_project_id, token, new_branch, args.target_branch, mr_title, mr_desc)
    finally:
        force_rmtree(tmpdir)

def run_interactive_mode(config_file: str, gitlab_url: str, token: str):
    config = configparser.ConfigParser(interpolation=None); config.optionxform = str; config.read(config_file)
    args = argparse.Namespace(config_file=config_file, dry_run=False, branch_prefix='feature/auto-config-sync', commit_message='chore(config): Automated sync')
    env_choice = select_from_list("Select environment type:", ["non_prod", "prod"])
    proj_section = f'projects_{env_choice}'
    if not config.has_section(proj_section) or not config.items(proj_section):
        print(colored(f"No projects found in '{proj_section}' section. Run with --update.", "red"))
        sys.exit(1)
    projects = {name: id for name, id in config.items(proj_section)}
    
    print(colored("\n--- Source Project ---", "cyan"))
    project_names = list(projects.keys())
    src_proj_name = select_from_list("Select SOURCE project:", project_names)
    args.source_project_id = projects[src_proj_name]
    src_branches = get_repo_branches(gitlab_url, args.source_project_id, token)
    args.source_branch = select_from_list("Select SOURCE branch:", src_branches)
    
    print(colored("\n--- Target Project ---", "cyan"))
    # Allow selecting the same project for source and target
    tgt_proj_name = select_from_list("Select TARGET project:", list(projects.keys()))
    args.target_project_id = projects[tgt_proj_name]
    
    # Get branches for the target project
    tgt_branches = get_repo_branches(gitlab_url, args.target_project_id, token)
    if not tgt_branches:
        print(colored(f"Error: No branches found for target project {tgt_proj_name}", "red"))
        sys.exit(1)
    
    # If same project is selected, show a warning and ensure different branches are selected
    if src_proj_name == tgt_proj_name:
        print(colored("\nNote: Same project selected for source and target. You can sync between different branches or files.", "yellow"))
        
        # If same project, filter out the source branch from target branch selection
        available_target_branches = [b for b in tgt_branches if b != args.source_branch]
        if not available_target_branches:
            print(colored(f"Error: No other branches available in {tgt_proj_name} besides {args.source_branch}", "red"))
            sys.exit(1)
            
        args.target_branch = select_from_list(
            f"Select TARGET branch (different from source branch {args.source_branch}):",
            available_target_branches
        )
    else:
        # Different project, can select any branch
        args.target_branch = select_from_list("Select TARGET branch:", tgt_branches)
    
    # Store project names for better error messages
    src_proj_display = f"{src_proj_name} ({args.source_branch})"
    tgt_proj_display = f"{tgt_proj_name} ({args.target_branch})"

    proj_info = get_project_info(gitlab_url, args.source_project_id, token)
    if not proj_info or not isinstance(proj_info, dict) or 'http_url_to_repo' not in proj_info:
        print(colored(f"Error: Could not retrieve project information for project ID {args.source_project_id}", "red"))
        print(f"Debug - proj_info: {proj_info}")
        sys.exit(1)
    
    repo_url = proj_info['http_url_to_repo']
    if not repo_url:
        print(colored(f"Error: Project {args.source_project_id} has no repository URL", "red"))
        sys.exit(1)
        
    ini_files = get_repo_ini_files(repo_url, args.source_branch, token)
    if not ini_files:
        print(colored(f"No .ini files found in {src_proj_name} on branch {args.source_branch}", "red"))
        sys.exit(1)

    print(colored("\n--- File Selection ---", "cyan"))
    args.source_env = select_from_list("Select SOURCE .ini file:", ini_files)
    args.target_envs = [select_from_list("Select TARGET .ini file:", ini_files)]
    args.source_commit = None # Not supported in interactive mode for simplicity

    print(colored("\n--- Review Sync ---", "cyan"))
    summary = (
        f"  Source Project: {src_proj_name} (ID: {args.source_project_id})\n"
        f"  Target Project: {tgt_proj_name} (ID: {args.target_project_id})\n"
        f"  Source Path:    {args.source_branch} -> {args.source_env}\n"
        f"  Target Path:    {args.target_branch} -> {args.target_envs[0]}"
    )
    print(summary)
    confirm = input("\nProceed with this sync? (y/n): ").lower()
    if confirm == 'y':
        run_sync_operation(args, token)
    else:
        print("Sync cancelled.")

# ------------------------------------------------------------------------------
# SECTION 5: Main Dispatcher
# ------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Advanced INI Configuration Sync Tool", formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--update", action="store_true", help="Update the local cache of GitLab projects and exit.")
    # Non-interactive flags
    parser.add_argument("-tp", "--target-project-id", help="GitLab Project ID to sync TO.")
    parser.add_argument("-sp", "--source-project-id", help="GitLab Project ID to sync FROM. If omitted, uses target-project-id.")
    parser.add_argument("-tb", "--target-branch", help="Target branch for sync.")
    parser.add_argument("-sb", "--source-branch", default="main", help="Source branch name.")
    parser.add_argument("-sc", "--source-commit", help="Source commit hash (overrides source-branch).")
    parser.add_argument("-se", "--source-env", help="Source environment file name (e.g., DEV-ABC.ini).")
    parser.add_argument("-te", "--target-envs", nargs='+', help="List of target environment files (e.g., SIT-ABC.ini).")
    # Common flags
    parser.add_argument("-c", "--config-file", default="sync-config.ini", help="Path to the script's configuration file.")
    parser.add_argument("-d", "--dry-run", action="store_true", help="Show changes without writing files or creating MRs.")
    parser.add_argument("-p", "--branch-prefix", default="feature/auto-config-sync")
    parser.add_argument("-m", "--commit-message", default="chore(config): Automated configuration sync")

    args = parser.parse_args()
    config = configparser.ConfigParser(); config.read(args.config_file)
    gitlab_url = config.get('gitlab', 'url', fallback='https://gitlab.com')
    token = config.get('gitlab', 'token', fallback=None)
    if not token:
        print(colored("ERROR: GitLab token not set in sync-config.ini", "red"))
        sys.exit(1)

    if args.update:
        update_projects_cache(args.config_file, gitlab_url, token)
    elif args.target_project_id and args.target_branch and args.source_env and args.target_envs:
        if not args.source_project_id: args.source_project_id = args.target_project_id
        run_sync_operation(args, token)
    else:
        run_interactive_mode(args.config_file, gitlab_url, token)

if __name__ == "__main__":
    main()
