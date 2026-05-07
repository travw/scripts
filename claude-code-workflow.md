# claude code workflow

how to stop copy-pasting code in and out of claude.ai chat and start letting claude work on real project folders directly.

## what changes

right now: you ask claude something in chat, copy the code it writes, paste it into a file, save, run it. if something breaks you copy the file back into chat and ask for a fix. one file at a time.

new way: claude reads and writes the files in your project folder itself. it can see the whole project at once, edit several files, run scripts, check what it broke, and fix it. you watch it work in claude desktop, then push to github when you like the result.

git is part of this. you don't have to know git -- claude commits for you. you mostly run two commands: `git status` (what changed) and `git push` (send to github).

## one-time setup

### 1. directory structure

create `C:\Projects\` if it doesn't exist. put one folder per project under it.

```
C:\Projects\
  freeze-thaw-plugin\
  some-other-thing\
  ...
```

avoid putting projects inside OneDrive or Dropbox folders -- their sync fights git and you'll get weird breakage.

### 2. github cli

skip if you already have `gh`. otherwise open powershell as your normal user (not admin) and run:

```
winget install GitHub.cli
```

close and reopen powershell after install so the `gh` command works. then:

```
gh auth login
```

pick "GitHub.com", "HTTPS", "Login with a web browser". it gives you a code, opens a browser, you paste the code, done.

### 3. git identity

before your first commit ever, tell git who you are. but use your github noreply email so your real email doesn't get baked into public commit history forever.

go to https://github.com/settings/emails, find the line that looks like `12345678+yourusername@users.noreply.github.com`, copy it.

then in powershell:

```
git config --global user.name "Your Name"
git config --global user.email "12345678+yourusername@users.noreply.github.com"
```

### 4. claude desktop

if you don't have it: https://claude.ai/download. install, sign in.

inside claude desktop you want to be in the mode where it can read and write files on your machine (sometimes called code mode or shown as a project / workspace). when you start a new conversation, point it at a project folder on disk. once that's set, claude can list files, open them, edit them, and run commands in that folder directly.

## starting a new project

### 5. make the folder + init git

open vs code. open the integrated terminal: `ctrl + backtick` (the key left of 1). this drops you into a powershell prompt inside vs code.

```
cd C:/Projects
mkdir my-thing
cd my-thing
git init
```

forward slashes work fine in paths -- you don't have to fight with backslashes.

### 6. point claude desktop at the folder

in claude desktop, start a new chat in code mode and select `C:\Projects\my-thing` as the project folder. claude can now see/edit anything in there.

### 7. tell claude what you want

just describe the thing in plain english. "i want a python script that reads a csv and renames every file in folder X to match column 1." claude will create the file, write the code, maybe ask clarifying questions. let it work.

if it asks to run something, let it (you'll see what it's about to run before it does).

### 8. review what it did

before you push anything to github, look at what changed. in vs code's terminal:

```
git status
```

shows you which files were created / modified / deleted.

```
git diff
```

shows the actual line-by-line changes in modified files. red lines were removed, green lines were added. vs code also shows this graphically in the source control panel (the branchy icon in the left rail).

### 9. commit

claude will normally do this for you when it finishes a piece of work -- it'll say something like "committed as: add csv renamer script". if you want to do it yourself or claude didn't:

```
git add .
git commit -m "short description of what changed"
```

`.` means "everything that changed". the message is just for future you.

### 10. push to github (first time only setup)

while in your project folder:

```
gh repo create
```

prompts:
- Repository name: hit enter for the folder name
- Description: optional, hit enter to skip
- Visibility: pick **Private** for now -- you can flip it public later if you want. private = only you can see it.
- "Would you like to add a remote?" -> yes
- Remote name: enter (use `origin`)
- "Would you like to push commits from the current branch?" -> yes

done. your code is now on github.

### 11. push from then on

after a commit, send it up:

```
git push
```

that's the whole loop.

## the daily loop

```
1. open vs code, open the project folder
2. ctrl + backtick to open terminal
3. open claude desktop, point it at the same folder
4. ask claude to do stuff
5. git status / git diff to check
6. git push when happy
```

## essentials cheat sheet

navigation:
- `cd C:/Projects/foo` -- go to that folder
- `cd ..` -- up one level
- `ls` -- list files in current folder
- `pwd` -- show where you are right now

git:
- `git status` -- what's changed since last commit
- `git diff` -- what specifically changed
- `git log --oneline` -- list of past commits
- `git push` -- send commits to github
- `git pull` -- get latest from github (for when you work on multiple machines)

## common gotchas

- **terminal won't open in vs code**: try `ctrl + backtick` again, or View menu -> Terminal.
- **"fatal: not a git repository"**: you forgot `git init`, or you're in the wrong folder. run `pwd` to check where you are.
- **pushed but don't see it on github.com**: refresh the page. confirm `git status` says "up to date with origin/main".
- **claude desktop can't see your files**: you're probably not in code/project mode, or you pointed it at the wrong folder. start a new chat and re-select the folder.
- **`gh` command not found right after installing**: close and reopen the terminal (or all of vs code) so it picks up the new install.
- **terminal pasted weird path with no slashes**: powershell drops bare backslashes occasionally. use forward slashes in paths: `cd C:/Projects/foo` not `cd C:\Projects\foo`.

## what to try first

pick something small and self-contained. a python script that does one thing. don't try to port a big existing project as your first move -- start fresh and see how the workflow feels.

once you've done one full loop (folder, claude edits, commit, push) the rest is repetition.
