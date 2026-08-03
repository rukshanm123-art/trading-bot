# Deploying the Testnet stack (Stage 4)

This runs the bot in **testnet** mode against the official Binance Spot
Testnet on a **separate** always-on VM, so it never touches the paper
qualification stack or its instance lock. Testnet is where you exercise the
real trade lifecycle (which a ~30 USDT paper account rarely does) and run the
failure drills in `docs/TESTNET_DRILLS.md`.

> Testnet uses fake money on Binance's official test network. No real funds
> are ever at risk here. LIVE remains locked regardless of this stack.

---

## Part A — Create the VM (browser, ~15 min)

Same provider as the paper VM, but sized with headroom so a second engine
never fights for memory.

1. Oracle Cloud Console → **Compute → Instances → Create instance**.
   - **Image:** Oracle Linux 9 (or Ubuntu 22.04/24.04).
   - **Shape:** `VM.Standard.A1.Flex` (Always Free eligible, **ARM**). Give it
     **1 OCPU / 6 GB RAM**. The extra RAM means no swap tuning and no
     one-package-at-a-time Docker install like the 500 MB paper box needed.
   - If ARM capacity is unavailable in your region, retry later or another AD;
     ARM Always-Free capacity comes and goes. A second `E2.1.Micro` also works
     but would need the same 4.5 GB swap tuning as the paper box.
   - **SSH keys:** upload your public key, or download the generated private
     key.
2. Copy the instance's **public IP**.
3. `chmod 600` your key and SSH in (user is `opc` on Oracle Linux, `ubuntu`
   on Ubuntu):
   ```bash
   ssh -i ~/path/to/key.key opc@<TESTNET_PUBLIC_IP>
   ```

## Part B — The reachability test (30 sec, on the VM)

Testnet has its own host; confirm this IP can reach it before anything else:
```bash
curl -s "https://testnet.binance.vision/api/v3/time"
```
- ✅ `{"serverTime":...}` → continue.
- ❌ hangs / `403` / `451` → this IP is blocked. Recreate in another region.

## Part C — Install Docker (one time)

On Oracle Linux 9 (6 GB, so no OOM dance):
```bash
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin git
sudo systemctl enable --now docker
```
(On Ubuntu: `sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git`.)

## Part D — Get testnet API keys (browser, ~2 min)

1. Go to <https://testnet.binance.vision>, sign in with GitHub.
2. **Generate HMAC_SHA256 Key** → copy the **API Key** and **Secret Key**
   (the secret is shown once).
3. These are fake-money keys — but still treat them as secrets. They go in
   `.env` on the VM in the next step, never in the repo.

## Part E — Deploy (~3 min)

```bash
git clone https://github.com/rukshanm123-art/trading-bot.git
cd trading-bot

# create .env with the testnet keys (and reuse your Telegram values if you
# want testnet alerts, tagged [testnet], on the same channel)
cat > .env <<'EOF'
BINANCE_TESTNET_API_KEY=PASTE_YOUR_TESTNET_API_KEY
BINANCE_TESTNET_API_SECRET=PASTE_YOUR_TESTNET_SECRET
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF

./scripts/deploy_testnet.sh --no-pull
```

The deployer stamps the image commit and prints `.build_info.json` at the end.

Confirm it's up:
```bash
sudo docker compose -f docker-compose.testnet.yml ps          # bot healthy
sudo docker compose -f docker-compose.testnet.yml logs -f bot # watch it
#  -> "TESTNET mode — official Spot Testnet, no real funds"
#  -> "instance lock acquired ..."
```

---

## Day-to-day (on the testnet VM)

```bash
cd ~/trading-bot
C="sudo docker compose -f docker-compose.testnet.yml"

$C logs -f bot                                                   # live logs
$C exec bot python -m trading_bot --config config/testnet.yaml status
$C exec bot python -m trading_bot --config config/testnet.yaml report performance
$C exec bot python -m trading_bot --config config/testnet.yaml notify test   # verify Telegram
```

### Update to a new version
```bash
cd ~/trading-bot && ./scripts/deploy_testnet.sh
```

### Stop / resume
```bash
sudo docker compose -f docker-compose.testnet.yml stop bot
sudo docker compose -f docker-compose.testnet.yml start bot
```

---

## After testnet

Run every drill in `docs/TESTNET_DRILLS.md` and record the outcomes on
`docs/LIVE_TRADING_CHECKLIST.md`. Testnet is not code-enforced by the live
gate — it is operator discipline. Only once the drills pass, the 30-day paper
gate clears, and every other live-gate prerequisite is green do you consider a
tiny live balance under `DAILY_APPROVAL`.
