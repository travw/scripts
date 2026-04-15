# agentic coding secure sandbox

what you're building: a linux environment running inside windows, w/ docker inside that, w/ a locked-down container inside that where claude code runs. three layers of "claude can't escape and eat your ssh keys."

## phase 0: prep (5 min)

open powershell AS ADMINISTRATOR (right-click start menu → "terminal (admin)" or "powershell (admin)")
check you're on win11 w/ virtualization enabled: run systeminfo | findstr /C:"Hyper-V" - you want to see "virtualization enabled in firmware: yes". if no, you need to enable virtualization in bios (google "enable virtualization [your motherboard]"). most modern systems have it on by default.


## phase 1: install wsl2 + ubuntu (10 min)

in admin powershell: wsl --install -d Ubuntu-24.04
it'll download stuff, then tell you to reboot. REBOOT.
after reboot, ubuntu will auto-launch a terminal window and ask you to create a username + password. pick something simple (e.g. travis). the password is for sudo inside linux, write it down, you'll need it. it does NOT show characters as you type, that's normal.
once you're at the travis@machine:~$ prompt, you're in linux. run:

   sudo apt update && sudo apt upgrade -y
enter your password. let it finish (few min).
5. close that terminal.

## phase 2: install docker desktop (10 min)

download docker desktop for windows: https://www.docker.com/products/docker-desktop/
run installer. when it asks, make sure "use wsl2 instead of hyper-v" is CHECKED.
after install, reboot again (yes, again).
launch docker desktop. accept the terms. you can skip the sign-in.
in docker desktop → settings (gear icon) → resources → wsl integration → make sure "enable integration with my default wsl distro" is ON, and toggle ON for Ubuntu-24.04. click "apply & restart".
verify: open a new ubuntu terminal (start menu → "ubuntu"), run docker --version. should print a version number. if it does, docker is talking to wsl correctly.


## phase 3: install vs code + extensions (5 min)

download vs code for windows: https://code.visualstudio.com/
install w/ defaults. IMPORTANT: on the "select additional tasks" screen, check "add to path".
open vs code. click the extensions icon (left sidebar, looks like four squares). install these three:

"WSL" (by microsoft)
"Dev Containers" (by microsoft)
"Docker" (by microsoft)


close vs code.


## phase 4: set up your first project in wsl (10 min)

open ubuntu terminal. you're in your linux home dir (~).
make a code folder and clone a test project (use one of yours, e.g. smartlam):

   mkdir -p ~/code
   cd ~/code
   git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
   cd YOUR_REPO
if you don't have git configured yet: git config --global user.email "you@example.com" and git config --global user.name "travis".
3. open it in vs code from inside wsl:
   code .
first time this runs it installs a vs code server in wsl, takes a min. vs code window will open, bottom-left corner should say "WSL: Ubuntu-24.04" in a green/blue badge. that means vs code is running against the linux filesystem. critical.

## phase 5: add the devcontainer (10 min)

this is the actual sandbox part.

in that vs code window, hit ctrl+shift+p to open the command palette.
type "Dev Containers: Add Dev Container Configuration Files" and select it.
choose "Add configuration to workspace".
pick a base: "Node.js & TypeScript" is a fine starting point for your smartlam work. pick the latest LTS version when prompted. skip the optional features for now (just hit ok).
this creates a .devcontainer/devcontainer.json file in your repo. open it.
replace its contents with this (copy-paste exactly):

json   {
     "name": "claude-sandbox",
     "image": "mcr.microsoft.com/devcontainers/javascript-node:1-22-bookworm",
     "features": {
       "ghcr.io/devcontainers/features/github-cli:1": {}
     },
     "postCreateCommand": "npm install -g @anthropic-ai/claude-code",
     "remoteUser": "node",
     "mounts": [],
     "runArgs": ["--cap-drop=ALL", "--security-opt=no-new-privileges"]
   }
save the file.
7. ctrl+shift+p again → "Dev Containers: Reopen in Container". vs code will build the container (first time = 3-5 min, downloads node image + installs claude code). bottom-left badge will change to "Dev Container: claude-sandbox".

# phase 6: run claude code in the sandbox (5 min)

in vs code, open a terminal: ctrl+backtick (the key above tab). this terminal is INSIDE the container.
run: claude
first run will ask you to log in - it'll print a url, open it in your browser, log in w/ your anthropic account, paste the token back. done.
now you can talk to claude code. it can only see/touch files in this project, and can only reach the network endpoints the container allows. injection blast radius = this container. when you're done, close vs code, the container stops.


# daily workflow from here on

open ubuntu terminal → cd ~/code/PROJECT && code . → "reopen in container" if it doesn't auto → terminal → claude
when you start a new project, copy the .devcontainer folder into it, reopen in container, done
keep rhino/grasshopper/bambu work on bare windows like normal. only the web/js/python stuff goes through this flow


common gotchas:

if code . doesn't work from ubuntu, close everything and reopen the ubuntu terminal (path needs refresh)
if docker desktop isn't running, the container won't start. it needs to be running in the background (it auto-starts w/ windows by default, you can disable that in its settings if it bugs you)
DO NOT put your projects in /mnt/c/... from inside wsl. always use ~/code/.... the /mnt/c path works but is 10x slower and partially defeats the isolation
if you need claude to access a new domain (e.g. some cdn), you'd add a firewall config - we can do that when it actually comes up, don't preoptimize

