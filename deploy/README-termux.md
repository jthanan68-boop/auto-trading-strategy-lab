# StratBot on an Android phone (Termux) - free, cardless, always-on

The engine runs on the phone itself; the dashboard opens in the phone's Chrome
and can be "installed" as a homescreen app. Other devices reach it through
Tailscale (optional).

## One-time phone preparation

1. Install **Termux** and **Termux:Boot** - both as APKs from
   https://github.com/termux/termux-app/releases (the same GitHub releases page
   lists `termux-boot`; the Play Store versions are outdated, do not use them).
   Open both apps once so Android fully installs them.
2. Give the project to the phone: copy `startbot-termux.zip` (attached) to the
   phone's Downloads (Drive, cable, WhatsApp file, anything).
3. In Termux:
       termux-setup-storage      # grants file access, press Allow
       cd ~
       unzip /sdcard/Download/startbot-termux.zip
       cd startbot
4. Battery rules (critical - Android kills background apps otherwise):
       Settings -> Apps -> Termux -> Battery -> Unrestricted
   On Xiaomi/Oppo/Vivo also disable their battery manager for Termux and lock
   Termux in recents.
   Optional helper: termux-wake-lock is run by the start scripts automatically.

## Install the bot (once, in Termux)

    cd ~/startbot
    bash deploy/termux_setup.sh

Installs Python + the ARM-built pandas/numpy from the Termux repo, the
pure-python deps via pip, verifies imports, carries the paper ledger over from
the PC zip, and registers the boot job.

## Start it

    cd ~/startbot
    bash deploy/termux_start.sh

- dashboard: Chrome on the phone -> http://localhost:8000
- make it a real-feeling app: Chrome menu -> "Add to Home screen"
- log:        tail -f logs/termux.log        (new shell)
- stop:       Ctrl+C or kill the termux session

## Always-on behaviour

- The script runs one engine cycle every 15 minutes, forever, with a wake-lock.
- Reboots: Termux:Boot starts the bot automatically ~10 s after the phone
  restarts (`deploy/termux_boot.sh` is copied to `~/.termux/boot` by setup).
- Keep the phone on the charger and battery optimisation off for Termux.

## Reaching the bot from other devices (optional, free)

Install Tailscale on this phone and any other device, approve the login link
it prints, then open `http://<phone-tailscale-ip>:8000` from the other device.
Nothing is exposed to the public internet.

## Differences from the PC setup

- `requirements-termux.txt` skips matplotlib/pytest (analysis stays on the PC).
- No venv: the Termux repo's prebuilt pandas/numpy only exist system-wide.
- Logs go to `logs/termux.log`; the PC build journals to the console instead.

## Verify it is healthy

    curl -s http://localhost:8000/api/status | head -c 400

Expect the limits JSON and cycles counter; the engine decides on one daily
candle and fills on the next, so quiet intervals are normal.
