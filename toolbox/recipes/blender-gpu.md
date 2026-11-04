# blender-gpu

Official Blender 5.0.1 (blender.org build): CUDA/OptiX Cycles on the RTX 4090 D + OIDN/OptiX denoising; Debian /usr/bin/blender is CPU-only

- where: both
- run: `/mnt/data/la-toolbox/opt/blender/blender -b file.blend --python script.py -- args`

## Install
curl https://download.blender.org/release/Blender5.0/blender-5.0.1-linux-x64.tar.xz | tar -xJ -C /mnt/data/la-toolbox/opt/blender --strip-components=1

## Notes / gotchas
Keep >=3-4 GB VRAM free for the desktop/games (llama-server holds ~18 GB). Headless: -b, --python, args after --. Used by /mnt/data/iwd-landing/blender/build_scene.py --device gpu.
