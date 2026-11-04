# lunar

Open Lunar Client on the real desktop with optional test mods (removed afterwards); drive with screenshot()+desktop

- where: host
- run: `host_run(tool='lunar', args=['--mods','/path/x.jar','--minutes','15'], wait_s=5)`

## Install
(already installed)

## Notes / gotchas
Lunar AppImage ~/Applications/LunarClient.AppImage, APPIMAGE_EXTRACT_AND_RUN=1. Profiles ~/.lunarclient/profiles/<ver>/mods (only 1.8 installed as of 2026-11-03). Script must keep running: Lunar dies when the host_run sandbox ends.
