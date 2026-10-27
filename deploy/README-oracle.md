# Oracle Always Free deployment for StratBot

Goal: the paper-trading engine runs 24/7 on a permanently free Oracle cloud VM
and the dashboard opens in your phone browser from anywhere.

## Part 1 - You: create the VM (~20 min, once)

1. Go to https://cloud.oracle.com and click "Start for free".
   - Sign up with your email; a debit/credit card is required only to verify
     your identity (Always Free stays free - watch that nothing paid is created).
   - **Pick your home region carefully (e.g. Mumbai/`ap-mumbai-1`)**: the Always
     Free allowance lives forever in the signup region only.
2. Sign in to the console -> left menu -> Compute -> Instances -> "Create instance"
   - Name: `startbot`
   - Image: **Ubuntu 24.04** (keep default)
   - Shape: click "Change" -> **Ampere A1 Flex**, set 2 OCPU + 12 GB RAM
     (half of the free 4 OCPU/24 GB, enough headroom for pandas over 41 symbols)
   - SSH keys: choose "Generate a key pair" -> **download BOTH the private key
     (`.key`) and the public key (`.pub`)**, keep the private key safe
   - leave networking defaults (port 22 open is enough)
   - Create.
3. When the instance is Running, copy its **Public IP address** from the page.
4. Save the private key file as `deploy\startbot.key` in this project folder so
   the session can connect for you.

## Part 2 - The session: deploy StratBot (tell me when Part 1 is done)

I will then:
- SSH to the VM with the key, upload the project including the built
  `dashboard/dist` (no Node needed on the VM),
- run `deploy/oracle_setup.sh` (installs Python deps, registers the
  `startbot.service` - auto-start at boot, auto-restart on crash),
- verify `/api/status` answers on the VM.

## Part 3 - You: mobile access (2 sign-ins)

1. On your phone install **Tailscale** (free, Play Store/App Store) and sign in.
2. On the VM I will install Tailscale and give you a short login link - open it
   and approve. The phone then reaches the dashboard via
   `http://<vm-tailscale-ip>:8000` from anywhere.
   Nothing is exposed to the open internet; the dashboard can be reached only
   by devices signed into your Tailscale account.

## Operations cheat-sheet (on the VM)

    journalctl -u startbot -f         # live logs
    sudo systemctl restart startbot   # restart after a config change
    ls data/                          # SQLite books live here; back them up

## Fail-safe notes

- `STRATBOT_CONTROL_TOKEN` in the service file guards halt/clean endpoints;
  oraclesetup.sh randomises it. Keep a copy if you want to control the bot from
  a new device none of us has signed in yet.
- The two date-bound `test_unfinished_bars.py` failures affect only tests, not
  the engine; do not count them against the deployment check.
