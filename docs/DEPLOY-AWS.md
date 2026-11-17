# Deploying to AWS

**The key fact:** the `MetaTrader5` Python package only works on **Windows**. Pick the option that matches what you need.

| Option | Live trading | Cost | Use when |
|---|---|---|---|
| A. Run on your own Windows PC | Yes | Free | You want it working now |
| B. Windows EC2 instance | Yes | Paid (Windows licence + hours) | You need it on 24/7 |
| C. Linux EC2 instance | No (demo mode) | Free-tier eligible | Showing the dashboard / portfolio demo |

## Security rules for any server

- The app has a login, but it is plain HTTP by default. **Never expose port 5000 to the internet.**
- Security group: allow inbound only from **your own IP** (or none, and use an SSH tunnel / RDP).
- For HTTPS put nginx or an AWS load balancer in front, and set `COOKIE_SECURE=1` and `TRUST_PROXY=1`.
- Do not copy `.env`, `data/` or logs to GitHub or the server by hand; enter the Exness details in the web form.

## Option C: Linux EC2 (demo mode)

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/LingarajH-011/exness-trading-bot.git && cd exness-trading-bot
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
PORT=5000 python app.py        # prints the one-time setup code
```

Reach it from your PC without opening any port:

```bash
ssh -L 5000:127.0.0.1:5000 ec2-user@<server-ip>      # then browse http://127.0.0.1:5000
```

To keep it running, create `/etc/systemd/system/exness-bot.service` with `ExecStart=/home/<user>/exness-trading-bot/.venv/bin/python app.py`, `WorkingDirectory=...`, `Restart=always`, then `sudo systemctl enable --now exness-bot`.

## Option B: Windows EC2

1. Launch a Windows Server instance (t3.medium or larger; this is **not** free tier).
2. Security group: RDP (3389) from your IP only. Connect with Remote Desktop.
3. Install MT5 (Exness build) and Python, then follow the Windows quick start in the README.
4. Run `python app.py` as a scheduled task or with [NSSM](https://nssm.cc) so it survives reboots. Reach the dashboard over RDP at `127.0.0.1:5000`.
5. Stop the instance when not in use to avoid charges.

Running MT5 under Wine on Linux is not supported by this project.
