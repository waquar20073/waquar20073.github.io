#!/usr/bin/env python3
"""
sync-configs.py - Advanced configuration synchronization tool for .ini files.

Supports multiple modes of operation:
- Non-interactive (flag-based) mode for automation.
- A cache update mode to refresh local project lists from GitLab.
"""

import argparse
import configparser
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set

import requests

# ANSI color codes for terminal colors
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    
# Alias for backward compatibility
Fore = Colors
Style = type('Style', (), {'RESET_ALL': Colors.RESET})


def config_to_string(config: configparser.ConfigParser) -> str:
    """Convert a ConfigParser object to a properly formatted INI string.
    
    Args:
        config: The ConfigParser instance to convert
        
    Returns:
        str: The formatted INI content as a string
    """
    output = []
    for section in config.sections():
        output.append(f"[{section}]")
        for key, value in config[section].items():
            output.append(f"{key}={value}")
        output.append("")  # Add empty line between sections
    return "\n".join(output)
    
def parse_ini_from_string(content: str) -> configparser.ConfigParser:
    """Parse INI content from a string.
    
    Args:
        content: The INI content as a string
        
    Returns:
        A ConfigParser instance with the parsed content
    """
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read_string(content)
    return config

# ------------------------------------------------------------------------------
# SECTION 1: Core Utilities & Helpers
# ------------------------------------------------------------------------------

def is_temp_branch(branch: str) -> bool:
    """Check if a branch name matches the temporary branch naming convention."""
    return re.match(r"tmp-sync-\d{8}-\d{6}-[0-9a-f]{8}", branch) is not None
def colored(text: str, color: str) -> str:
    """Colorize text using ANSI escape codes.
    
    Args:
        text: The text to colorize
        color: One of 'red', 'green', 'yellow', 'blue', 'cyan', or 'reset'
        
    Returns:
        Colored text string with reset code
    """
    colors = {
        "red": Colors.RED,
        "green": Colors.GREEN,
        "yellow": Colors.YELLOW,
        "blue": Colors.BLUE,
        "cyan": Colors.CYAN,
        "reset": Colors.RESET
    }
    return f"{colors.get(color.lower(), '')}{text}{Colors.RESET}"

def unified_diff_str(a: str, b: str, fromfile: str, tofile: str) -> str:
    """Generate a unified diff between two strings.
    
    Args:
        a: Original text
        b: Modified text
        fromfile: Label for original file in diff
        tofile: Label for modified file in diff
        
    Returns:
        str: Unified diff as a string
    """
    return "".join(difflib.unified_diff(
        a.splitlines(keepends=True), 
        b.splitlines(keepends=True), 
        fromfile=fromfile, 
        tofile=tofile
    ))

def write_ini_file(file_path: str, config: configparser.ConfigParser):
    """Write INI file with consistent formatting."""
    with open(file_path, 'w') as f:
        # Custom writer to remove spaces around = and after commas
        for section in config.sections():
            f.write(f"[{section}]\n")
            for key, value in config[section].items():
                # Remove spaces after commas in list values
                if ',' in value:
                    value = ','.join([v.strip() for v in value.split(',')])
                f.write(f"{key}={value}\n")
            f.write("\n")

def select_from_list(prompt: str, options: List[Any]) -> Any:
    """Display a numbered menu and get user's selection.
    
    Args:
        prompt: The prompt to display to the user
        options: List of options to display
        
    Returns:
        The selected option from the list
    """
    if not options:
        raise ValueError("No options provided to select from")
        
    print(prompt)
    for i, option in enumerate(options, 1):
        print(f"  {i}) {option}")
        
    while True:
        try:
            choice = input("Enter number: ").strip()
            if not choice:  # Handle empty input
                print(colored("Please enter a number.", "yellow"))
                continue
                
            choice_idx = int(choice) - 1
            if 0 <= choice_idx < len(options):
                return options[choice_idx]
                
            print(colored(f"Please enter a number between 1 and {len(options)}.", "yellow"))
            
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

# When showing the file list, you might want to indicate which files exist in target
def get_repo_ini_files(repo_url: str, branch: str, token: str) -> List[str]:
    """Get list of .ini files in the repository.
    
    Args:
        repo_url: Git repository URL
        branch: Branch to check
        token: GitLab access token
        
    Returns:
        List of .ini file paths
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Clone the repo
        subprocess.run(["git", "clone", "--branch", branch, "--single-branch", repo_url, tmpdir], 
                      check=True, capture_output=True)
        
        # Find all .ini files
        ini_files = []
        for root, _, files in os.walk(tmpdir):
            for file in files:
                if file.endswith('.ini'):
                    # Get relative path from repo root
                    rel_path = os.path.relpath(os.path.join(root, file), tmpdir)
                    ini_files.append(rel_path)
        return ini_files

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

def create_and_push_branch(repo_path: str, new_branch: str, commit_message: str, source_branch: str = None, is_temp_branch: bool = False):
    """Create a new branch, commit changes, and push to remote.
    
    Args:
        repo_path: Path to the git repository
        new_branch: Name of the new branch to create
        commit_message: Commit message
        source_branch: Optional source branch to base the new branch on
        is_temp_branch: Whether this is a temporary branch for same-branch sync
    """
    try:
        # If source_branch is provided, create the new branch from it
        if source_branch:
            subprocess.run(["git", "checkout", source_branch], cwd=repo_path, check=True, capture_output=True, text=True)
        
        # Create and switch to the new branch
        subprocess.run(["git", "checkout", "-b", new_branch], cwd=repo_path, check=True, capture_output=True, text=True)
        
        # Add and commit changes
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_path, check=True, capture_output=True, text=True)
        
        # Push the new branch to remote
        push_cmd = ["git", "push", "-u", "origin", new_branch]
        result = subprocess.run(push_cmd, cwd=repo_path, capture_output=True, text=True)
        
        if result.returncode != 0:
            if "has no upstream branch" in result.stderr:
                # Try to set upstream if not set
                subprocess.run(["git", "push", "--set-upstream", "origin", new_branch], 
                             cwd=repo_path, check=True, capture_output=True, text=True)
            else:
                result.check_returncode()  # Raise error for other issues
        
        print(colored(f"\nSuccessfully created and pushed branch: {new_branch}", "green"))
        return True
        
    except subprocess.CalledProcessError as e:
        print(colored(f"\nError creating/pushing branch {new_branch}:", "red"))
        if e.stderr:
            print(colored(e.stderr.strip(), "red"))
        if e.stdout:
            print(colored(e.stdout.strip(), "yellow"))
        return False

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
    deletions = []
    merged_config = configparser.ConfigParser(interpolation=None)
    merged_config.optionxform = str
    merged_config.read_dict(target_config)
    
    if 'DEFAULT' not in merged_config:
        merged_config.add_section('DEFAULT')
    
    # Handle updates and new keys from source
    if 'DEFAULT' in source_config:
        src = source_config['DEFAULT']
        for key, src_val in src.items():
            if any(fnmatch.fnmatchcase(key, p) for p in ignore_keys):
                continue
                
            tgt_val = merged_config['DEFAULT'].get(key)
            if src_val == tgt_val:
                continue
                
            is_csv = (src_val and ',' in src_val) or (tgt_val and ',' in tgt_val)
            final_val = src_val
            
            if is_csv and csv_strategy == 'union':
                src_list = {s.strip() for s in (src_val or '').split(',') if s.strip()}
                tgt_list = {s.strip() for s in (tgt_val or '').split(',') if s.strip()}
                final_val = ",".join(sorted(list(tgt_list | src_list)))
            
            if final_val != tgt_val:
                merged_config.set('DEFAULT', key, final_val)
                changes.append(f"Update key '{key}': '{tgt_val}' -> '{final_val}'")
    
    # Find keys in target that are not in source (potential deletions)
    if 'DEFAULT' in target_config:
        tgt = target_config['DEFAULT']
        src = source_config.get('DEFAULT', {}) if 'DEFAULT' in source_config else {}
        
        for key in list(tgt.keys()):
            if any(fnmatch.fnmatchcase(key, p) for p in ignore_keys):
                continue
                
            if key not in src:
                deletions.append(f"Remove key '{key}': '{tgt[key]}'")
    
    # If there are deletions, ask for confirmation
    if deletions:
        print("\n" + colored("The following keys will be removed:", "yellow"))
        for d in deletions:
            print(f"  - {d}")
            
        confirm = input("\nDo you want to proceed with these deletions? [y/N] ").strip().lower()
        if confirm == 'y':
            for d in deletions:
                # Extract key from deletion message more reliably
                key_match = re.search(r"Remove key '([^']+)'")
                if not key_match:
                    print(colored(f"Warning: Could not parse key from deletion message: {d}", "yellow"))
                    continue
                key = key_match.group(1)
                del merged_config['DEFAULT'][key]
                changes.append(d)
        else:
            print(colored("Skipping deletions as requested.", "yellow"))
    
    return merged_config, changes

def merge_ini_three_way(ancestor_config, source_config, target_config, ignore_keys, csv_strategy):
    changes, conflicts = [], []
    merged_config = configparser.ConfigParser(interpolation=None); merged_config.optionxform = str
    merged_config.read_dict(target_config)
    anc = ancestor_config['DEFAULT'] if 'DEFAULT' in ancestor_config else {}
    src = source_config['DEFAULT'] if 'DEFAULT' in source_config else {}
    tgt = target_config['DEFAULT'] if 'DEFAULT' in target_config else {}
    if 'DEFAULT' not in merged_config: merged_config.add_section('DEFAULT')
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
            if key in merged_config['DEFAULT']: del merged_config['DEFAULT'][key]; changes.append(f"Removed key '{key}'")
            continue
        if changed:
            merged_config.set('DEFAULT', key, str(final_val)); changes.append(f"Updated key '{key}': '{tgt_val}' -> '{final_val}'")
        elif final_val is not None and key not in merged_config['DEFAULT']:
             merged_config.set('DEFAULT', key, str(final_val)); changes.append(f"Added key '{key}': '{final_val}'")
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
    config = configparser.ConfigParser()
    config.read(args.config_file)
    gitlab_url = config.get('gitlab', 'url', fallback='https://gitlab.com')
    ignore_keys = config.get('ignore', 'keys', fallback='').split()
    csv_strategy = config.get('defaults', 'csv_strategy', fallback='union')
    
    # Get target project info
    target_info = get_project_info(gitlab_url, args.target_project_id, token)
    if not target_info:
        print(colored(f"Error: Could not find target project {args.target_project_id}", "red"))
        sys.exit(1)
    
    # Check if this is a temporary branch for same-branch sync
    is_temp_branch_sync = hasattr(args, 'original_target_branch')
    target_branch = getattr(args, 'original_target_branch', args.target_branch)
    
    tmpdir = tempfile.mkdtemp(prefix="config-sync-")
    try:
        print(f"Cloning target repo {target_info['path_with_namespace']}...")
        
        # Clone the target branch (or source branch for temp branch sync)
        clone_branch = target_branch if not is_temp_branch_sync else args.source_branch
        clone_repo(target_info["http_url_to_repo"], token, clone_branch, tmpdir)
        
        # Set up source and target references
        target_ref = f"origin/{target_branch}"
        is_same_repo = args.source_project_id == args.target_project_id
        
        if not is_same_repo:
            source_info = get_project_info(gitlab_url, args.source_project_id, token)
            print(f"Adding source remote for {source_info['path_with_namespace']}...")
            subprocess.run(["git", "remote", "add", "source", source_info["http_url_to_repo"]], 
                         cwd=tmpdir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "fetch", "source", args.source_branch], 
                         cwd=tmpdir, check=True, capture_output=True, text=True)
            source_ref = args.source_commit or f"source/{args.source_branch}"
        else:
            if not args.source_commit: 
                subprocess.run(["git", "fetch", "origin", args.source_branch], 
                             cwd=tmpdir, check=True, capture_output=True, text=True)
            source_ref = args.source_commit or f"origin/{args.source_branch}"
        
        # For same-branch sync with temp branch, we need to create the target branch
        if is_temp_branch_sync and not args.dry_run:
            subprocess.run(["git", "checkout", "-b", args.target_branch], 
                         cwd=tmpdir, check=True, capture_output=True, text=True)
        
        # Determine merge base
        is_same_branch = is_same_repo and args.source_branch == target_branch
        ancestor = None if is_same_branch else compute_three_way_ancestor(tmpdir, source_ref, target_ref)
        if not is_same_branch and not ancestor:
            print(colored("Warning: No common ancestor. Using 2-way merge.", "yellow"))
        
        all_changes, all_conflicts, all_diffs, files_to_commit = [], [], [], []
        
        for target_env in args.target_envs:
            print(colored(f"\n--- Processing {args.source_env} -> {target_env} ---", "blue"))
            
            # Get file contents
            src_content = get_file_content_from_ref(tmpdir, source_ref, args.source_env)
            tgt_content = get_file_content_from_ref(tmpdir, target_ref, target_env)
            
            if not src_content:
                print(colored(f"Warning: Source file '{args.source_env}' not found. Skipping.", "yellow"))
                continue
                
            # Perform the merge
            if is_same_branch or not ancestor:
                merged_cfg, changes = merge_ini_two_way(
                    parse_ini_from_string(src_content), 
                    parse_ini_from_string(tgt_content) if tgt_content else configparser.ConfigParser(),
                    ignore_keys, 
                    csv_strategy
                )
                conflicts = []
            else:
                anc_content = get_file_content_from_ref(tmpdir, ancestor, target_env)
                merged_cfg, changes, conflicts = merge_ini_three_way(
                    parse_ini_from_string(anc_content) if anc_content else configparser.ConfigParser(),
                    parse_ini_from_string(src_content),
                    parse_ini_from_string(tgt_content) if tgt_content else configparser.ConfigParser(),
                    ignore_keys,
                    csv_strategy
                )
            
            if conflicts:
                all_conflicts.extend([f"[{target_env}] {c}" for c in conflicts])
            if changes:
                all_changes.extend([f"[{target_env}] {c}" for c in changes])
            
            # Write changes if there are any
            merged_content = config_to_string(merged_cfg)
            if not tgt_content or tgt_content != merged_content:
                all_diffs.append(unified_diff_str(
                    tgt_content if tgt_content else "", 
                    merged_content, 
                    fromfile=target_env, 
                    tofile=target_env
                ))
                if not args.dry_run:
                    file_path = Path(tmpdir) / target_env
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(merged_content)
                    files_to_commit.append(target_env)
        
        # Handle conflicts and no-changes cases
        if all_conflicts:
            print(colored("\nConflicts detected! Aborting.", "red"))
            for c in all_conflicts:
                print(f"  - {c}")
            sys.exit(1)
                
        if not files_to_commit:
            print(colored("\nNo changes to sync.", "green"))
            sys.exit(0)
        
        # Show diffs
        print(colored("\nChanges to be committed:", "yellow"))
        print("\n".join(all_diffs))
        
        if args.dry_run:
            print(colored("\nDRY-RUN: No files written, no MR created.", "yellow"))
            sys.exit(0)
        
        # For temp branch sync, we already created the branch earlier
        if not is_temp_branch_sync:
            # Create a new branch for the changes
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            new_branch = f"{args.branch_prefix}-{timestamp}"
            create_and_push_branch(
                repo_path=tmpdir,
                new_branch=new_branch,
                commit_message=args.commit_message,
                source_branch=target_branch
            )
        else:
            new_branch = args.target_branch
            # Add and commit changes
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "commit", "-m", args.commit_message], 
                cwd=tmpdir, 
                check=True, 
                capture_output=True, 
                text=True
            )
            # Push the new branch
            subprocess.run(
                ["git", "push", "-u", "origin", new_branch], 
                cwd=tmpdir, 
                check=True, 
                capture_output=True, 
                text=True
            )
            print(colored(f"\nSuccessfully pushed changes to branch: {new_branch}", "green"))
        
        # Create merge request if needed
        if is_temp_branch_sync or not is_same_branch:
            mr_title = f"chore(config): Automated configuration sync from {args.source_env}"
            mr_desc = (
                f"Automated sync from `{args.source_project_id}:{args.source_branch}:{args.source_env}` "
                f"to `{args.target_project_id}:{target_branch}`.\n\n"
                "**Changes:**\n- " + "\n- ".join(all_changes)
            )
            
            create_gitlab_mr(
                gitlab_url=gitlab_url,
                project_id=args.target_project_id,
                token=token,
                source_branch=new_branch,
                target_branch=target_branch,
                title=mr_title,
                description=mr_desc
            )
    except subprocess.CalledProcessError as e:
        print(colored(f"\nError executing command:", "red"))
        print(colored(f"Command: {e.cmd}", "red"))
        if e.stdout:
            print(colored(f"\nStdout:\n{e.stdout}", "yellow"))
        if e.stderr:
            print(colored(f"\nStderr:\n{e.stderr}", "red"))
        sys.exit(1)
    except Exception as e:
        print(colored(f"\nUnexpected error: {str(e)}", "red"))
        import traceback
        traceback.print_exc()
        sys.exit(1)
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
    
    # Handle branch selection based on project selection
    if src_proj_name == tgt_proj_name:
        print(colored("\nNote: Same project selected for source and target.", "yellow"))
        
        # Show all branches including the source branch
        branch_choice = select_from_list(
            "Select TARGET branch (select same branch to create a temporary branch):",
            tgt_branches + ["[Create new temporary branch]"]
        )
        
        if branch_choice == "[Create new temporary branch]":
            # Generate a timestamp in milliseconds
            timestamp = str(int(datetime.now().timestamp() * 1000))
            
            # Determine branch type based on target branch name
            if args.target_branch == 'develop' or args.target_branch.startswith('feature/'):
                branch_prefix = 'feature/coreb'
            elif args.target_branch.startswith('release/'):
                branch_prefix = 'bugfix/coreb'
            else:
                branch_prefix = 'hotfix/coreb'
                
            args.target_branch = f"{branch_prefix}-{timestamp}-automated-branch"
            print(colored(f"\nWill create and use temporary branch: {args.target_branch}", "cyan"))
        else:
            args.target_branch = branch_choice
            
            # If same branch is selected, we'll create a temporary branch for the changes
            if args.target_branch == args.source_branch:
                # Generate a timestamp in milliseconds
                timestamp = str(int(datetime.now().timestamp() * 1000))
                args.original_target_branch = args.target_branch
                
                # Determine branch type based on target branch name
                if args.target_branch == 'develop' or args.target_branch.startswith('feature/'):
                    branch_prefix = 'feature/coreb'
                elif args.target_branch.startswith('release/'):
                    branch_prefix = 'bugfix/coreb'
                else:
                    branch_prefix = 'hotfix/coreb'
                    
                args.target_branch = f"{branch_prefix}-{timestamp}-automated-branch"
                print(colored(f"\nSame branch selected. Will create temporary branch: {args.target_branch}", "cyan"))
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
