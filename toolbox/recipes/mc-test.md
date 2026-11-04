# mc-test

Launch MC client (vanilla/fabric/forge/neoforge/quilt) with mods, wait for title screen/crash, screenshot, report

- where: both
- run: `mc-test fabric:1.21.5 mod.jar`

## Install
(already installed)

## Notes / gotchas
Sandbox = Xvfb + llvmpipe (slow but works); host_run mc-gpu = real GPU. Game files cached in opt/minecraft (portablemc --main-dir). Offline user la_test. Java auto: 26.x->25, >=1.20.5->21, older->17.
