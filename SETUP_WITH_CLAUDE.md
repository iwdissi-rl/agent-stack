# Set up agent-stack with Claude Code

You are Claude Code. The user wants the agent-stack running on their own Linux machine. When you are done, three pieces work: the llama.cpp model server answers on http://127.0.0.1:8181, the web HUD runs on http://127.0.0.1:8190 with an owner password the user chose, and the `tb` toolbox is on the PATH and can install tools. The search services (SearXNG, Crawl4AI, Qdrant) run in Docker. Follow the steps below in order. The README's "Getting started" section is the source for every command here; if something here and the README disagree, trust the README and tell the user.

## How to work

- Before each step, give the user a one or two line update: what you are about to do and why.
- Ask before any `sudo` command or system-wide install (driver, apt packages, Docker, creating `/mnt/data`).
- Ask before changing shell or service configuration: `~/.bashrc`, the `docker` group, systemd units.
- Ask before any download larger than about 1 GB. The model file is about 17 GB for the default profile, and the CUDA toolkit and Docker images are large too.
- Never print, log or commit secrets or `.env` values. `services/.env` holds the Crawl4AI token: create it with the command below and never `cat` it. The SearXNG `secret_key` and the HUD password are secrets too. The profile files in `profiles/*.env` are model settings, not secrets; you may read and edit them.
- The HUD password must come from the user. Do not choose it, and do not put it on a command line or in a file. Ask the user to run the password line themselves (step 9).
- Do not modify files outside the project, except for installing prerequisites and the install-path links the user agrees to in step 3.
- Do not run any git command that writes in this repo (`add`, `commit`, `push`). Some setup steps edit tracked files (`services/docker-compose.yml`, `services/searxng/settings.yml`, the profile). Tell the user those edits now differ from Git and must not be committed, because `settings.yml` will hold their secret key.
- If a step fails twice, stop. Show the user the error and ask how to continue. The README's "6. Troubleshooting" list covers the common failures; check it first.

## 1. Detect the system

Run read-only checks and report the results to the user in plain words:

```sh
uname -s -m                                  # must be Linux
cat /etc/os-release 2>/dev/null | head -3    # Ubuntu or Debian expected
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
nproc                                        # CPU cores, needed for THREADS= later
free -g                                      # system RAM
df -h /mnt 2>/dev/null || df -h /            # free disk space
```

- **An NVIDIA GPU is required for the model server.** The default profile `qwen3.8-27b` runs fully on a 24 GB card. Tell the user their VRAM from the `memory.total` column. With less than 24 GB they need a smaller model (step 7). Without an NVIDIA GPU, tell them they can still run the HUD, the search services and the toolbox, but not the model profiles. Ask whether to continue on that basis.
- If `nvidia-smi` is missing or fails, the driver is not installed (step 2).
- **macOS is not supported.** The profiles need an NVIDIA GPU and CUDA. Stop and tell the user.
- **Windows:** use WSL2 with Ubuntu (`wsl --install` in PowerShell, then restart). The normal NVIDIA driver goes on Windows; the GPU then works inside WSL2. Run Claude Code inside the Ubuntu terminal. Install Docker inside WSL2 with the `get.docker.com` script, not Docker Desktop, because two services use host networking.
- Disk: about 20 GB for the default model file, plus a few GB for Docker images.

## 2. Check the prerequisites

Check each one. Install only what is missing, and ask before installing.

| Tool | Check | Install |
|---|---|---|
| NVIDIA driver | `nvidia-smi` prints a table with the GPU name | `sudo ubuntu-drivers install`, then the user reboots |
| git, curl, CMake, C++ compiler, CUDA toolkit | `git --version`, `curl --version`, `cmake --version`, `g++ --version`, `nvcc --version` | `sudo apt update` then `sudo apt install -y git curl cmake build-essential nvidia-cuda-toolkit` |
| Python 3.12 or newer, with venv | `python3 --version` | `sudo apt install -y python3 python3-venv` (Ubuntu 24.04 already has 3.12) |
| Docker with Compose | `docker --version`, `docker compose version` | `curl -fsSL https://get.docker.com \| sh` |
| openssl | `openssl version` | Usually already installed |

Notes:

- After installing the driver the machine needs a reboot. You cannot do that for the user: tell them, and continue after they come back.
- To use Docker without `sudo`, the user needs `sudo usermod -aG docker $USER` and must then log out and back in. Ask before running it. Docker "permission denied" errors mean this step is missing.
- Nous Hermes Agent (github.com/NousResearch/hermes-agent) is optional and not part of this guide. It powers chat, the console and `./agent.sh chat`. Mention it in the final step.

## 3. Choose the install location

The scripts have hard-coded paths. They expect the stack at `/mnt/data/local-agent` and the toolbox at `/mnt/data/la-toolbox`. Explain this to the user and ask which they prefer:

**A. Use the expected paths (easiest, the README's way).** This needs `sudo` once to create `/mnt/data`:

```sh
sudo mkdir -p /mnt/data && sudo chown $USER /mnt/data
git clone https://github.com/iwdissi-rl/agent-stack.git /mnt/data/agent-stack
ln -s /mnt/data/agent-stack/local-agent /mnt/data/local-agent
ln -s /mnt/data/agent-stack/toolbox /mnt/data/la-toolbox
cd /mnt/data/local-agent
```

If the repo is already cloned somewhere else, ask whether to move it to `/mnt/data/agent-stack` and then run the two `ln -s` lines.

**B. Keep a different location.** Then you must edit the hard-coded paths: `ROOT` in `local-agent/hud/app.py`, `ROOT` at the top of `local-agent/agent.sh` and `local-agent/scripts/run-model.sh`, and `TB` in `toolbox/bin/tb`. The wrappers `tbpy`, `prettypdf` and `mc-test` in `toolbox/bin/` use the same path. List every place first and show the user what you plan to change:

```sh
grep -rn /mnt/data local-agent toolbox
```

The rest of this guide uses the paths from option A. With option B, replace `/mnt/data/local-agent` and `/mnt/data/la-toolbox` with the user's chosen folders everywhere.

Before going on, confirm you are in the right place:

```sh
git -C /mnt/data/agent-stack remote get-url origin   # should mention iwdissi-rl/agent-stack
ls /mnt/data/local-agent/agent.sh /mnt/data/la-toolbox/bin/tb
```

## 4. Create the private folders

Run from `/mnt/data/local-agent`. These hold private state, so they are not in Git:

```sh
mkdir -p workspace logs models
```

## 5. Configure the search services

**a. Crawl4AI token.** Docker Compose reads it from `services/.env`. Create it without printing the value:

```sh
echo "CRAWL4AI_API_TOKEN=$(openssl rand -hex 24)" > services/.env
```

If `services/.env` already exists, ask before overwriting it.

**b. HTTP proxy settings.** The reference machine sends web traffic through a proxy at `127.0.0.1:7897`. Ask the user if they have one there. They almost certainly do not; if not, delete these lines:

- `services/docker-compose.yml`: under `crawl4ai`, the `HTTP_PROXY:` and `HTTPS_PROXY:` lines.
- `services/searxng/settings.yml`: under `outgoing:`, the three lines `proxies:`, `all://:` and `- http://127.0.0.1:7897`.

Show the user the lines before and after the edit (these lines are not secret).

**c. SearXNG secret key.** Replace the value of `secret_key:` in `services/searxng/settings.yml` with a fresh random string from `openssl rand -hex 32`. Write it straight into the file without printing it, for example:

```sh
sed -i "s|^\(\s*secret_key:\).*|\1 \"$(openssl rand -hex 32)\"|" services/searxng/settings.yml
grep -c 'CHANGE_ME' services/searxng/settings.yml   # should print 0
```

## 6. Build llama.cpp and the HUD environment

**a. llama.cpp.** About 10 to 20 minutes. `scripts/run-model.sh` runs `llama.cpp/build/bin/llama-server` inside `local-agent`:

```sh
git clone https://github.com/ggml-org/llama.cpp /mnt/data/local-agent/llama.cpp
cd /mnt/data/local-agent/llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j
ls build/bin/llama-server
cd /mnt/data/local-agent
```

The build must end without an error and `build/bin/llama-server` must exist.

**b. Python environment for the HUD.** It also installs `hf`, the Hugging Face download tool used in step 7. Run from `/mnt/data/local-agent`:

```sh
python3 -m venv hud/.venv
hud/.venv/bin/pip install fastapi uvicorn websockets httpx argon2-cffi python-multipart pillow pillow-heif huggingface_hub
```

## 7. Pick a model profile and download the model

A profile is a small file in `profiles/` that says which model file to load and how. The repo's `profiles/active.env` points at `qwen3.8-flash-next`, which needs about 110 GB of system RAM. Unless the user really has that, switch to the 27B profile:

```sh
ln -sfn qwen3.8-27b.env profiles/active.env
```

Match the profile to the VRAM from step 1:

- **24 GB or more:** keep `qwen3.8-27b`.
- **Less than 24 GB:** first try lowering `CTX=` in the profile (try `32768`). If that is not enough, the user needs a smaller GGUF (fewer parameters, or a smaller quant such as Q4_K_S). Copy the profile to `profiles/my-model.env`, set its `MODEL=`, and run `ln -sfn my-model.env profiles/active.env`.

**Model download.** Weights are not included. Ask first: this is a large download (the default profile used a 16.6 GB Q4_K_M file). The first comment line of the profile names the model:

```sh
grep -m1 '^#' profiles/qwen3.8-27b.env
```

Let the user choose the GGUF. Tell them to search huggingface.co for the model name plus "GGUF" and pick a file that fits their GPU. Do not pick one for them. Then download it with `hf` from the HUD's Python environment (step 6b), using the repo and file names the user gives you:

```sh
hud/.venv/bin/hf download <user>/<repo> <file>.gguf --local-dir models/<folder>
```

Then edit the active profile. Only two lines matter:

- `MODEL=` the full path of the downloaded file, for example `MODEL=/mnt/data/local-agent/models/<folder>/<file>.gguf`.
- `THREADS=` the number of CPU cores from `nproc`.

Leave the other values as they are. `MMPROJ=` is only for image input; if that file does not exist, `run-model.sh` skips it.

## 8. Start the services and the model server

**a. Search services:**

```sh
cd /mnt/data/local-agent/services
docker compose up -d
docker compose ps
cd /mnt/data/local-agent
```

The first run downloads the images and ends with three containers "Started". `docker compose ps` should list `searxng`, `crawl4ai` and `qdrant` as running.

**b. Model server.** It keeps running, so start it in the background (or ask the user to run it in a new terminal):

```sh
cd /mnt/data/local-agent
./scripts/run-model.sh
```

Loading takes about 15 seconds to a few minutes. Then check it:

```sh
curl http://127.0.0.1:8181/health    # {"status":"ok"} means the model is ready
```

If it stops with "out of memory" or `cudaMalloc failed`, the model does not fit. Go back to step 7 and lower `CTX=` or choose a smaller GGUF.

## 9. Set the HUD password and start the HUD

Ask the user to run this themselves in their own terminal, so the password never passes through you:

```sh
cd /mnt/data/local-agent/hud
read -rs P && printf '%s' "$P" | .venv/bin/python set_password.py owner; unset P
```

They type a password of 8 or more characters and press Enter; nothing shows while they type. They should see a line starting with `password set for 'owner'`. Wait until they confirm.

Then start the HUD in the background:

```sh
cd /mnt/data/local-agent/hud
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8190 --ws websockets
```

It should print `Uvicorn running on http://127.0.0.1:8190`. If the HUD later says "No owner password set", the password line was not run.

## 10. Set up the toolbox

`tb` installs tools with micromamba, which is not in Git:

```sh
curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj -C /tmp bin/micromamba
mv /tmp/bin/micromamba /mnt/data/la-toolbox/bin/micromamba
MAMBA_ROOT_PREFIX=/mnt/data/la-toolbox/mamba /mnt/data/la-toolbox/bin/micromamba create -y -p /mnt/data/la-toolbox/env
```

It should print `Empty environment created at prefix: /mnt/data/la-toolbox/env`.

Putting the toolbox on the PATH changes `~/.bashrc`, so ask first:

```sh
echo 'export PATH="/mnt/data/la-toolbox/bin:$PATH:/mnt/data/la-toolbox/env/bin"' >> ~/.bashrc
source ~/.bashrc
```

Your own shell may not pick this up. Use full paths (`/mnt/data/la-toolbox/bin/tb`) in the next steps if `tb` is not found.

## 11. Test each part

There is no unit test suite to run here. Check each part instead:

```sh
cd /mnt/data/local-agent
./agent.sh status
```

`model server :8181`, `searxng :8888`, `crawl4ai :11235`, `qdrant :6333` and `web HUD :8190` should say `up`. The words in brackets (such as `[inactive]`) refer to systemd units; they stay inactive while things are started by hand, which is fine. `embeddings :8182` and `sandbox relay :7898` stay `DOWN` until the RAG and sandbox parts are set up; that is expected.

Then smoke-test the model server's chat and tool calling:

```sh
hud/.venv/bin/python scripts/test_server.py
```

It prints `[plain]`, `[tool]`, `[parallel]` and `[tool-result]` lines and ends with `ALL OK` (or `SOME CHECKS FAILED`). Report the result. A smaller model may fail some tool-calling checks; tell the user rather than trying to fix it.

Check that SearXNG answers searches:

```sh
curl 'http://127.0.0.1:8888/search?q=test&format=json' | head -c 300
```

## 12. First run

1. Tell the user to open **http://127.0.0.1:8190** in their browser and log in with the username `owner` and the password from step 9. The Overview page shows the model server, the search services and the GPU.
2. Try the toolbox and show the user the output:

   ```sh
   tb list           # every tool in the registry
   tb show jq        # the recipe for jq
   tb install jq     # installs jq from conda-forge into the toolbox
   jq --version      # prints a version
   tb doctor         # checks every registered tool
   ```

   `tb doctor` will report many tools as `MISSING`. That is expected: the registry lists what the reference machine installed. Each one installs with `tb install <name>`.

## 13. Tell the user what to do next

Give a short summary: GPU and profile used, model file, which parts are up, and which files now differ from Git (`services/docker-compose.yml`, `services/searxng/settings.yml`, the profile). Remind them not to commit those. Then give them these commands:

```sh
cd /mnt/data/local-agent && ./agent.sh status                     # check every part
cd /mnt/data/local-agent && ./scripts/run-model.sh                 # start the model server again
cd /mnt/data/local-agent/services && docker compose up -d          # start the search services again
cd /mnt/data/local-agent/hud && .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8190 --ws websockets   # start the HUD again
tb install <name>                                                  # add a tool from the registry
```

Also tell them:

- To use chat, the console and `./agent.sh chat`, install Hermes Agent into `local-agent/hermes-agent` (the HUD looks for `hermes-agent/.venv/bin/hermes`).
- `./agent.sh start|stop|restart` drives systemd user units named `la-model`, `la-embed`, `la-relay`, `la-services`, `la-hud` and `la-watchdog.timer`. Those unit files are not in this repo; `local-agent/README.md` ("Always on") describes what each one runs. Do not create them unless the user asks.
