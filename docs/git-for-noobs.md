# git & github on windows 11: the absolute basics

a no-background-assumed walkthrough using vs code's built-in terminal. read top to bottom the first time.

---

## 0. what even is this stuff

- **git** = a program on your computer that tracks changes to files in a folder. it's local. it works offline. it has nothing to do with the internet by itself.
- **github** = a website owned by microsoft that hosts copies of git folders so you can back them up, share them, and collab with others. github is to git what dropbox is to a folder on your desktop -- one is a service, the other is the thing being synced.
- **vs code** = a free code editor from microsoft. it's where you'll edit files AND run git commands, all in one window.
- **repository (repo)** = a folder that git is tracking. that's it. a repo is just a folder with a hidden `.git` subfolder inside that stores all the history.
- **commit** = a snapshot of your repo at a point in time, with a message you wrote describing what changed. think "save point" in a video game.
- **remote** = a copy of your repo that lives somewhere else (usually github). your local repo can "push" commits up to it and "pull" commits down from it.
- **branch** = a parallel timeline of commits. the default branch is usually called `main`. for now just know it exists; you'll live on `main` for a while.

---

## 1. install the tools

you need three things: vs code, git, and the github cli. install in this order.

### 1a. install vs code

1. go to https://code.visualstudio.com
2. click the big blue download button for windows.
3. run the installer. accept the license.
4. on the "Select Additional Tasks" screen, CHECK these boxes (they're off by default):
   - "Add 'Open with Code' action to Windows Explorer file context menu"
   - "Add 'Open with Code' action to Windows Explorer directory context menu"
   - "Register Code as an editor for supported file types"
   - "Add to PATH" (usually pre-checked, leave it)
5. finish the install. launch vs code once to make sure it opens.

### 1b. install git for windows

1. go to https://git-scm.com/download/win
2. download the 64-bit installer, run it.
3. click next through everything. the defaults are fine. two things to watch for:
   - when it asks "Choose the default editor used by Git," scroll the dropdown and pick **"Use Visual Studio Code as Git's default editor."** this matters -- it means when git needs you to type a commit message or resolve something, it'll pop open vs code instead of something scary like vim.
   - when it asks about line endings, leave it on "Checkout Windows-style, commit Unix-style line endings". this is the right default on windows.
4. finish the installer.

### 1c. install github cli

this handles github authentication for you so you don't have to deal with tokens.

1. go to https://cli.github.com
2. download the windows installer (`.msi`), run it, next-next-finish.

### 1d. restart vs code

close vs code completely and reopen it. this makes sure it picks up the newly-installed git and gh commands from your PATH.

### 1e. sanity check

in vs code, open the integrated terminal: press ``Ctrl + ` `` (that's control + the backtick key, usually above tab on the left of the 1 key). a terminal panel appears at the bottom of the window.

in that terminal, run:

```
git --version
gh --version
```

you should see version numbers for both. if you get "command not found" or "is not recognized," close vs code entirely, reopen it, try again. if still broken, reinstall the failing tool and make sure you didn't uncheck any "add to PATH" option during install.

### 1f. (optional) confirm powershell is your default shell

vs code's terminal defaults to powershell on windows, which is fine. if for some reason it opens something else and you want to switch: click the little dropdown arrow next to the `+` in the terminal panel, pick "Select Default Profile," choose "PowerShell." then click the trash icon on the current terminal and open a new one with `` Ctrl + ` ``.

all commands in this guide work in powershell.

---

## 2. tell git who you are

git stamps every commit with your name and email. do this ONCE per machine. in the vs code terminal:

```
git config --global user.name "your name"
git config --global user.email "you@example.com"
```

use the same email you'll use for github. it doesn't have to be your real name but it should be something you're ok with appearing in commit history forever.

also set the default branch name to `main` (matches github's default):

```
git config --global init.defaultBranch main
```

---

## 3. make a github account & authenticate

### 3a. sign up

go to https://github.com, make an account, use the email you put in `git config` above. verify the email.

### 3b. authenticate via gh cli

github stopped accepting passwords for git operations years ago. the gh cli handles this for you. in the vs code terminal:

```
gh auth login
```

answer the prompts (use arrow keys + enter):

- **What account?** GitHub.com
- **Preferred protocol?** HTTPS
- **Authenticate Git with your GitHub credentials?** Yes
- **How would you like to authenticate?** Login with a web browser

it'll show you a one-time code like `ABCD-1234` and pause. copy that code, press enter, your browser opens to a github page. paste the code, click authorize. close the browser tab, come back to vs code. the terminal should now say you're logged in.

from now on, when git needs to talk to github, it's authenticated automatically. you won't be asked for passwords or tokens.

---

## 4. your first repo: starting from scratch

scenario: you want to start a new project and track it with git from day one.

### 4a. make a folder and open it in vs code

the cleanest way is to let vs code do the folder creation for you:

1. in vs code, go to `File > Open Folder` (or press `Ctrl + K` then `Ctrl + O`).
2. navigate to where you want the project (e.g., `C:\Users\yourname\Documents`).
3. click "New Folder" in the dialog, name it something like `my-first-repo`, then open that new empty folder.
4. vs code may ask "Do you trust the authors of the files in this folder?" -- click "Yes, I trust the authors." it's your own folder.
5. the file explorer on the left now shows your empty folder. open the terminal with `` Ctrl + ` `` -- notice it automatically opens INSIDE that folder. no `cd`-ing required. this is the main reason we're using vs code's terminal.

### 4b. turn the folder into a git repo

in the terminal:

```
git init
```

you'll see "Initialized empty Git repository in ...". a hidden `.git` folder now exists (vs code won't show it by default). don't touch it.

### 4c. make something to commit

in vs code's file explorer (left sidebar), hover over the folder name and click the "New File" icon (looks like a page with a `+`). name the file `README.md` and press enter. type some text in the editor panel, like:

```
# my first repo

this is a test.
```

save with `Ctrl + S`. github displays README.md files automatically on the repo page, which is why everyone has one.

### 4d. check status

back in the terminal:

```
git status
```

this is the command you'll run MORE than any other. it tells you: what branch you're on, what files git sees as new/changed/deleted, and what's staged for the next commit. run it whenever you're confused about what git thinks is going on.

right now it should say README.md is "untracked."

you'll also notice the file in vs code's sidebar has a green `U` next to it -- vs code has a built-in git view that shows you the same info visually. for now, stick with terminal commands to build the mental model.

### 4e. stage the file

"staging" means "i'm telling git i want this change to be part of the next commit." it's a two-step dance: stage, then commit.

```
git add README.md
```

or to stage everything that's changed in the folder:

```
git add .
```

the `.` means "current directory and everything in it." run `git status` again -- README.md is now "staged" (shown in green).

### 4f. commit

```
git commit -m "first commit"
```

`-m` means "message." the message should describe what the commit changes. for your first one, "first commit" is fine. in general, write messages like "add login page" or "fix typo in header" -- short, present tense, describes the change.

congrats, you have one commit. run `git log` to see it. press `q` to exit the log view.

---

## 5. push your local repo up to github

right now your repo only exists on your computer. to get it on github, in the terminal:

```
gh repo create
```

answer the prompts:

- **What would you like to do?** Push an existing local repository to GitHub
- **Path to local repository:** `.` (just a period, means current directory)
- **Repository name:** whatever you want
- **Description:** whatever, or leave blank
- **Visibility:** Public or Private (Private = only you can see it)
- **Add a remote?** Yes
- **What should the new remote be called?** `origin` (that's the convention, just press enter)
- **Would you like to push commits from the current branch to "origin"?** Yes

that's it. the output will include a url. `Ctrl + click` on it to open your repo on github in a browser. your files are there.

---

## 6. the everyday loop

this is 90% of what you'll actually do once set up. memorize this rhythm:

1. edit files in vs code.
2. save them with `Ctrl + S`.
3. in the terminal: `git status` -- see what you changed.
4. `git add .` -- stage all changes.
5. `git commit -m "describe what you did"` -- snapshot it.
6. `git push` -- send it to github.

that's it. repeat forever.

### why stage then commit? why not one step?

bc sometimes you've changed 5 files but only want to commit 3 of them right now. staging lets you pick. as a beginner you can just always do `git add .` and not think about it.

---

## 7. cloning an existing repo

scenario: someone (you, a coworker, a stranger) has a repo on github and you want a copy on your machine.

### 7a. get the url

1. go to the repo's github page.
2. click the green "Code" button.
3. copy the HTTPS url (looks like `https://github.com/someone/something.git`).

### 7b. clone it with vs code's command palette

the smooth way:

1. in vs code, press `Ctrl + Shift + P` to open the command palette (a searchable menu of every command).
2. type `Git: Clone` and press enter.
3. paste the url, press enter.
4. it asks where to save the folder. pick somewhere like `C:\Users\yourname\Documents`.
5. when it's done, it asks "Would you like to open the cloned repository?" -- click "Open."

you're now inside the cloned repo in vs code. open the terminal with `` Ctrl + ` `` -- it's already in the right folder. it's already set up with `origin` pointing at github. you can immediately `git pull`, `git push` (if you have permission), make commits, etc.

### 7c. or clone from the terminal if you prefer

same result, more explicit. first open any folder in vs code, then in its terminal:

```
cd C:\Users\yourname\Documents
git clone https://github.com/someone/something.git
```

then `File > Open Folder` to navigate into the new `something` folder.

**cloning vs init:** cloning is for when the repo already exists on github. `git init` is for when you're starting a new one locally. don't mix them up.

---

## 8. pulling changes from github

scenario: you pushed a commit from your laptop yesterday, now you're on your desktop and you want the latest. or: a collaborator pushed changes and you want them.

```
git pull
```

that's the whole command. it fetches the latest commits from `origin` and merges them into your current branch. run it before you start working each session as a habit.

**gotcha:** if you have uncommitted changes locally AND there are new commits on github that touch the same files, `git pull` will complain about conflicts. for now, the simple rule: commit or stash your local changes BEFORE you pull. "stash" is a topic for later; just commit for now.

---

## 9. the commands you'll actually use, cheat sheet

```
git status                         # what's going on? run constantly.
git add .                          # stage all changes in current folder
git add somefile.txt               # stage one specific file
git commit -m "message"            # commit staged changes
git push                           # upload commits to github
git pull                           # download commits from github
git log                            # show commit history (q to quit)
git log --oneline                  # shorter history
git diff                           # see unstaged changes
git diff --staged                  # see staged changes
git clone <url>                    # copy a github repo locally
git init                           # make current folder into a repo
git remote -v                      # see where "origin" points
```

### looking at and restoring old commits

first, you need a commit's "hash" -- the ID git uses to refer to it. get it from:

```
git log --oneline                  # lists commits w/ short hashes like "a1b2c3d"
```

copy the hash of the commit you care about. then:

```
git show a1b2c3d                   # see what changed in that commit (q to quit)
git show a1b2c3d -- somefile.txt   # just that file's diff in that commit
git diff a1b2c3d HEAD              # all changes from that commit to now
```

### undoing stuff -- pick the right tool

these four commands all "go back in time" but do very different things. read carefully.

**1. throw away uncommitted changes to a file** (you edited it, haven't committed, want to start over):

```
git restore somefile.txt           # revert file to its last committed state
git restore .                      # revert EVERYTHING uncommitted. nuclear option.
```

safe -- only touches unstaged edits. can't undo a commit.

**2. restore ONE file to how it looked in an older commit** (keep everything else as-is):

```
git restore --source=a1b2c3d somefile.txt
```

this changes the file in your working directory to match that old version. it does NOT make a commit -- it just edits the file. then you `git add` + `git commit` to save it as a new commit on top of your current history. this is the safest way to "restore an old version."

**3. undo an entire past commit by making a NEW commit that reverses it** (safe, preserves history):

```
git revert a1b2c3d
```

git opens an editor for a commit message (vs code, if you set it up in section 1b), save and close. result: a new commit appears on top that undoes whatever that old commit did. nothing in history is erased. SAFE to use even on commits you've already pushed.

**4. rewind history, erasing commits after a certain point** (DANGEROUS):

```
git reset --hard a1b2c3d           # move branch pointer to a1b2c3d, DELETE everything after
```

this deletes commits. it will delete uncommitted work too. if you've already pushed those commits to github, you'll create a mess for yourself and any collaborators. RULE: don't use `--hard` unless you're certain nothing after the target commit matters AND you haven't pushed it. if you already pushed, use `git revert` instead.

a gentler version:

```
git reset --soft a1b2c3d           # rewind, but keep all the changes as "staged" for re-committing
```

useful if you want to squish several messy commits into one clean one.

**5. just look around at an old commit without changing anything:**

```
git checkout a1b2c3d               # temporarily snap your working dir to that commit
git switch -                       # come back to your branch (replace "-" with branch name if needed)
```

you'll see a scary "detached HEAD" message -- that just means "you're not on a branch rn, don't make commits here or they'll be orphaned." look around, then `git switch main` (or whatever your branch is) to return. totally safe as long as you don't commit while detached.

### escape hatch: you screwed up and want out

```
git reflog                         # shows EVERY recent HEAD movement, even "lost" commits
```

git almost never truly deletes commits -- they linger for ~30 days. `reflog` shows you every state your repo has been in recently, with hashes. if you `reset --hard`'d something you actually wanted, find its hash in reflog and `git reset --hard <that-hash>` to get back. this has saved me more times than i can count. remember it exists.

### handy vs code shortcuts

```
Ctrl + `                           # open/focus the terminal
Ctrl + Shift + `                   # open a NEW terminal tab
Ctrl + S                           # save current file
Ctrl + Shift + P                   # command palette (find any command)
Ctrl + K then Ctrl + O             # open folder
Ctrl + B                           # toggle sidebar
```

---

## 10. things that will trip you up

- **vs code terminal isn't in your repo folder.** if you opened vs code without opening a folder, the terminal starts in some random place. always use `File > Open Folder` to open your project, not individual files. check with `pwd` in powershell -- it prints the current directory.
- **you can't `git init` inside another git repo.** if you accidentally do, you'll get confused fast. check with `git status` -- if you're not sure which repo you're in, run `git rev-parse --show-toplevel` to see the repo root.
- **line endings on windows.** windows uses CRLF, linux/mac use LF. the installer default handles this. if you ever see a commit that claims every line of every file changed, it's a line ending issue. google "git autocrlf" when it happens.
- **huge files.** git is bad at files over ~100mb. don't commit videos, datasets, huge binaries. github will actually reject files over 100mb. use `.gitignore` (see below) to exclude them.
- **`.gitignore`**. a plain text file in the root of your repo listing patterns of files git should pretend don't exist. create one by right-clicking in the vs code file explorer, "New File," name it `.gitignore` (yes, starting with a dot). example contents:

```
node_modules/
*.log
.env
dist/
```

make one the day you start any real project. github has templates at https://github.com/github/gitignore.

- **committing secrets.** never commit passwords, api keys, or .env files. git history is forever and even if you delete the file in a later commit, the secret is still in the history. if you do this by accident, assume the secret is compromised and rotate it immediately. don't just try to scrub history -- people have already scraped it.
- **vs code's "source control" panel.** there's a git icon in the left sidebar (looks like a branch with dots). it provides a GUI for staging and committing. it works fine, but as a beginner i recommend sticking with terminal commands until they feel automatic -- otherwise you end up clicking buttons without understanding what git is actually doing.

---

## 11. what to learn next (in order)

once the above feels automatic:

1. **branches**: `git branch`, `git switch -c new-branch-name`. how to work on a feature without messing with `main`.
2. **pull requests (PRs) on github**: propose merging a branch into `main` with a review. this is how team collaboration actually happens. the `gh` cli can create them with `gh pr create`.
3. **merge conflicts**: when git can't auto-combine two sets of changes and you have to resolve them by hand. vs code has an excellent built-in merge conflict UI -- it'll light up automatically when you hit one.
4. **`.gitignore`** in depth.
5. **undoing stuff**: `git restore`, `git reset`, `git revert`. they do different things, pick carefully.
6. **ssh keys** instead of the gh cli token flow, if you want to be fancy.

github has a decent interactive tutorial at https://skills.github.com if you want structured practice.

---

## tldr

install vs code, git, and gh cli. run `gh auth login`. set your name/email in `git config`. for every project: `File > Open Folder` in vs code, open the terminal with `` Ctrl + ` ``, and then the loop forever is: edit, save, `git add .`, `git commit -m "..."`, `git push`. to get a copy of someone else's repo: `Ctrl + Shift + P` then `Git: Clone`. to get the latest changes: `git pull`. when in doubt: `git status`.
