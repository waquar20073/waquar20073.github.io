# Config Sync Tool

This is a handy little script I threw together to keep INI config files in sync across different Git branches and projects. It's got some nifty tricks up its sleeve like 3-way merging (fancy, I know) and a dead-simple menu system so you don't have to remember a bunch of flags.

## What's it do?

- **Smart Merging**: Tries to be clever about merging changes, and falls back to simple mode if it gets confused
- **Easy-Peasy Menus**: Don't want to remember commands? Just run it and pick what you need
- **Scripting Friendly**: Or use flags if you're into that sort of thing
- **Works Across Projects**: Sync between totally different GitLab repos if you want
- **Speedy**: Caches your projects so you're not waiting around

## Setting Things Up

First, you'll need a config file named `sync-config.json`. Here's what goes in it:

```json
{
  "gitlab": {
    "url": "https://gitlab.com",
    "token": "YOUR_GITLAB_TOKEN",  // Don't commit this to git, obviously
    "prod_group_id": "123",       // Find this in GitLab group settings
    "non_prod_group_id": "456"    // Ditto for this one
  },
  "defaults": {
    "csv_strategy": "union"  // Makes CSV values combine instead of overwrite
  },
  "ignore": {
    "keys": [
      "JAVA_OPTS",  // Things you don't want to sync
      "*.secret"    // Wildcards work too
    ]
  },
  "projects_prod": {},       // This gets filled in automatically
  "projects_non_prod": {}    // This too
}
```

## Quick Start

1. First, update your project list (do this whenever you add new projects):
   ```bash
   python sync-configs.py --update
   ```
   This will fill in all your project IDs automatically. Handy, right?

2. Then just run it and follow the menus:
   ```bash
   python sync-configs.py
   ```

## Fancy Options for Fancy People

### The --update Flag

This little guy does all the heavy lifting of finding your projects. Run it:
- When you first set things up
- When you add new projects to your groups
- When you're bored and want to watch the terminal do work

It's smart enough to:
- Keep all your settings and ignores
- Make a backup before changing anything
- Not mess up your carefully crafted config

### The Easy Way (Interactive Mode)

Just type:
```bash
python sync-configs.py
```

And follow the bouncing ball. It'll ask you what you want to do next. No thinking required.

### For the Terminal Warriors (Command Line Args)

If you're into that sort of thing, here's how to use the flags:

```bash
# Sync between different config files in the same branch
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
{{ ... }}
    --target-branch main \
    --source-env config.ini \
    --target-envs config.ini
```
**Example 2: Sync the same file between branches**

```bash
python sync-configs.py \
    --target-project-id 12345 \
    --source-branch develop \
    --target-branch main \
    --source-env config.ini \
    --target-envs config.ini \
    --config sync-config.json
```
## All Those Fancy Flags

Here's what they do (in case the script didn't tell you already):

- `--config`: Where your config file lives (default: `sync-config.json`)
- `--update`: Go fetch the latest projects from GitLab
- `--dry-run`: All talk, no action (great for testing)
- `--branch-prefix`: If you don't like my branch names
- `--commit-message`: For when "Update config" just isn't descriptive enough
- `--no-mr`: For rebels who don't want merge requests

## Pro Tips

- Makes a backup before doing anything dumb (you're welcome)
- CSV values? No problem, it'll merge them nicely
- Tired of syncing that one annoying key? Just add it to the ignore list
- Cleans up after itself (most of the time)

## Known Quirks

- Still can't make coffee (working on it)
- Gets grumpy without proper GitLab permissions
- Might throw a tantrum if you feed it garbage.
