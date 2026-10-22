# Updates

Session Bridge keeps itself up to date from its [GitHub releases](https://github.com/pma1999/session-bridge/releases). You decide when an update is installed, and the version you have keeps working if anything goes wrong.

## What happens when you open Session Bridge

When you run `sessionbridge` with no command, it checks GitHub for a newer release, at most once a day, in the background while your conversations load. If there is one, it asks before showing the search:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/update-en-dark.svg">
  <img alt="The update offer with its three choices" src="images/update-en-light.svg" width="100%">
</picture>

| Choice | What happens |
| --- | --- |
| **Update now** | The update is installed and Session Bridge reopens on the new version, which shows the version you updated from. |
| **Not now** | Nothing changes; you are asked again the next day. Esc does the same. |
| **Skip this version** | You are not asked about this version again, only about the next one. |

The **What's new** link under the question opens the release notes.

Only the start screen checks and asks. Commands with arguments, such as `sessionbridge list`, and anything run with `--json` never check for updates or ask questions, so scripts are never interrupted. If GitHub cannot be reached, nothing is shown, and Session Bridge tries again an hour later.

## How an update is installed

**From a release ZIP** (the usual installation):

1. Session Bridge downloads the new release's portable ZIP and its `SHA256SUMS` file from GitHub.
2. It checks the ZIP against those checksums, and against the checksum GitHub publishes for the file. It also checks that every file in the ZIP stays inside the project folder and that the ZIP is the version it claims to be.
3. It unpacks the release into a folder of its own, `%LOCALAPPDATA%\SessionBridge\app\X.Y.Z`, and runs **that release's own installer**, the same `scripts/install.ps1` you ran the first time.
4. The installer switches the `sessionbridge` command to the new version and restarts the synchronization worker on it.
5. Session Bridge reopens on the new version.

Older version folders are cleaned up on later starts; the one in use and the one before it are always kept.

**From a Git clone**, Session Bridge updates with Git instead of downloading a ZIP. It moves `main` forward to the release tag (`git merge --ff-only vX.Y.Z`) and installs a fresh environment for it in `%LOCALAPPDATA%\SessionBridge\runtime\X.Y.Z`. The question says which folder it will update. It only does this when all of these are true:

- the clone is on `main`, following `origin/main`;
- `origin` is `github.com/pma1999/session-bridge`;
- there are no uncommitted changes;
- there are no commits of your own that are not in the release.

Otherwise Session Bridge does not ask. It shows a line saying a new version exists and why it will not update the clone, for example *Your Git copy has uncommitted changes*, and you update by hand with `git pull` and `scripts\install.ps1`. It never stashes, rebases, resets or merges your own work. If the installation fails after the clone was moved forward, the clone is put back where it was.

**Other installations**, such as a wheel installed with pip, or Linux, are told about new versions with a link to the release, and update by hand.

## If something goes wrong

- **Nothing is replaced until the new version is ready.** The command is switched only after the new installation has been checked, and the installer's record decides which version is in use.
- **A failed update leaves the version you have working.** The message names the installer's log, in `%LOCALAPPDATA%\SessionBridge\logs\update-X.Y.Z.log`.
- **Two windows cannot update at once.** A second window that tries is told another one is installing, and a window opened before an update notices that the update already happened.
- **An interrupted installer is cleaned up.** Ctrl+C does not abandon a half-finished installation, and an installer that takes more than 15 minutes is stopped with everything it started.

Run `sessionbridge update` to try again at any time.

## The `update` command

```powershell
sessionbridge update
sessionbridge update --check
sessionbridge update --check --json
sessionbridge update --yes
sessionbridge --version
```

| Command | Does |
| --- | --- |
| `sessionbridge update` | Checks now, even if it checked today or you skipped that version, and asks whether to install. |
| `sessionbridge update --check` | Only says which version you have, which is published, and whether this installation can update itself. |
| `sessionbridge update --yes` | Installs without asking. Without a terminal, installing needs `--yes`. |
| `sessionbridge --version` | Shows the version you have. |

`update --check --json` returns `current`, `latest`, `available`, `installable`, `kind`, `blocker` (why a Git clone cannot update itself), `page_url` and `startup_check`, for scripts.

## Turning the check off

```powershell
sessionbridge update --startup-check off
sessionbridge update --startup-check on
```

The choice is saved in `settings.json` in the data folder. The `SESSIONBRIDGE_NO_UPDATE_CHECK=1` environment variable also turns the check off, for one shell or a whole machine. `sessionbridge update` still works when the check is off.

## What the checksums do and do not prove

The checksums come from the same GitHub release as the files. They prove that the download arrived complete and unchanged from that release. They do not prove who published the release: someone who could publish releases in this repository could publish matching checksums. Session Bridge only downloads from `github.com` and GitHub's own download servers, over HTTPS.

## Updating from 0.7.x or earlier

The updater runs in the version you have, so versions released before it existed cannot update themselves. To move from 0.7.x or earlier, install 0.8.0 or later once by hand, the same way you installed Session Bridge the first time. From then on, it updates itself.
