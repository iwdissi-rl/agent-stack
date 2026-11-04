# agent-stack

A fully local LLM agent stack and the toolbox that backs it. Two parts, one repo:

- `local-agent/` — the runtime: llama.cpp model server profiles, Nous Hermes Agent configuration, a web HUD (FastAPI) with chat profiles, crew mode (14 role prompts behind an MCP server), a Qdrant RAG server, SearXNG + Crawl4AI search services, a Claude Code delegation gate, evals and a watchdog. See `local-agent/README.md`.
- `toolbox/` — `tb`, a persistent tool manager (micromamba env, pinned binaries, shims, recipes) plus host helpers such as `mc-test` and `prettypdf`. See `toolbox/TOOLBOX.md`.

<!-- hero: docs/images/hud.png -->

## What is and is not here

This repo holds code and configuration only. Model weights, Hermes home directories, the HUD database, logs, virtualenvs, the micromamba environment and every `.env` file are excluded by `.gitignore` and are never committed. The public hostname of the reference deployment is replaced by `EXAMPLE_HOST`.

## Set it up with Claude Code

This repo includes `SETUP_WITH_CLAUDE.md`, a guided setup prompt. Claude Code reads it, checks your GPU and system, installs what is missing (asking first), helps you pick a model that fits, starts each part and checks it, and then tells you what to do next. Install [Claude Code](https://claude.com/claude-code), open a terminal in the cloned folder and run:

```sh
claude "Read SETUP_WITH_CLAUDE.md and set this project up for me"
```

## Getting started

This guide takes you from nothing to three working pieces: the model server, the web HUD and the `tb` toolbox. It assumes Linux (Ubuntu or Debian). Every command goes into a terminal. Lines that start with `#` are comments; you do not need to type them.

### 1. What you need

- **An NVIDIA GPU (needed for the model server).** The default profile `qwen3.8-27b` runs fully on a 24 GB card. Smaller cards need a smaller model (see Troubleshooting). Without an NVIDIA GPU you can still run the HUD, the search services and the toolbox, but not the default model profiles.
- **The NVIDIA driver.** It lets programs use the GPU.
  ```sh
  sudo ubuntu-drivers install    # then reboot
  nvidia-smi                     # should print a table with your GPU's name
  ```
- **Git, CMake, a C++ compiler and the CUDA toolkit.** Git downloads code. The rest builds llama.cpp, the program that runs the model.
  ```sh
  sudo apt update
  sudo apt install -y git curl cmake build-essential nvidia-cuda-toolkit
  ```
- **Python 3.12 or newer.** The HUD and `tb` are Python programs. Ubuntu 24.04 already has 3.12.
  ```sh
  sudo apt install -y python3 python3-venv
  python3 --version              # should print Python 3.12 or higher
  ```
- **Docker with Compose.** Docker runs the search services (SearXNG, Crawl4AI, Qdrant) in containers.
  ```sh
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER  # lets you use docker without sudo; log out and back in afterwards
  docker compose version         # should print a version number
  ```
- **Disk space.** About 20 GB for the default model file, plus a few GB for Docker images.
- **Optional: Nous Hermes Agent** ([github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)). It powers the chat and console pages and `./agent.sh chat`. It is not included here. You can do this whole guide without it.

**macOS:** not supported. The model profiles need an NVIDIA GPU and CUDA.

**Windows:** use WSL2 with Ubuntu (`wsl --install` in PowerShell, then restart). Install the normal NVIDIA driver on Windows; the GPU then works inside WSL2. Do every step below inside the Ubuntu terminal. Install Docker inside WSL2 with the `get.docker.com` command above, because two services use host networking, which Docker Desktop handles poorly.

### 2. Download the code

The scripts expect the stack at `/mnt/data/local-agent` and the toolbox at `/mnt/data/la-toolbox`. The easiest path is to use exactly those paths. Clone the repo into `/mnt/data` and link the two folders into place:

```sh
sudo mkdir -p /mnt/data && sudo chown $USER /mnt/data
git clone https://github.com/iwdissi-rl/agent-stack.git /mnt/data/agent-stack
ln -s /mnt/data/agent-stack/local-agent /mnt/data/local-agent
ln -s /mnt/data/agent-stack/toolbox /mnt/data/la-toolbox
cd /mnt/data/local-agent
```

No Git? On the GitHub page click **Code → Download ZIP**, unzip it, and move the folder to `/mnt/data/agent-stack`. Then run the two `ln -s` lines.

Want a different location? Then you must edit the hard-coded paths: `ROOT` in `local-agent/hud/app.py`, `ROOT` at the top of `local-agent/agent.sh` and `local-agent/scripts/run-model.sh`, and `TB` in `toolbox/bin/tb` (the wrappers `tbpy`, `prettypdf` and `mc-test` in `toolbox/bin/` use the same path). `grep -rn /mnt/data local-agent toolbox` lists every place.

### 3. Configure

Run these from `/mnt/data/local-agent`.

**a. Create the folders the HUD expects** (they hold private state, so they are not in Git):

```sh
mkdir -p workspace logs models
```

**b. Give Crawl4AI a token.** Docker Compose reads it from `services/.env`:

```sh
echo "CRAWL4AI_API_TOKEN=$(openssl rand -hex 24)" > services/.env
```

**c. Turn off the HTTP proxy settings.** The reference machine sends all web traffic through a proxy at `127.0.0.1:7897`. You almost certainly do not have one. Open these two files in a text editor (for example `nano services/docker-compose.yml`) and delete the lines shown:

- `services/docker-compose.yml`: under `crawl4ai`, delete the `HTTP_PROXY:` and `HTTPS_PROXY:` lines.
- `services/searxng/settings.yml`: under `outgoing:`, delete the three lines `proxies:`, `all://:` and `- http://127.0.0.1:7897`.

While `settings.yml` is open, replace the value of `secret_key:` with your own random string (`openssl rand -hex 32` makes one).

**d. Pick a model profile.** A profile is a small text file in `profiles/` that says which model file to load and how. The repo's `active.env` points at `qwen3.8-flash-next`, which needs about 110 GB of system RAM. Switch to the 27B profile:

```sh
ln -sfn qwen3.8-27b.env profiles/active.env
```

Open `profiles/qwen3.8-27b.env`. You only need to care about two lines:

- `MODEL=` — the full path to the model file (`.gguf`). You download it in step 4c.
- `THREADS=` — set this to your number of CPU cores (`nproc` prints it).

Leave the other values as they are. `MMPROJ=` is only for image input; if that file does not exist, it is skipped.

### 4. Install and start

**a. Build llama.cpp** (about 10–20 minutes). The model script looks for it in `local-agent/llama.cpp`:

```sh
git clone https://github.com/ggml-org/llama.cpp /mnt/data/local-agent/llama.cpp
cd /mnt/data/local-agent/llama.cpp
cmake -B build -DGGML_CUDA=ON              # prepare the build with GPU support
cmake --build build --config Release -j    # compile; ends without an error
ls build/bin/llama-server                  # this file should now exist
cd /mnt/data/local-agent
```

**b. Make a Python environment for the HUD.** This also installs `hf`, the Hugging Face download tool:

```sh
python3 -m venv hud/.venv
hud/.venv/bin/pip install fastapi uvicorn websockets httpx argon2-cffi python-multipart pillow pillow-heif huggingface_hub
```

**c. Download the model.** Model weights are not included. The first comment line of each profile names the model. Find a GGUF build of it on [huggingface.co](https://huggingface.co) (search for the model name plus "GGUF"). Pick a file that fits your GPU; the default profile used a 16.6 GB Q4_K_M file. Then download it, using the repo name and file name from the Hugging Face page:

```sh
hud/.venv/bin/hf download <user>/<repo> <file>.gguf --local-dir models/<folder>
```

Put the full path of the downloaded file into the `MODEL=` line of `profiles/qwen3.8-27b.env`, for example `MODEL=/mnt/data/local-agent/models/<folder>/<file>.gguf`.

**d. Start the search services:**

```sh
cd /mnt/data/local-agent/services
docker compose up -d          # first time downloads the images; ends with three containers "Started"
docker compose ps             # searxng, crawl4ai and qdrant should be listed as running
cd /mnt/data/local-agent
```

**e. Start the model server.** Open a new terminal for this; it keeps running:

```sh
cd /mnt/data/local-agent
./scripts/run-model.sh        # loads the model; takes about 15 s to a few minutes
```

In another terminal, check it:

```sh
curl http://127.0.0.1:8181/health    # {"status":"ok"} means the model is ready
```

**f. Set a HUD password and start the HUD.** Open another new terminal:

```sh
cd /mnt/data/local-agent/hud
read -rs P && printf '%s' "$P" | .venv/bin/python set_password.py owner; unset P
# type a password (8+ characters), press Enter; nothing shows while you type
# you should see a line starting with: password set for 'owner'
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8190 --ws websockets
# you should see: Uvicorn running on http://127.0.0.1:8190
```

**g. Set up the toolbox.** `tb` installs tools with micromamba, which is not in Git. Download it and create the main tool environment:

```sh
curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj -C /tmp bin/micromamba
mv /tmp/bin/micromamba /mnt/data/la-toolbox/bin/micromamba
MAMBA_ROOT_PREFIX=/mnt/data/la-toolbox/mamba /mnt/data/la-toolbox/bin/micromamba create -y -p /mnt/data/la-toolbox/env
# you should see: Empty environment created at prefix: /mnt/data/la-toolbox/env
```

Put the toolbox on your PATH so `tb` and the tools it installs work from anywhere:

```sh
echo 'export PATH="/mnt/data/la-toolbox/bin:$PATH:/mnt/data/la-toolbox/env/bin"' >> ~/.bashrc
source ~/.bashrc
```

### 5. Your first run

1. Open **http://127.0.0.1:8190** in your browser. Log in with the username `owner` and the password from step 4f. The Overview page shows the model server, the search services and your GPU.
2. Check every part from a terminal:
   ```sh
   cd /mnt/data/local-agent
   ./agent.sh status
   ```
   `model server :8181`, `searxng :8888`, `crawl4ai :11235`, `qdrant :6333` and `web HUD :8190` should say `up`. The words in brackets (such as `[inactive]`) refer to systemd units; they stay inactive while you start things by hand, which is fine. `embeddings :8182` and `sandbox relay :7898` stay `DOWN` until you set up the RAG and sandbox parts.
3. Try the toolbox:
   ```sh
   tb list           # every tool in the registry, one per line
   tb show jq        # the recipe: how jq is installed and run
   tb install jq     # installs jq from conda-forge into the toolbox
   jq --version      # prints a version: it worked
   tb doctor         # checks every registered tool
   ```
   `tb doctor` will report many tools as `MISSING`. That is expected: the registry lists what the reference machine installed. Each one installs with `tb install <name>`.

Next steps: install Hermes Agent into `local-agent/hermes-agent` (the HUD looks for `hermes-agent/.venv/bin/hermes`) to use chat, the console and `./agent.sh chat`. `./agent.sh start|stop|restart` drives systemd user units named `la-model`, `la-embed`, `la-relay`, `la-services`, `la-hud` and `la-watchdog.timer`. Those unit files are not in this repo; `local-agent/README.md` ("Always on") describes what each one runs.

### 6. Troubleshooting

- **"command not found" right after installing something** — close the terminal and open a new one, or run `source ~/.bashrc`.
- **"permission denied" from Docker** — run `sudo usermod -aG docker $USER`, then log out and back in.
- **"address already in use"** — another program has the port. `ss -ltnp | grep 8190` (or 8181, 8888, 6333) shows which one. Stop it, or pick another port in the start command.
- **The model server stops with "out of memory" or `cudaMalloc failed`** — the model does not fit on your GPU. Lower `CTX=` in the profile (try `32768`). If that is not enough, download a smaller GGUF (fewer parameters or a smaller quant such as Q4_K_S), copy the profile to `profiles/my-model.env`, set its `MODEL=`, and point `profiles/active.env` at it with `ln -sfn my-model.env profiles/active.env`.
- **The HUD says "No owner password set"** — run the `set_password.py` line from step 4f again.
- **Python errors such as "No module named" or a syntax error** — check `python3 --version` is 3.12 or newer, and start the HUD with `hud/.venv/bin/uvicorn`, not a system `uvicorn`.

## Keeping the repo in sync

`./sync.sh` re-exports the code/config subset from a live install into this repo (defaults to the paths above), scrubs the hostname, and leaves state and secrets behind.

## License

MIT. See `LICENSE`. Third-party components (llama.cpp, Hermes Agent, SearXNG, Crawl4AI, Qdrant, micromamba) are not vendored here and keep their own licences.

---

made by IWDissi and the fabulous claude assistant 😋
