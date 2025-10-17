#!/usr/bin/env python3
"""
sync-configs.py - Advanced configuration synchronization tool for .ini files.

Supports multiple modes of operation:
- Non-interactive (flag-based) mode for automation.
- A cache update mode to refresh local project lists from GitLab.
"""

import argparse
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


def config_to_string(config):
    # Gotta make this dict into an INI string...
    output = []
    
    # Always start with DEFAULT section, even if it's empty
    # (some tools get cranky if it's not there)
    if 'DEFAULT' in config:
        output.append("[DEFAULT]")  # Gotta have the brackets!
        
        # Only add key-values if there are any
        if config['DEFAULT']:
            for key, value in config['DEFAULT'].items():
                output.append(f"{key}={value}")
            
            # Add some breathing room if there's more stuff coming
            if len(config) > 1:
                output.append("")  # Empty line looks nice
    
    # Now do the rest of the sections
    for section, items in config.items():
        if section == 'DEFAULT':
            continue  # Did this one already
            
        # Skip empty sections, they're just taking up space
        if items:
            output.append(f"[{section}]")  # Section header in square brackets
            
            # Add all the key=value pairs
            for key, value in items.items():
                output.append(f"{key}={value}")
                
            # Add a blank line after each section (looks nicer)
            output.append("")
    
    # Oops, don't want that extra newline at the end
    if output and output[-1] == "":
        output = output[:-1]
        
    # Smush it all together with newlines
    return "\n".join(output)
    
def parse_ini_from_string(content: str) -> dict:
    """Parse INI content from a string into a dictionary structure.
    
    Args:
        content: The INI content as a string
        
    Returns:
        A dictionary representing the INI structure with sections and key-value pairs
    """
    result = {}
    current_section = 'DEFAULT'
    result[current_section] = {}
    
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(';') or line.startswith('#'):
            continue
            
        # Handle section headers
        if line.startswith('[') and line.endswith(']'):
            current_section = line[1:-1].strip()
            if current_section not in result:
                result[current_section] = {}
            continue
            
        # Handle key-value pairs
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            result[current_section][key] = value
    
    # Debug output
    print("\n" + "="*50)
    print("DEBUG: Parsed INI content")
    print(f"Content length: {len(content)} characters")
    print(f"Sections found: {list(result.keys())}")
    
    for section, items in result.items():
        print(f"\nSection: [{section}]")
        for key, value in items.items():
            print(f"  {key} = {value[:50]}{'...' if len(str(value)) > 50 else ''}")
    
    if not result:
        print("\nWARNING: No valid INI content found!")
        print("Raw content preview:", content[:200] + ("..." if len(content) > 200 else ""))
    
    return result

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

def write_ini_file(file_path: str, content: str):
    """Write INI file with consistent formatting."""
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

def load_config(config_file: str) -> dict:
    """Load and parse the JSON configuration file.
    
    Args:
        config_file: Path to the JSON config file
        
    Returns:
        A dictionary representing the configuration
    """
    import json
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        # Ensure all required sections exist with defaults
        config.setdefault('gitlab', {})
        config['gitlab'].setdefault('url', 'https://gitlab.com')
        config.setdefault('defaults', {})
        config['defaults'].setdefault('csv_strategy', 'union')
        config.setdefault('ignore', {})
        config['ignore'].setdefault('keys', [])
        
        # Ensure ignore keys is a list
        if isinstance(config['ignore']['keys'], str):
            config['ignore']['keys'] = [k.strip() for k in config['ignore']['keys'].split(',') if k.strip()]
            
        # Debug output
        print("\n=== Loaded Configuration ===")
        print(f"GitLab URL: {config['gitlab'].get('url')}")
        print(f"CSV Strategy: {config['defaults'].get('csv_strategy')}")
        print(f"Ignore Keys: {config['ignore'].get('keys')}")
        
        return config
        
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON config file: {e}")
        raise
    except Exception as e:
        print(f"Error loading config file: {e}")
        raise

def select_from_list(prompt: str, options: List[Any], allow_commit_hash: bool = False):
    """Display a numbered menu and get user's selection.
    
    Args:
        prompt: The prompt to display to the user
        options: List of options to display
        allow_commit_hash: If True, allows direct commit hash input
        
    Returns:
        The selected option from the list or the entered commit hash
    """
    if not options and not allow_commit_hash:
        raise ValueError("No options provided to select from")
        
    print(f"\n{prompt}")
    
    # Display options with numbers
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    
    # Add option for commit hash if allowed
    if allow_commit_hash:
        print(f"  {len(options) + 1}. Enter commit hash")
    
    while True:
        try:
            choice = input("\nEnter your choice (number): ").strip()
            if not choice:
                continue
                
            # Check if it's a valid number
            try:
                idx = int(choice) - 1
                
                # If they selected the commit hash option
                if allow_commit_hash and idx == len(options):
                    while True:
                        commit_hash = input("\nEnter commit hash (7-40 hex characters): ").strip()
                        if re.match(r'^[0-9a-f]{7,40}$', commit_hash, re.IGNORECASE):
                            return commit_hash
                        print(colored("Invalid commit hash. Must be 7-40 hex characters.", "red"))
                
                # If they selected a regular option
                if 0 <= idx < len(options):
                    return options[idx]
                    
                print(colored(f"Please enter a number between 1 and {len(options) + (1 if allow_commit_hash else 0)}", "red"))
                
            except ValueError:
                print(colored(f"Please enter a valid number between 1 and {len(options) + (1 if allow_commit_hash else 0)}", "red"))
            
        except KeyboardInterrupt:
            print("\nOperation cancelled by user.")
            sys.exit(1)

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

def clone_repo(project_id: str, token: str, branch: str, target_dir: str, gitlab_url: str = 'https://gitlab.com') -> Optional[str]:
    """Clone a git repository to a target directory.
    
    Args:
        project_id: GitLab project ID or path
        token: GitLab access token
        branch: Branch to clone
        target_dir: Directory to clone into
        gitlab_url: Base URL of the GitLab instance (default: https://gitlab.com)
        
    Returns:
        Path to the cloned repository or None if failed
    """
    try:
        # Clean up target directory if it exists
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
            
        # Get the repository URL
        base_url = gitlab_url.rstrip('/')
        # For self-hosted GitLab, the project_id might be URL-encoded
        # We need to ensure it's properly formatted for the git clone URL
        if not project_id.startswith(('http://', 'https://')):
            # Handle project paths (e.g., 'group/project' or 'group%2Fproject')
            if '%2F' not in project_id and '/' not in project_id:
                # If it's just a project ID (number), we need to get the full path first
                try:
                    import urllib.parse
                    import requests
                    
                    headers = {
                        'PRIVATE-TOKEN': token,
                        'Content-Type': 'application/json'
                    }
                    api_url = f"{base_url}/api/v4/projects/{urllib.parse.quote(project_id, safe='')}"
                    
                    # Make a direct request to the GitLab API
                    response = requests.get(api_url, headers=headers)
                    response.raise_for_status()
                    project_info = response.json()
                    
                    if isinstance(project_info, dict) and 'path_with_namespace' in project_info:
                        project_path = project_info['path_with_namespace']
                        print(colored(f"Using repository path: {project_path}", "green"))
                    else:
                        print(colored(f"Unexpected API response format. Falling back to project_id.", "yellow"))
                        project_path = project_id
                        
                except Exception as e:
                    print(colored(f"Error getting project info: {e}", "red"))
                    print(colored(f"Falling back to using project_id as path", "yellow"))
                    project_path = project_id
            else:
                project_path = project_id
                
            # Ensure the project path is properly URL-encoded for the git clone URL
            project_path = project_path.replace(' ', '%20')
            repo_url = f"{base_url.replace('://', f'://oauth2:{token}@')}/{project_path}.git"
        else:
            # If it's already a full URL, just add the token
            repo_url = project_id.replace('://', f'://oauth2:{token}@')
        
        # Check if branch is a commit hash (7-40 hex characters)
        is_commit_hash = re.match(r'^[0-9a-f]{7,40}$', branch, re.IGNORECASE)
        
        if is_commit_hash:
            # For commit hashes, first clone the default branch (without --single-branch)
            # then checkout the specific commit
            cmd = [
                "git", "clone",
                "--depth", "50",  # Get some history to ensure we have the commit
                repo_url,
                target_dir
            ]
            
            print(colored(f"Cloning repository to {target_dir}...", "cyan"))
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                # Now checkout the specific commit
                checkout_cmd = [
                    "git", "checkout", branch
                ]
                result = subprocess.run(checkout_cmd, cwd=target_dir, capture_output=True, text=True)
        else:
            # For branch names, use the original behavior
            cmd = [
                "git", "clone",
                "--branch", branch,
                "--single-branch",
                "--depth", "1",
                repo_url,
                target_dir
            ]
            print(colored(f"Cloning {branch} branch to {target_dir}...", "cyan"))
            result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            print(colored(f"Error cloning repository: {error_msg}", "red"))
            return None
            
        return target_dir
        
    except Exception as e:
        print(colored(f"Error in clone_repo: {str(e)}", "red"))
        return None

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
    # Ensure gitlab_url doesn't end with a slash
    gitlab_url = gitlab_url.rstrip('/')
    
    # URL-encode the project_id if it's a path
    import urllib.parse
    if not project_id.isdigit() and '/' in project_id:
        project_id = urllib.parse.quote(project_id, safe='')
    
    api_url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests"
    headers = {
        "PRIVATE-TOKEN": token, 
        "Content-Type": "application/json"
    }
    
    payload = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": True,
        "squash": False
    }
    
    print(colored(f"\nCreating merge request from {source_branch} to {target_branch}...", "cyan"))
    print(colored(f"API URL: {api_url}", "cyan"))
    
    try:
        print(colored("\n=== MR Creation Details ===", "cyan"))
        print(colored(f"API URL: {api_url}", "cyan"))
        print(colored(f"Headers: {json.dumps(headers, indent=2)}", "cyan"))
        print(colored(f"Payload: {json.dumps(payload, indent=2)}", "cyan"))
        
        response = requests.post(
            api_url, 
            headers=headers, 
            json=payload, 
            timeout=30
        )
        
        # Print response for debugging
        print(colored(f"\n=== MR Creation Response ===", "cyan"))
        print(colored(f"Status Code: {response.status_code}", "cyan"))
        if response.text:
            try:
                print(colored(f"Response JSON: {json.dumps(response.json(), indent=2)}", "cyan"))
            except:
                print(colored(f"Response Text: {response.text}", "cyan"))
        
        response.raise_for_status()
        
        mr_data = response.json()
        mr_url = mr_data.get('web_url', 'URL not available')
        mr_iid = mr_data.get('iid', 'N/A')
        print(colored(f"\n✓ Successfully created merge request!", "green"))
        print(colored(f"MR IID: {mr_iid}", "green"))
        print(colored(f"MR URL: {mr_url}", "green"))
        return True
        
    except requests.exceptions.RequestException as e:
        print(colored("\n!!! MR Creation Failed !!!", "red"))
        error_msg = [f"Error: {str(e)}"]
        
        if hasattr(e, 'response') and e.response is not None:
            error_msg.append(f"Status Code: {e.response.status_code}")
            try:
                error_msg.append(f"Response: {e.response.text}")
                if e.response.status_code == 400:
                    error_msg.append("Possible issues:")
                    error_msg.append("- Source and target branches are the same")
                    error_msg.append("- Merge request already exists")
                    error_msg.append("- Invalid project ID or insufficient permissions")
            except:
                pass
                
        error_msg.append("\nPlease check:")
        error_msg.append(f"1. The project ID is correct: {project_id}")
        error_msg.append(f"2. The source branch exists: {source_branch}")
        error_msg.append(f"3. The target branch exists: {target_branch}")
        error_msg.append("4. Your GitLab token has sufficient permissions")
        
        print(colored("\n".join(error_msg), "red"))
        return False

# ------------------------------------------------------------------------------
# SECTION 3: Merge Logic
# ------------------------------------------------------------------------------

def merge_ini_two_way(source_config, target_config, ignore_keys, csv_strategy):
    print("\n" + "="*50)
    print("DEBUG: Starting merge_ini_two_way")
    print("Source config sections:", list(source_config.keys()))
    print("Target config sections:", list(target_config.keys()))
    print("Ignored keys:", ignore_keys)  # Debug output for ignored keys
    
    changes = []
    deletions = []
    merged_config = {}
    
    # Ensure DEFAULT section exists in all configs
    source_config.setdefault('DEFAULT', {})
    target_config.setdefault('DEFAULT', {})
    merged_config['DEFAULT'] = target_config['DEFAULT'].copy()
    
    # Convert ignore_keys to a list if it's a string
    if isinstance(ignore_keys, str):
        ignore_keys = [ignore_keys] if ignore_keys else []
    
    # Get source and target sections (default to empty dict if not found)
    src = source_config.get('DEFAULT', {})
    tgt = target_config.get('DEFAULT', {})
    
    # Copy target config to merged config first
    if tgt:
        merged_config['DEFAULT'].update(tgt)
    
    # Track all keys for better reporting
    all_keys = set(src.keys()) | set(tgt.keys())
    updated_keys = []
    ignored_keys = []
    unchanged_keys = []
    
    # Process updates and new keys from source
    for key, src_val in src.items():
        # Check if key should be ignored
        key_ignored = False
        for pattern in ignore_keys:
            if key == pattern or fnmatch.fnmatchcase(key, pattern):
                ignored_keys.append((key, pattern))
                key_ignored = True
                break
        if key_ignored:
            continue
            
        tgt_val = tgt.get(key, '')
        if src_val == tgt_val:
            continue
            
        is_csv = (src_val and ',' in src_val) or (tgt_val and ',' in tgt_val)
        final_val = src_val
        
        if is_csv and csv_strategy == 'union':
            # Preserve original order while removing duplicates
            seen = set()
            src_list = []
            for item in (src_val or '').split(','):
                item = item.strip()
                if item and item not in seen:
                    seen.add(item)
                    src_list.append(item)
            
            # Add target values that aren't in source
            for item in (tgt_val or '').split(','):
                item = item.strip()
                if item and item not in seen:
                    seen.add(item)
                    src_list.append(item)
            
            final_val = ",".join(src_list)
        
        if final_val != tgt_val:
            merged_config['DEFAULT'][key] = final_val
            changes.append(f"Update key '{key}': '{tgt_val}' -> '{final_val}'")
            updated_keys.append(key)
        else:
            unchanged_keys.append(key)
    
    # Find keys in target that are not in source (potential deletions)
    for key in list(tgt.keys()):  # Create a list of keys to avoid modifying dict during iteration
        if any(fnmatch.fnmatchcase(key, p) for p in ignore_keys):
            continue
            
        if key not in src:
            value = tgt[key]
            deletions.append(f"Remove key '{key}': '{value}'")
            
            # Remove the key from merged config if it exists
            if key in merged_config['DEFAULT']:
                del merged_config['DEFAULT'][key]
    
    # Print key processing summary
    print("\n" + "="*50)
    print(colored("KEY PROCESSING SUMMARY", "cyan"))
    
    if ignored_keys:
        print("\n" + colored("IGNORED KEYS (not modified):", "yellow"))
        for key, pattern in ignored_keys:
            print(f"  - {key} (matches ignore pattern: {pattern})")
    
    if updated_keys:
        print("\n" + colored("UPDATED KEYS:", "green"))
        for key in sorted(updated_keys):
            old_val = tgt.get(key, '')
            new_val = src.get(key, '')
            print(f"  - {key}: {old_val} -> {new_val}")
    
    if unchanged_keys:
        print("\n" + colored("UNCHANGED KEYS:", "blue"))
        for key in sorted(unchanged_keys):
            print(f"  - {key}")
    
    # If there are deletions, ask for confirmation
    if deletions:
        print("\n" + colored("The following keys will be removed:", "yellow"))
        for d in deletions:
            print(f"  - {d}")
            
        confirm = input("\nDo you want to proceed with these deletions? [y/N] ").strip().lower()
        if confirm == 'y':
            for d in deletions:
                # Extract key from deletion message
                key_match = re.search(r"Remove key '([^']+)'", d)
                if not key_match:
                    print(colored(f"Warning: Could not parse key from deletion message: {d}", "yellow"))
                    continue
                key = key_match.group(1)
                if key in merged_config['DEFAULT']:
                    del merged_config['DEFAULT'][key]
                    changes.append(d)
        else:
            print(colored("Skipping deletions as requested.", "yellow"))
    
    return merged_config, changes

def merge_ini_three_way(ancestor_config, source_config, target_config, ignore_keys, csv_strategy):
    changes, conflicts = [], []
    merged_config = {}
    
    # Ensure DEFAULT section exists in all configs
    ancestor_config.setdefault('DEFAULT', {})
    source_config.setdefault('DEFAULT', {})
    target_config.setdefault('DEFAULT', {})
    merged_config['DEFAULT'] = target_config['DEFAULT'].copy()
    
    # Convert ignore_keys to a list if it's a string
    if isinstance(ignore_keys, str):
        ignore_keys = [ignore_keys] if ignore_keys else []
    
    # Get all sections as dictionaries
    anc = ancestor_config.get('DEFAULT', {})
    src = source_config.get('DEFAULT', {})
    tgt = target_config.get('DEFAULT', {})
    
    # Copy target config to merged config
    merged_config['DEFAULT'].update(tgt)
    
    all_keys = set(anc.keys()) | set(src.keys()) | set(tgt.keys())
    for key in all_keys:
        # Skip ignored keys
        if ignore_keys and any(fnmatch.fnmatchcase(key, p) for p in ignore_keys):
            print(f"DEBUG: Skipping ignored key in three-way merge: {key}")
            continue
            
        anc_val = anc.get(key)
        src_val = src.get(key)
        tgt_val = tgt.get(key)
        
        src_changed = src_val != anc_val
        tgt_changed = tgt_val != anc_val
        
        # Check for conflicts
        if src_changed and tgt_changed and src_val != tgt_val:
            conflicts.append(f"Conflict on key '{key}': source='{src_val}', target='{tgt_val}'")
            continue
            
        final_val = tgt_val
        changed = False
        
        if src_changed and src_val != tgt_val:
            is_csv = (src_val and ',' in src_val) or (tgt_val and ',' in tgt_val)
            if is_csv and csv_strategy == 'union':
                # Preserve order while handling CSV values
                seen = set()
                result = []
                
                # First add all target values
                for item in (tgt_val or '').split(','):
                    item = item.strip()
                    if item and item not in seen:
                        seen.add(item)
                        result.append(item)
                
                # Then add source values that aren't in target or ancestor
                anc_set = {s.strip() for s in (anc_val or '').split(',') if s.strip()}
                for item in (src_val or '').split(','):
                    item = item.strip()
                    if item and item not in seen and item not in anc_set:
                        seen.add(item)
                        result.append(item)
                
                final_val = ",".join(result)
                changed = final_val != tgt_val
            else:
                final_val = src_val
                changed = True
                
        # Handle key removal
        if src_val is None and anc_val is not None and not tgt_changed:
            if key in merged_config['DEFAULT']:
                del merged_config['DEFAULT'][key]
                changes.append(f"Removed key '{key}'")
            continue
            
        # Apply changes
        if changed:
            merged_config['DEFAULT'][key] = str(final_val)
            changes.append(f"Updated key '{key}': '{tgt_val}' -> '{final_val}'")
        elif final_val is not None and key not in merged_config['DEFAULT']:
            merged_config['DEFAULT'][key] = str(final_val)
            changes.append(f"Added key '{key}': '{final_val}'")
    return merged_config, changes, conflicts

# ------------------------------------------------------------------------------
# SECTION 4: Application Modes
# ------------------------------------------------------------------------------

def update_projects_cache(config_file: str, gitlab_url: str, token: str):
    """Update the local cache of GitLab projects."""
    print(colored("\n=== Updating Project Cache ===", "cyan"))
    
    import json
    
    # Read existing config
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        print(colored(f"Error reading config file: {e}", "red"))
        return
    
    # Ensure required sections exist
    config.setdefault('gitlab', {})
    config.setdefault('defaults', {})
    config.setdefault('projects_prod', {})
    config.setdefault('projects_non_prod', {})
    
    # Get group IDs with defaults
    gitlab_config = config.get('gitlab', {})
    prod_group_id = gitlab_config.get('prod_group_id')
    non_prod_group_id = gitlab_config.get('non_prod_group_id')
    
    if not prod_group_id or not non_prod_group_id:
        print(colored("ERROR: prod_group_id and non_prod_group_id must be set in the gitlab section of the config file", "red"))
        return
    
    # Get projects from GitLab
    print("Fetching production projects...")
    prod_projects = get_group_projects(gitlab_url, prod_group_id, token)
    print(f"Found {len(prod_projects)} production projects")
    
    print("\nFetching non-production projects...")
    non_prod_projects = get_group_projects(gitlab_url, non_prod_group_id, token)
    print(f"Found {len(non_prod_projects)} non-production projects")
    
    # Update project lists
    config['projects_prod'] = {proj['name_with_namespace']: str(proj['id']) for proj in prod_projects}
    config['projects_non_prod'] = {proj['name_with_namespace']: str(proj['id']) for proj in non_prod_projects}
    
    # Create backup of original config
    backup_file = f"{config_file}.bak"
    shutil.copy2(config_file, backup_file)
    print(f"Created backup of config at: {backup_file}")
    
    # Write the updated config back to file
    try:
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, sort_keys=True)
        
        print(colored("\nProject cache updated successfully!", "green"))
        print(colored("Changes made to the config file:", "cyan"))
        print(f"  - Updated project lists in projects_prod and projects_non_prod")
        print(colored("\nOriginal config was backed up to:", "yellow") + f" {backup_file}")
        
    except Exception as e:
        print(colored(f"\nError writing config file: {e}", "red"))
        print(colored("Original config was backed up to:", "yellow") + f" {backup_file}")


def run_interactive_mode(config_file: str, gitlab_url: str, token: str, config: dict):
    """Run the tool in interactive mode."""
    print(colored("\n=== Interactive Mode ===", "cyan"))
    if 'gitlab' not in config:
        print(colored("ERROR: [gitlab] section not in config!", "red"))
        sys.exit(1)
    
    # Save non-project sections to preserve them
    preserved_sections = {}
    for section in config:
        if not section.startswith('projects_') and section != 'gitlab' and section != 'defaults':
            preserved_sections[section] = config[section].copy()
    
    gitlab_config = config.get('gitlab', {})
    
    for env in ['prod', 'non_prod']:
        group_id = gitlab_config.get(f'{env}_group_id')
        if not group_id:
            continue
            
        section = f'projects_{env}'
        print(f"Fetching projects for '{env}' group ({group_id})...")
        projects = get_group_projects(gitlab_url, group_id, token)
        
        # Update the projects section
        config[section] = {}
        for proj in sorted(projects, key=lambda p: p['name_with_namespace']):
            config[section][proj['name_with_namespace']] = str(proj['id'])
    
    # Restore preserved sections
    for section, values in preserved_sections.items():
        config[section] = values
    
    # Create backup of original config
    backup_file = f"{config_file}.bak"
    shutil.copy2(config_file, backup_file)
    print(f"Created backup of config at: {backup_file}")
    
    # Write the updated config back to file
    with open(config_file, 'w', encoding='utf-8') as f:
        f.write(config_to_string(config))
    
    print(colored("\nProject cache updated successfully!", "green"))
    print(colored("Changes made to the config file:", "cyan"))
    print(f"  - Updated project lists in [projects_prod] and [projects_non_prod]")
    print(f"  - Preserved all other sections including [ignore] and [defaults]")
    print(colored("\nOriginal config was backed up to:", "yellow") + f" {backup_file}")

def run_sync_operation(args: argparse.Namespace, token: str, config: dict):
    """Run the sync operation between source and target branches."""
    # Create separate temp directories for source and target
    try:
        with tempfile.TemporaryDirectory(prefix="src_") as src_tmpdir, \
             tempfile.TemporaryDirectory(prefix="tgt_") as tgt_tmpdir:
            
            print(colored("\n=== Starting Sync Operation ===", "cyan"))
            print(f"Source branch: {args.source_branch}")
            print(f"Target branch: {args.target_branch}")
            print(f"Source file: {args.source_env}")
            
            # Get config values with fallbacks
            gitlab_url = config.get('gitlab', {}).get('url', 'https://gitlab.com')
            
            # Get ignore keys from config
            ignore_keys = config.get('ignore', {}).get('keys', [])
            
            # Ensure ignore_keys is a list
            if isinstance(ignore_keys, str):
                ignore_keys = [k.strip() for k in ignore_keys.split(',') if k.strip()]
            
            print(colored("\n=== Ignore Keys Processing ===", "cyan"))
            print(f"Ignore keys: {ignore_keys}")
            print(f"Type: {type(ignore_keys).__name__}")
            
            csv_strategy = config.get('defaults', {}).get('csv_strategy', 'union')
        
        # Check if source is a commit hash
        is_commit_hash = re.match(r'^[0-9a-f]{7,40}$', args.source_branch, re.IGNORECASE)
        
        if is_commit_hash:
            # For commit hashes, we'll use git show to get the file content later
            src_repo_path = clone_repo(
                project_id=args.source_project_id,
                token=token,
                branch='develop',  # Clone develop branch as base
                target_dir=src_tmpdir,
                gitlab_url=gitlab_url
            )
            if not src_repo_path:
                print(colored("Failed to clone source repository", "red"))
                return
                
            # Check if the commit exists in the repository
            result = subprocess.run(
                ["git", "show", f"{args.source_branch}:{args.source_env}"],
                cwd=src_repo_path,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print(colored(f"Error: Commit {args.source_branch} not found or file doesn't exist at that commit", "red"))
                return
        else:
            # For branch names, use the normal clone
            print(colored("\n=== Cloning Source Repository ===", "cyan"))
            src_repo_path = clone_repo(
                project_id=args.source_project_id,
                token=token,
                branch=args.source_branch,
                target_dir=src_tmpdir,
                gitlab_url=gitlab_url
            )
            if not src_repo_path:
                print(colored("Failed to clone source repository", "red"))
                return
            
        # Clone target branch
        print(colored("\n=== Cloning Target Repository ===", "cyan"))
        tgt_repo_path = clone_repo(
            project_id=args.target_project_id,
            token=token,
            branch=args.target_branch,
            target_dir=tgt_tmpdir,
            gitlab_url=gitlab_url
        )
        if not tgt_repo_path:
            print(colored("Failed to clone target repository", "red"))
            return
            
        # Get source content
        if is_commit_hash:
            # For commit hashes, use git show to get the file content at that specific commit
            result = subprocess.run(
                ["git", "show", f"{args.source_branch}:{args.source_env}"],
                cwd=src_repo_path,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print(colored(f"Error: Could not read file '{args.source_env}' at commit {args.source_branch}", "red"))
                print(f"Error: {result.stderr}")
                return
                
            src_content = result.stdout
        else:
            # For branches, read the file directly from the filesystem
            src_file = os.path.join(src_repo_path, args.source_env)
            if not os.path.exists(src_file):
                print(colored(f"Error: Source file '{args.source_env}' not found in branch '{args.source_branch}'", "red"))
                print(f"Files in source repo: {os.listdir(src_repo_path)}")
                return
                
            with open(src_file, 'r') as f:
                src_content = f.read()
            
        # Get target file path
        tgt_file = os.path.join(tgt_repo_path, args.target_envs[0])
        
        # If target file doesn't exist, create it
        if not os.path.exists(tgt_file):
            os.makedirs(os.path.dirname(tgt_file), exist_ok=True)
            with open(tgt_file, 'w') as f:
                f.write("")
            print(colored(f"Created new target file: {args.target_envs[0]}", "yellow"))
            
        # Get target content
        with open(tgt_file, 'r') as f:
            tgt_content = f.read()
            
        # Parse INI files
        src_config = parse_ini_from_string(src_content)
        tgt_config = parse_ini_from_string(tgt_content)
        
        # Perform the merge
        print(colored("\n=== Performing Merge ===", "cyan"))
        
        # Check if this is a temporary branch for same-branch sync
        is_temp_branch_sync = hasattr(args, 'original_target_branch')
        target_branch = getattr(args, 'original_target_branch', args.target_branch)
        
        # Determine if we're doing a two-way or three-way merge
        is_same_branch = args.source_branch == target_branch
        ancestor = None if is_same_branch else compute_three_way_ancestor(
            tgt_repo_path, args.source_branch, target_branch
        )
        
        if not is_same_branch and not ancestor:
            print(colored("Warning: No common ancestor found, falling back to two-way merge", "yellow"))
        
        # Perform the merge
        if is_same_branch or not ancestor:
            merged_config, changes = merge_ini_two_way(
                src_config, tgt_config, ignore_keys, csv_strategy
            )
            conflicts = []
        else:
            # Get ancestor content for three-way merge
            anc_file = os.path.join(src_repo_path, args.source_env)
            anc_content = ""
            if os.path.exists(anc_file):
                with open(anc_file, 'r', encoding='utf-8') as f:
                    anc_content = f.read()
            
            # Use empty dict if no ancestor content
            anc_config = parse_ini_from_string(anc_content) if anc_content else {'DEFAULT': {}}
            merged_config, changes, conflicts = merge_ini_three_way(
                anc_config, src_config, tgt_config, ignore_keys, csv_strategy
            )
        
        # Handle conflicts
        if conflicts:
            print(colored("\n=== Merge Conflicts ===", "red"))
            for conflict in conflicts:
                print(colored(f"Conflict: {conflict}", "red"))
            print(colored("\nPlease resolve conflicts manually", "red"))
            return
        
        # Write merged config back to target file
        with open(tgt_file, 'w', encoding='utf-8') as f:
            f.write(config_to_string(merged_config))
            
        print(colored("\n=== Merge Successful ===", "green"))
        if changes:
            print(colored("Changes:", "cyan"))
            for change in changes:
                print(f"- {change}")
        else:
            print(colored("No changes were made", "yellow"))
            return
            
        # Create and push branch with changes
        print(colored("\n=== Creating and Pushing Changes ===", "cyan"))
        
        # Change to target repo directory
        os.chdir(tgt_repo_path)
        
        # Create a new branch for the changes with the required naming pattern
        timestamp = str(int(datetime.now().timestamp() * 1000))
        if args.target_branch.startswith('release/'):
            new_branch = f"bugfix/coreb-{timestamp}-config-sync-automation"
        elif args.target_branch == 'develop' or args.target_branch.startswith('feature/'):
            new_branch = f"feature/coreb-{timestamp}-config-sync-automation"
        else:
            new_branch = f"hotfix/coreb-{timestamp}-config-sync-automation"
            
        print(colored(f"Creating new branch: {new_branch}", "cyan"))
        
        subprocess.run(["git", "checkout", "-b", new_branch], check=True)
        subprocess.run(["git", "add", args.target_envs[0]], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"chore: Update {args.target_envs[0]} from {args.source_branch}"],
            check=True
        )
        
        # Push the new branch
        print(colored(f"Pushing changes to {new_branch}...", "cyan"))
        subprocess.run(["git", "push", "-u", "origin", new_branch], check=True)
        
        # Wait for 5 seconds to ensure the branch is fully pushed and propagated
        print(colored("\nWaiting 5 seconds to ensure branch is fully pushed...", "yellow"))
        import time
        time.sleep(5)
        
        # Verify the branch exists on remote with retries
        max_retries = 3
        retry_delay = 5  # seconds
        branch_found = False
        
        for attempt in range(max_retries):
            print(colored(f"\nVerifying branch exists on remote (attempt {attempt + 1}/{max_retries})...", "cyan"))
            verify_cmd = ["git", "ls-remote", "--heads", "origin", new_branch]
            result = subprocess.run(verify_cmd, capture_output=True, text=True)
            
            if new_branch in result.stdout:
                branch_found = True
                print(colored("✓ Branch verified on remote repository", "green"))
                break
                
            if attempt < max_retries - 1:
                print(colored(f"Branch not found yet, waiting {retry_delay} seconds before retry...", "yellow"))
                time.sleep(retry_delay)
        
        if not branch_found:
            print(colored("\n❌ Error: Branch not found on remote after multiple attempts!", "red"))
            print(colored(f"Please check if the branch '{new_branch}' exists on the remote repository.", "red"))
            print(colored("You may need to create the merge request manually.", "red"))
            return
        
        # Create merge request if needed
        if hasattr(args, 'create_mr') and args.create_mr:
            print(colored("\n=== Creating Merge Request ===", "cyan"))
            print(colored(f"Source branch: {new_branch}", "cyan"))
            print(colored(f"Target branch: {args.target_branch}", "cyan"))
            print(colored(f"Project ID: {args.target_project_id}", "cyan"))
            
            try:
                mr_created = create_gitlab_mr(
                    gitlab_url=gitlab_url,
                    project_id=args.target_project_id,
                    token=token,
                    source_branch=new_branch,
                    target_branch=args.target_branch,
                    title=f"chore: Update {args.target_envs[0]} from {args.source_branch}",
                    description="Automated configuration sync"
                )
                if not mr_created:
                    print(colored("\nFailed to create merge request. You can create it manually with:", "yellow"))
                    print(colored(f"Source branch: {new_branch}", "yellow"))
                    print(colored(f"Target branch: {args.target_branch}\n", "yellow"))
            except Exception as e:
                print(colored(f"\nError creating merge request: {str(e)}", "red"))
                print(colored("\nYou can create the merge request manually with:", "yellow"))
                print(colored(f"Source branch: {new_branch}", "yellow"))
                print(colored(f"Target branch: {args.target_branch}\n", "yellow"))
    except subprocess.CalledProcessError as e:
        print(colored(f"\nError executing command:", "red"))
        print(colored(f"Command: {e.cmd}", "red"))
        if e.stdout:
            print(colored(f"\nStdout:\n{e.stdout}", "yellow"))
        if e.stderr:
            print(colored(f"\nStderr:\n{e.stderr}", "red"))
        print(colored("\nTemporary directories will be automatically cleaned up by the system.", "yellow"))
        sys.exit(1)
    except Exception as e:
        print(colored(f"\nUnexpected error: {str(e)}", "red"))
        import traceback
        traceback.print_exc()
        print(colored("\nTemporary directories will be automatically cleaned up by the system.", "yellow"))
        sys.exit(1)

def run_interactive_mode(gitlab_url: str, token: str, config: dict):
    # Initialize common args with MR creation enabled by default
    args = argparse.Namespace(
        config_file=config.get('config_file', 'sync-config.json'),
        dry_run=False,
        branch_prefix='feature/auto-config-sync',
        commit_message='chore(config): Automated sync',
        token=token or config.get('gitlab', {}).get('token'),
        create_mr=True  # Enable MR creation by default in interactive mode
    )
    
    # Select source environment and project
    print(colored("\n--- Source Environment & Project ---", "cyan"))
    src_env = select_from_list("Select SOURCE environment type:", ["non_prod", "prod"])
    src_proj_section = f'projects_{src_env}'
    
    if src_proj_section not in config or not config[src_proj_section]:
        print(colored(f"No projects found in '{src_proj_section}' section. Run with --update.", "red"))
        sys.exit(1)
        
    projects = config[src_proj_section]
    project_names = list(projects.keys())
    
    # Select source project and branch or commit
    src_proj_name = select_from_list("Select SOURCE project:", project_names)
    args.source_project_id = projects[src_proj_name]
    src_branches = get_repo_branches(gitlab_url, args.source_project_id, token)
    
    # Allow entering a commit hash directly
    print("\n" + "="*80)
    print(colored("You can either:", "cyan"))
    print("1. Select a branch from the list below")
    print("2. Enter a commit hash (7-40 hex characters)")
    print("="*80 + "\n")
    
    args.source_branch = select_from_list(
        "Select SOURCE branch (or enter commit hash):", 
        src_branches, 
        allow_commit_hash=True
    )
    
    # Select target environment
    print(colored("\n--- Target Environment & Project ---", "cyan"))
    tgt_env = select_from_list("Select TARGET environment type:", ["non_prod", "prod"])
    tgt_proj_section = f'projects_{tgt_env}'
    
    if tgt_proj_section not in config or not config[tgt_proj_section]:
        print(colored(f"No projects found in '{tgt_proj_section}' section. Run with --update.", "red"))
        sys.exit(1)
        
    tgt_projects = config[tgt_proj_section]
    tgt_project_names = list(tgt_projects.keys())
    
    # Select target project and branch
    tgt_proj_name = select_from_list("Select TARGET project:", tgt_project_names)
    args.target_project_id = tgt_projects[tgt_proj_name]
    
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
            
            # Determine branch type based on source branch name
            if args.source_branch.startswith('release/'):
                args.target_branch = f"bugfix/coreb-{timestamp}-config-sync-automation"
            elif args.source_branch == 'develop' or args.source_branch.startswith('feature/'):
                args.target_branch = f"feature/coreb-{timestamp}-config-sync-automation"
            else:
                args.target_branch = f"hotfix/coreb-{timestamp}-config-sync-automation"
            print(colored(f"\nWill create and use temporary branch: {args.target_branch}", "cyan"))
        else:
            args.target_branch = branch_choice
            
            # If same branch is selected, create a temporary branch
            if args.target_branch == args.source_branch:
                timestamp = str(int(datetime.now().timestamp() * 1000))
                args.original_target_branch = args.target_branch
                
                # Determine branch type based on target branch name
                if args.target_branch.startswith('release/'):
                    args.target_branch = f"bugfix/coreb-{timestamp}-config-sync-automation"
                elif args.target_branch == 'develop' or args.target_branch.startswith('feature/'):
                    args.target_branch = f"feature/coreb-{timestamp}-config-sync-automation"
                else:
                    args.target_branch = f"hotfix/coreb-{timestamp}-config-sync-automation"
                print(colored(f"\nSame branch selected. Will create temporary branch: {args.target_branch}", "cyan"))
    else:
        # For different projects, allow selecting a branch or entering a commit hash
        print("\n" + "="*80)
        print(colored("You can either:", "cyan"))
        print("1. Select a target branch from the list below")
        print("2. Enter a commit hash (7-40 hex characters)")
        print("="*80 + "\n")
        
        args.target_branch = select_from_list(
            "Select TARGET branch (or enter commit hash):", 
            tgt_branches, 
            allow_commit_hash=True
        )
    
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

    # Ask if user wants to create a merge request
    print(colored("\n--- Merge Request Settings ---", "cyan"))
    create_mr = input("Create a merge request for these changes? [Y/n]: ").strip().lower()
    args.create_mr = create_mr != 'n'

    print(colored("\n--- Review Sync ---", "cyan"))
    summary = (
        f"  Source Project: {src_proj_name} (ID: {args.source_project_id})\n"
        f"  Target Project: {tgt_proj_name} (ID: {args.target_project_id})\n"
        f"  Source Path:    {args.source_branch} -> {args.source_env}\n"
        f"  Target Path:    {args.target_branch} -> {args.target_envs[0]}\n"
        f"  MR Creation:    {'Yes' if args.create_mr else 'No'}"
    )
    print(summary)
    confirm = input("\nProceed with this sync? (y/n): ").lower()
    if confirm == 'y':
        run_sync_operation(args, token, config)
    else:
        print("Sync cancelled.")

# ------------------------------------------------------------------------------
# SECTION 5: Main Dispatcher
# ------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Advanced Configuration Sync Tool", formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--config", default="sync-config.json", help="Path to the JSON configuration file (default: sync-config.json)")
    parser.add_argument("--update", action="store_true", help="Update the local cache of GitLab projects and exit.")
    
    # Non-interactive flags
    parser.add_argument("-tp", "--target-project-id", help="GitLab Project ID to sync TO.")
    parser.add_argument("-d", "--dry-run", action="store_true", help="Show changes without writing files or creating MRs.")
    parser.add_argument("-p", "--branch-prefix", default="feature/auto-config-sync")
    parser.add_argument("-m", "--commit-message", default="chore(config): Automated configuration sync")
    parser.add_argument("--no-mr", action="store_false", dest="create_mr", help="Disable automatic MR creation")
    
    args = parser.parse_args()
    
    # Load configuration
    try:
        config = load_config(args.config)
    except Exception as e:
        print(colored(f"ERROR: Failed to read config file: {e}", "red"))
        sys.exit(1)
        
    gitlab_url = config.get('gitlab', {}).get('url', 'https://gitlab.com')
    token = config.get('gitlab', {}).get('token')
    
    if not token:
        print(colored("ERROR: GitLab token not set in configuration file", "red"))
        sys.exit(1)

    if args.update:
        update_projects_cache(args.config, gitlab_url, token)
    elif args.target_project_id and args.target_branch and args.source_env and args.target_envs:
        if not args.source_project_id: 
            args.source_project_id = args.target_project_id
        run_sync_operation(args, token, config)
    else:
        run_interactive_mode(gitlab_url, token, config)

if __name__ == "__main__":
    main()
