# Deploying to a free 24/7 cloud VM (Oracle Cloud Always Free)

This runs the bot **and** PostgreSQL together via Docker Compose on an
always-on VM, so it survives lid-close, sleep, and reboots without a laptop.
The compose stack is proven: `docker compose up -d` brings up bot + Postgres
in PAPER mode reaching Binance, with `restart: unless-stopped`.

> **What only YOU can do:** create the Oracle account and the VM (needs your
> details + a card for identity verification — the Always Free resources are
> not charged). Everything after SSH is copy-paste below.

---

## Part A — Create the VM (browser, ~15 min)

1. Sign up at <https://www.oracle.com/cloud/free/>. Choose a **home region you
   are NOT blocked from Binance in** — for NZ pick **Sydney, Melbourne, or
   Singapore**. **Do NOT pick a US region** (Binance blocks US IPs).
   > The home region is permanent, so choose carefully at signup.
2. Console → **Compute → Instances → Create instance**.
   - **Image:** Canonical Ubuntu 24.04 (or 22.04).
   - **Shape:** "Always Free eligible" — either `VM.Standard.A1.Flex`
     (ARM, give it 1 OCPU / 6 GB — plenty) or `VM.Standard.E2.1.Micro` (AMD).
     If ARM capacity is unavailable in your region, use the AMD micro; this
     bot needs ~256 MB.
   - **SSH keys:** upload your public key, or let Oracle generate one and
     **download the private key** — you need it to log in.
3. After it boots, copy the instance's **public IP**.
4. SSH in from your Mac terminal:
   ```bash
   chmod 600 ~/Downloads/your-key.key           # the private key you downloaded
   ssh -i ~/Downloads/your-key.key ubuntu@<PUBLIC_IP>
   ```

## Part B — The one test that decides everything (30 sec)

Binance blocks many cloud IPs. **Run this first, on the VM:**
```bash
curl -s "https://api.binance.com/api/v3/time"
```
- ✅ `{"serverTime":...}` → this VM works, continue.
- ❌ hangs, or `403`/`451` → this IP is blocked. Terminate the instance and
  create a new one in a different region, or try a different provider. Do not
  proceed until this returns a serverTime.

## Part C — Install Docker (one time, ~3 min)

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
newgrp docker           # or log out/in so 'docker' works without sudo
docker run --rm hello-world   # sanity check
```

## Part D — Deploy the bot (~3 min)

```bash
git clone https://github.com/rukshanm123-art/trading-bot.git
cd trading-bot

# create .env with a strong DB password (nothing else needed for paper)
cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 16)
EOF

./scripts/deploy_update.sh --no-pull   # builds image, starts Postgres + bot
```

**Always deploy with `scripts/deploy_update.sh`, never a bare
`docker compose up -d --build`.** The image ships no `.git`, so the script
stamps it with the deployed commit (`GIT_COMMIT`). Without that stamp the
container cannot identify its own code and records **no** qualification
evidence — the bot runs perfectly and still shows `0/30` days forever.

Confirm it's up and qualifying — check for all three lines:
```bash
docker compose ps                         # both should be "healthy"
docker compose logs bot | grep -i qualification
#  -> "qualification-eligible storage: live-market PAPER is using PostgreSQL"
#  -> "qualification provenance: image <commit>"
#  ...and after the first 30 minutes:
#  -> "qualification evidence flushed (30.0 min, N decisions)"
```
Any `NON-QUALIFYING PAPER RUN` line means the clock is not running. Fix it
before you start counting days.

That's it — it's running 24/7. Note the date; the 30-day clock starts now.

---

## Day-to-day (all on the VM)

```bash
cd ~/trading-bot

# live logs
docker compose logs -f bot

# status / reports (package is installed in the image — no PYTHONPATH needed)
docker compose exec bot python -m trading_bot --config config/paper.yaml status
docker compose exec bot python -m trading_bot --config config/paper.yaml report performance

# daily report files are inside the bot's volume:
docker compose exec bot ls var/reports
```

### Stopping / emergency
```bash
docker compose stop bot        # simplest full stop (Postgres keeps running)
docker compose start bot       # resume

# kill switch WITHOUT stopping the container (blocks new entries, keeps monitoring):
#   add  TRADING_KILL_SWITCH=true  to .env, then:
docker compose up -d bot
```

### Updating to a new version
```bash
cd ~/trading-bot
./scripts/deploy_update.sh     # git pull + rebuild + restart, commit-stamped
```
It prints the container's `.build_info.json` at the end; if `git_commit` is
empty, the deployment records no qualification evidence — do not leave it that
way. Postgres data (and the evidence ledger) persists across rebuilds.

### Reboots
`restart: unless-stopped` means both containers come back automatically after
a VM reboot. Nothing to do.

---

## Security notes (already handled, but know them)

- The monitoring endpoint binds inside the container only — it is **not**
  published to the VM's public IP. Keep it that way. If you ever expose it,
  set `MONITORING_TOKEN` in `.env` and put it behind a firewall rule.
- Oracle's default security list blocks inbound except SSH (port 22) — leave
  it that way. The bot only needs **outbound** HTTPS to Binance.
- `.env` holds the DB password; it is gitignored and never leaves the VM.
- Live trading remains locked regardless of host — it needs the full unlock
  ceremony and, per your config, PostgreSQL (which this deploy uses).

## When you move to LIVE (much later)

Same VM, but: set testnet keys first and run `config/testnet.yaml` for the
Stage-4 drills, then follow `docs/LIVE_TRADING_CHECKLIST.md`. A cloud VM (not a
laptop) is the correct host for live because it never sleeps — the protective
stop is only monitored while the process runs.
