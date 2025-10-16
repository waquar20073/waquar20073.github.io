#!/usr/bin/env python3
"""
config_sync.py - Config synchronization tool with JSON Schema validation, 3-way conflict detection,
and support for applying to multiple target branches.
Usage (example):
python config_sync.py \
  --repo-url "https://gitlab.com/org/config-repo.git" \
  --project-id 12345 \
  --gitlab-url "https://gitlab.com" \
  --source-branch develop \
  --source-commit <commit_hash> \
  --target-branches release/1.4 release/1.5 \
  --from-env DEV \
  --to-env SIT UAT \
  --services microservice-1 microservice-26 \
  --config-file sync-config.json \
  --schemas-dir schemas/ \
  --no-mr-description
"""

from pathlib import Path
from typing import Dict, Any, Tuple, List
import random
import os
import sys
import argparse
import tempfile
import shutil
import json
import fnmatch
import difflib
import subprocess
import requests

# -----------------------------
# Utilities: flatten / unflatten
# -----------------------------
def flatten(d: Dict[str, Any], parent_key: str = "", sep: str = "|"):
    """Flattens a nested dictionary into a single level with concatenated keys."""
    items = {}
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, dict):
            items.update(flatten(v, new_key, sep=sep))
        else:
            items[new_key] = v
    return items

def unflatten(flat: Dict[str, Any], sep: str = "|"):
    """Converts a flattened dictionary back into a nested dictionary."""
    result = {}
    for compound_key, value in flat.items():
        parts = compound_key.split(sep)
        cur = result
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = value
    return result

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
# Config / Policy helpers
# -----------------------------
def load_json(path: Path) -> Dict[str, Any]:
    """Loads a JSON file from the given path."""
    if not path.exists():
        return {}
    with open(path, "r") as f:
        return json.load(f, object_pairs_hook=dict) or {}

def key_matches_any(patterns: List[str], key: str) -> bool:
    """Checks if a key matches any of the given wildcard patterns."""
    for pat in patterns:
        if fnmatch.fnmatchcase(key, pat):
            return True
    return False

# -----------------------------
# Merge strategies & CSV helpers
# -----------------------------
def parse_csv_string_to_list(s: str, sep: str = ","):
    """Parses a CSV string into a list of strings."""
    return [seg.strip() for seg in str(s).split(sep) if seg.strip()]

def list_to_csv_string(lst: List[str], sep: str = ","):
    """Converts a list of strings into a CSV string."""
    return sep.join(lst)

def merge_values(key: str, src_val, tgt_val, strat: str, csv_sep: str):
    """Merges two values based on a given strategy."""
    if src_val is None and tgt_val is None:
        return None, False

    is_csv = (isinstance(src_val, str) and "," in src_val) or \
             (isinstance(tgt_val, str) and "," in tgt_val)

    if is_csv:
        src_list = parse_csv_string_to_list(src_val or "", csv_sep)
        tgt_list = parse_csv_string_to_list(tgt_val or "", csv_sep)
        if strat == "append":
            merged = tgt_list + [x for x in src_list if x not in tgt_list]
        else:  # union
            merged = []
            for x in tgt_list + src_list:
                if x not in merged:
                    merged.append(x)
        merged_s = list_to_csv_string(merged, sep=csv_sep)
        changed = merged_s != (tgt_val or "")
        return merged_s, changed

    if isinstance(src_val, list) or isinstance(tgt_val, list):
        s = src_val if isinstance(src_val, list) else []
        t = tgt_val if isinstance(tgt_val, list) else []
        if strat == "append":
            merged = t + [x for x in s if x not in t]
        else:  # union
            merged = []
            for x in t + s:
                if x not in merged:
                    merged.append(x)
        changed = merged != t
        return merged, changed

    if strat == "replace":
        changed = src_val != tgt_val
        return src_val, changed

    if strat == "keep-target-on-conflict":
        if tgt_val is not None:
            return tgt_val, False
        else:
            return src_val, src_val != tgt_val

    if strat == "remove-if-source-missing":
        changed = src_val != tgt_val
        return src_val, changed

    changed = src_val != tgt_val
    return src_val, changed

# -----------------------------
# Merge engine with 3-way conflict detection
# -----------------------------
def compute_three_way_ancestor(repo_path: str, source_ref: str, target_ref: str) -> str:
    """Computes the common ancestor of two refs using 'git merge-base'."""
    try:
        result = subprocess.run(
            ["git", "merge-base", source_ref, target_ref],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None

def merge_json_configs_three_way(
    ancestor_json: Dict[str, Any],
    src_json: Dict[str, Any],
    tgt_json: Dict[str, Any],
    ignore_keys: List[str],
    policy: Dict[str, Any],
    csv_sep: str = ",",
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Performs a 3-way merge of JSON configurations, detecting conflicts."""
    warnings = []
    changes = []
    conflicts = []

    flat_anc = flatten(ancestor_json) if ancestor_json else {}
    flat_src = flatten(src_json)
    flat_tgt = flatten(tgt_json)
    merged_flat = dict(flat_tgt)

    defaults = policy.get("defaults", {})
    default_scalar = defaults.get("scalar", "replace")
    default_list = defaults.get("list", "union")
    on_missing_in_source = defaults.get("on_missing_in_source", "keep")

    overrides = policy.get("overrides", [])

    def get_strategy_for_key(k: str) -> str:
        for ov in overrides:
            match = ov.get("match")
            if not match:
                continue
            if fnmatch.fnmatchcase(k, match):
                return ov.get("strategy")
        return None

    for k, src_val in flat_src.items():
        if key_matches_any(ignore_keys, k):
            if k in flat_anc:
                if flat_anc.get(k) != src_val:
                    warnings.append(f"Ignored key changed in source: {k} (manual update required).")
            elif k in flat_tgt and flat_tgt[k] != src_val:
                warnings.append(f"Ignored key changed in source: {k} (manual update required).")
            continue

        tgt_val = flat_tgt.get(k)
        anc_val = flat_anc.get(k)

        src_changed = anc_val != src_val
        tgt_changed = (anc_val != tgt_val) if (k in flat_tgt or anc_val is not None) else False
        
        strat = get_strategy_for_key(k)
        if not strat:
            is_csv = (isinstance(src_val, str) and "," in src_val) or \
                     (isinstance(tgt_val, str) and "," in tgt_val)
            if isinstance(src_val, list) or isinstance(tgt_val, list) or is_csv:
                strat = default_list
            else:
                strat = default_scalar

        if src_changed and tgt_changed and (tgt_val != src_val) and strat not in ["union", "append"]:
            conflicts.append(f"Conflict on key {k}: ancestor={repr(anc_val)} source={repr(src_val)} target={repr(tgt_val)}")
            continue

        merged_val, changed = merge_values(k, src_val, tgt_val, strat, csv_sep)
        if changed:
            merged_flat[k] = merged_val
            changes.append(f"Updated {k}: {repr(tgt_val)} -> {repr(merged_val)}")
        else:
            if k not in merged_flat and merged_val is not None:
                merged_flat[k] = merged_val
                changes.append(f"Added {k}: {repr(merged_val)}")

    for k in flat_tgt:
        if k in flat_src:
            continue
        if key_matches_any(ignore_keys, k):
            continue
        anc_val = flat_anc.get(k)
        if anc_val is not None:
            if on_missing_in_source == "remove":
                if flat_tgt.get(k) != anc_val:
                    conflicts.append(f"Conflict on removal of key {k}: target changed since ancestor; manual resolution required.")
                    continue
                if k in merged_flat:
                    del merged_flat[k]
                    changes.append(f"Removed {k} because missing in source")

    merged_json = unflatten(merged_flat)
    return merged_json, warnings + conflicts, changes


# -----------------------------
# Helper: unified diff string
# -----------------------------
def unified_diff_str(a: str, b: str, fromfile: str = "orig", tofile: str = "merged"):
    """Generates a unified diff string for two strings."""
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = difflib.unified_diff(a_lines, b_lines, fromfile=fromfile, tofile=tofile)
    return "".join(diff)

# -----------------------------
# JSON Schema validation (Removed)
# -----------------------------
def validate_against_schema(schema_path: Path, json_data: Dict[str, Any]) -> List[str]:
    """Validates JSON data against a schema. (Currently disabled)"""
    return []

# -----------------------------
# GitLab / Git operations
# -----------------------------
def clone_repo(repo_url: str, token: str, branch: str, tmpdir: str, depth: int = None):
    """Clones a git repository."""
    if token and repo_url.startswith("https://"):
        parts = repo_url.split("https://", 1)
        auth_url = f"https://oauth2:{token}@{parts[1]}"
    else:
        auth_url = repo_url
    
    cmd = ["git", "clone", "--branch", branch]
    if depth:
        cmd.extend(["--depth", str(depth)])
    cmd.extend([auth_url, tmpdir])
    
    subprocess.run(cmd, check=True, capture_output=True)

def create_and_push_branch(repo_path: str, new_branch: str, commit_paths: List[str], commit_message: str):
    """Creates a new branch, commits changes, and pushes to the remote."""
    subprocess.run(["git", "checkout", "-b", new_branch], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "add"] + commit_paths, cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", f"{new_branch}:{new_branch}"], cwd=repo_path, check=True, capture_output=True)

# -----------------------------
# CLI main
# -----------------------------
def main():
    """The main entry point for the script."""
    parser = argparse.ArgumentParser(description="Config sync tool (3-way merge + multi-target branches)")
    parser.add_argument("--repo-url", required=True, help="URL of the Git repository")
    parser.add_argument("--project-id", required=True, type=int, help="GitLab project ID")
    parser.add_argument("--gitlab-url", default="https://gitlab.com", help="GitLab instance URL")
    parser.add_argument("--config-file", default="sync-config.json", help="path inside repo or local path")
    parser.add_argument("--source-branch", default="develop", help="Source branch to sync from")
    parser.add_argument("--source-commit", help="Source commit hash to sync from (overrides source-branch)")
    parser.add_argument("--source-config-filename", default="config.json", help="Filename of the config in the source directory")
    parser.add_argument("--target-config-filename", default="config.json", help="Filename of the config in the target directory")
    parser.add_argument("--target-branches", nargs="+", required=True, help="one or more target branches (release branches)")
    parser.add_argument("--from-env", default="dev", help="Source environment to sync from")
    parser.add_argument("--to-env", nargs="+", required=True, help="Target environments to sync to")
    parser.add_argument("--services", nargs="+", required=True, help="service folder names")
    parser.add_argument("--gitlab-token-env", default="GITLAB_TOKEN", help="env var that holds GitLab token")
    parser.add_argument("--csv-sep", default=",", help="separator used for CSV-like keys")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without making them")
    parser.add_argument("--branch-prefix", default="bugfix/coreb-000-auto-app-config", help="Prefix for the new branch name")
    parser.add_argument("--commit-message", default="chore(config): auto-sync app configuration", help="Commit message to use")
    parser.add_argument("--mr-labels", default="", help="comma separated labels to apply to MR")
    parser.add_argument("--schemas-dir", default="schemas", help="directory in repo (or local) where service schemas live (optional)")
    parser.add_argument("--fetch-depth", type=int, default=0, help="Git fetch depth. Use 0 for full history (required for reliable 3-way merge).")
    parser.add_argument("--same-branch", action="store_true", help="Source and target configs are on the same branch (requires separate clones)")
    args = parser.parse_args()

    token = os.environ.get(args.gitlab_token_env)
    if not token:
        print(colored(f"ERROR: GitLab token not found in env {args.gitlab_token_env}", "red") )
        sys.exit(1)

    tmpdir_base = tempfile.mkdtemp(prefix="config-sync-")
    created_mrs = []
    overall_success = True

    try:
        for tgt_branch in args.target_branches:
            print(colored(f"\n=== Processing target branch: {tgt_branch} ===", "cyan") )
            
            # Set up target directory
            tmpdir = Path(tmpdir_base) / f"target_{tgt_branch.replace('/', '_')}"
            tmpdir.mkdir(parents=True, exist_ok=True)
            
            # Set up source directory if using same-branch mode
            source_tmpdir = None
            if args.same_branch and (args.source_commit or args.source_branch != tgt_branch):
                source_tmpdir = Path(tmpdir_base) / f"source_{tgt_branch.replace('/', '_')}"
                source_tmpdir.mkdir(parents=True, exist_ok=True)
                print(f"Cloning source ({args.source_commit or args.source_branch}) to {source_tmpdir}...")
                try:
                    clone_repo(
                        args.repo_url, 
                        token, 
                        branch=args.source_commit or args.source_branch,
                        tmpdir=str(source_tmpdir), 
                        depth=args.fetch_depth if args.fetch_depth > 0 else None
                    )
                except subprocess.CalledProcessError as e:
                    print(colored(f"ERROR cloning source repo: {e.stderr}", "red"))
                    overall_success = False
                    continue

            # Clone target branch
            print(f"Cloning target branch {tgt_branch} to {tmpdir}...")
            try:
                clone_repo(
                    args.repo_url, 
                    token, 
                    branch=tgt_branch, 
                    tmpdir=str(tmpdir), 
                    depth=args.fetch_depth if args.fetch_depth > 0 else None
                )
            except subprocess.CalledProcessError as e:
                print(colored(f"ERROR cloning target repo for branch {tgt_branch}: {e.stderr}", "red") )
                overall_success = False
                continue

            # Set up source reference (only used if not using same-branch mode with separate clone)
            source_ref = args.source_commit or f"origin/{args.source_branch}"
            if not args.source_commit and not source_tmpdir:
                try:
                    subprocess.run(
                        ["git", "fetch", "origin", args.source_branch], 
                        cwd=str(tmpdir), 
                        check=True, 
                        capture_output=True
                    )
                except subprocess.CalledProcessError as e:
                    print(colored(f"Warning: failed to fetch source branch {args.source_branch}: {e.stderr}", "yellow") )

            config_path_in_repo = tmpdir / args.config_file
            policy = load_json(config_path_in_repo) if config_path_in_repo.exists() else load_json(Path(args.config_file))

            ignore_keys = policy.get("ignore_keys", [])
            
            ancestor_commit_sha = compute_three_way_ancestor(str(tmpdir), source_ref, tgt_branch)

            all_changes_for_branch = []
            all_warnings_conflicts = []
            all_changed_files = []
            all_diffs = []

            for service in args.services:
                for to_env in args.to_env:
                    print(colored(f"\n-- Processing: {service}/{to_env}", "blue") )

                    relative_path = Path(service) / to_env / args.target_config_filename
                    tgt_file_path = tmpdir / relative_path

                    ancestor_json = {}
                    if ancestor_commit_sha:
                        try:
                            ancestor_blob_path = f"{ancestor_commit_sha}:{relative_path.as_posix()}"
                            ancestor_content = subprocess.run(["git", "show", ancestor_blob_path], cwd=str(tmpdir), capture_output=True, text=True).stdout
                            if ancestor_content:
                                ancestor_json = json.loads(ancestor_content)
                        except (subprocess.CalledProcessError, json.JSONDecodeError):
                            pass # File might not exist in ancestor

                    # Read source config - either from separate clone or same repo
                    if source_tmpdir:
                        # Read from separate clone
                        source_path = source_tmpdir / service / args.from_env / args.source_config_filename
                        try:
                            with open(source_path) as f:
                                src_json = json.load(f)
                        except (FileNotFoundError, json.JSONDecodeError) as e:
                            print(colored(f"[SKIP] Source config not found at {source_path}: {e}", "yellow"))
                            continue
                    else:
                        # Original behavior - read from same repo
                        try:
                            source_path = Path(service) / args.from_env / args.source_config_filename
                            src_blob_ref = f"{source_ref}:{source_path.as_posix()}"
                            src_content = subprocess.run(
                                ["git", "show", src_blob_ref], 
                                cwd=str(tmpdir), 
                                capture_output=True, 
                                text=True, 
                                check=True
                            ).stdout
                            src_json = json.loads(src_content)
                        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
                            print(colored(f"[SKIP] Source config not found for {service}/{args.from_env} at {source_ref}: {e}", "yellow"))
                            continue

                    # Read target config
                    tgt_json = json.loads(tgt_file_path.read_text()) if tgt_file_path.exists() else {}

                    merged_json, warnings_conflicts, changes = merge_json_configs_three_way(
                        ancestor_json=ancestor_json, src_json=src_json, tgt_json=tgt_json,
{{ ... }}
                    )

                    if warnings_conflicts:
                        all_warnings_conflicts.extend([f"[{service}/{to_env}] {w}" for w in warnings_conflicts])

                    if not changes:
                        continue

                    all_changes_for_branch.extend([f"[{service}/{to_env}] {c}" for c in changes])

                    orig_text = json.dumps(tgt_json, indent=2, sort_keys=False) + "\n"
                    merged_text = json.dumps(merged_json, indent=2, sort_keys=False) + "\n"
                    
                    if orig_text == merged_text:
                        continue

                    all_diffs.append(unified_diff_str(orig_text, merged_text, fromfile=str(relative_path), tofile=str(relative_path)))

                    if not args.dry_run:
                        tgt_file_path.parent.mkdir(parents=True, exist_ok=True)
                        tgt_file_path.write_text(merged_text, encoding="utf-8")
                    
                    all_changed_files.append(str(relative_path))

            if not all_changed_files:
                print(colored(f"No changes detected for target branch {tgt_branch}", "green") )
                continue

            if any(w.startswith("Conflict") for w in all_warnings_conflicts):
                print(colored("Conflicts detected. Aborting automatic MR creation.", "red") )
                for w in all_warnings_conflicts:
                    print(colored(f"  - {w}", "yellow") )
                overall_success = False
                continue

            if args.dry_run:
                print(colored(f"DRY-RUN: Would create branch and MR for {tgt_branch}", "yellow") )
                print("\n".join(all_diffs))
                continue

            new_branch = f"{args.branch_prefix}-{random.randint(1000, 9999)}"
            try:
                create_and_push_branch(str(tmpdir), new_branch, all_changed_files, args.commit_message)
            except subprocess.CalledProcessError as e:
                print(colored(f"ERROR during git commit/push: {e.stderr}", "red") )
                overall_success = False
                continue

            mr_title = f"Bugfix: Automated Application Configuration Sync to {tgt_branch}"
            mr_body = ""
            if not args.no_mr_description:
                mr_body_lines = [f"Automated config sync from `{source_ref}` to `{tgt_branch}`.\n"]
                if all_changes_for_branch:
                    mr_body_lines.append("### Changes\n")
                    mr_body_lines.extend([f"- {ch}" for ch in all_changes_for_branch])
                if all_warnings_conflicts:
                    mr_body_lines.append("\n### Warnings\n")
                    mr_body_lines.extend([f"- {w}" for w in all_warnings_conflicts])
                
                mr_body_lines.append("\n### Diff (truncated)\n")
                full_diff_text = "\n".join(all_diffs)
                max_chars = 4000
                if len(full_diff_text) > max_chars:
                    mr_body_lines.append(f"Diff is large; included first {max_chars} chars below. Full diff is in commit.\n```diff\n")
                    mr_body_lines.append(full_diff_text[:max_chars])
                    mr_body_lines.append("\n```\n")
                else:
                    mr_body_lines.append("```diff\n")
                    mr_body_lines.append(full_diff_text)
                    mr_body_lines.append("\n```\n")
                mr_body = "\n".join(mr_body_lines)

            try:
                headers = {"PRIVATE-TOKEN": token}
                payload = {
                    "source_branch": new_branch,
                    "target_branch": tgt_branch,
                    "title": mr_title,
                    "description": mr_body,
                    "labels": args.mr_labels.split(",") if args.mr_labels else [],
                }
                mr_url = f"{args.gitlab_url}/api/v4/projects/{args.project_id}/merge_requests"
                response = requests.post(mr_url, headers=headers, json=payload)
                response.raise_for_status()
                mr_data = response.json()
                print(colored(f"Created MR: {mr_data['web_url']}", "green") )
                created_mrs.append(mr_data["web_url"])
            except requests.exceptions.RequestException as e:
                print(colored(f"ERROR creating MR: {e.response.text if e.response else e}", "red") )
                print(colored("Local branch created and pushed; please open MR manually.", "yellow") )
                overall_success = False
                continue

            finally:
                shutil.rmtree(str(tmpdir), ignore_errors=True)

        if created_mrs:
            print(colored("\nCreated MRs:", "green") )
            for u in created_mrs:
                print(" -", u)
        else:
            if overall_success:
                print(colored("\nNo MRs created (no changes or dry-run).", "green") )
            else:
                print(colored("\nFinished with warnings/errors; check output above.", "yellow") )

    finally:
        # Clean up all temporary directories
        if 'tmpdir_base' in locals() and os.path.exists(tmpdir_base):
            shutil.rmtree(tmpdir_base, ignore_errors=True)
        # Also clean up any source directories that might have been created
        if 'source_tmpdir' in locals() and source_tmpdir and os.path.exists(source_tmpdir):
            shutil.rmtree(source_tmpdir, ignore_errors=True)

    if not overall_success:
        sys.exit(2)

if __name__ == "__main__":
    main()
